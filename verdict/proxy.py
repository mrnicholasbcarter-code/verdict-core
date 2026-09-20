"""OpenAI-compatible upstream transport for the HTTP proxy.

The transport is intentionally protocol-oriented: it does not deserialize chat
responses or discard fields, so upstream usage, tool calls, errors, and future
response extensions can pass through unchanged.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import httpx

from verdict.capability_passports import RouteIdentity
from verdict.responses_compatibility import adapt_responses_payload
from verdict.security import host_is_allowed, pin_upstream_url, validate_upstream_url

_HOP_BY_HOP_HEADERS = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)
_UNSAFE_RESPONSE_HEADERS = frozenset({"set-cookie"})


@dataclass(frozen=True)
class BufferedUpstreamResponse:
    """A fully buffered upstream response suitable for Starlette ``Response``."""

    status_code: int
    headers: list[tuple[str, str]]
    body: bytes
    actual_route: RouteIdentity | None = None
    compatibility_rule_version: str | None = None


@dataclass(frozen=True)
class StreamedUpstreamResponse:
    """An upstream response whose body remains an arbitrary byte stream."""

    status_code: int
    headers: list[tuple[str, str]]
    body: AsyncIterator[bytes]
    actual_route: RouteIdentity | None = None
    compatibility_rule_version: str | None = None


class UpstreamProxy:
    """Forward OpenAI-compatible requests to one configured upstream URL."""

    def __init__(
        self,
        base_url: str,
        api_key: str | None = None,
        timeout: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
        allow_private_hosts: set[str] | None = None,
    ) -> None:
        if not base_url.strip():
            raise ValueError("upstream base URL must not be empty")
        self.allow_private_hosts = allow_private_hosts or set()
        normalized = validate_upstream_url(base_url, allow_private_hosts=self.allow_private_hosts)
        self.base_url = normalized
        self.api_key = api_key
        self.timeout = timeout
        self.transport = transport

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=self.transport, timeout=self.timeout, follow_redirects=False
        )

    def _validate_destination(self) -> None:
        """Re-resolve DNS immediately before transport use to reduce rebinding risk."""
        if self.transport is None:
            from urllib.parse import urlsplit

            host = urlsplit(self.base_url).hostname
            if host:
                host_is_allowed(host, self.allow_private_hosts)

    def _headers(self) -> dict[str, str]:
        headers = {"accept": "application/json"}
        if self.api_key:
            headers["authorization"] = f"Bearer {self.api_key}"
        return headers

    def _url(self, path: str) -> str:
        return f"{self.base_url}/{path.lstrip('/')}"

    def _build_request(
        self,
        method: str,
        path: str,
        *,
        headers: dict[str, str] | None = None,
        json_payload: dict[str, Any] | None = None,
    ) -> httpx.Request:
        """Build a credential-bearing request pinned to the validated address."""
        url = self._url(path)
        pinned_headers: dict[str, str] = {}
        extensions: dict[str, str] = {}
        if self.transport is None:
            url, pinned_headers, extensions = pin_upstream_url(url, self.allow_private_hosts)
        return httpx.Request(
            method,
            url,
            headers={**self._headers(), **pinned_headers, **(headers or {})},
            json=json_payload,
            extensions=extensions,
        )

    def route_identity(self, model: str, protocol: str) -> RouteIdentity:
        """Describe the configured executable route without reading gateway state."""

        provider = model.split("/", 1)[0] if "/" in model else "configured-upstream"
        return RouteIdentity(
            gateway="verdict-upstream",
            provider=provider,
            connection="configured",
            endpoint=self._url(
                "responses" if protocol == "openai.responses" else "chat/completions"
            ),
            protocol=protocol,
            model_id=model,
        )

    @staticmethod
    def _actual_route(
        response: httpx.Response, *, requested_model: str | None = None, protocol: str
    ) -> RouteIdentity | None:
        """Decode an optional adapter-owned route attestation header.

        Generic OpenAI providers do not expose this header.  In that case the
        relay records the configured route but deliberately does not claim an
        observed member route for opaque gateway aliases.
        """

        raw = response.headers.get("x-verdict-actual-route")
        if raw:
            try:
                value = json.loads(raw)
                return RouteIdentity.from_dict(value) if isinstance(value, dict) else None
            except (TypeError, ValueError):
                return None
        if requested_model is None or response.status_code >= 400:
            return None
        try:
            value = response.json()
        except (TypeError, ValueError):
            return None
        served_model = value.get("model") if isinstance(value, dict) else None
        if not isinstance(served_model, str) or not served_model.strip():
            return None
        provider = served_model.split("/", 1)[0] if "/" in served_model else "configured-upstream"
        return RouteIdentity(
            gateway="provider-response",
            provider=provider,
            connection="response-body",
            endpoint="provider-response-body",
            protocol=protocol,
            model_id=served_model,
        )

    @staticmethod
    def _response_headers(response: httpx.Response) -> list[tuple[str, str]]:
        return [
            (name, value)
            for name, value in response.headers.multi_items()
            if name.lower() not in _HOP_BY_HOP_HEADERS
            and name.lower() not in _UNSAFE_RESPONSE_HEADERS
            and not name.lower().startswith("x-verdict-")
        ]

    async def models(self) -> BufferedUpstreamResponse:
        """Fetch the configured upstream model catalog without reshaping it."""
        client = self._client()
        try:
            request = self._build_request("GET", "models")
            response = await client.send(request)
            return BufferedUpstreamResponse(
                status_code=response.status_code,
                headers=self._response_headers(response),
                body=response.content,
            )
        finally:
            await client.aclose()

    async def chat(
        self, payload: dict[str, Any], *, idempotency_key: str | None = None
    ) -> BufferedUpstreamResponse | StreamedUpstreamResponse:
        """Forward a chat request while preserving the upstream wire format."""
        return await self._forward("chat/completions", payload, idempotency_key=idempotency_key)

    async def responses(
        self, payload: dict[str, Any], *, idempotency_key: str | None = None
    ) -> BufferedUpstreamResponse | StreamedUpstreamResponse:
        """Forward a Responses request with route-scoped compatibility adaptation."""

        model = payload.get("model")
        route = self.route_identity(
            model if isinstance(model, str) else "configured-upstream", "openai.responses"
        )
        adapted_payload, compatibility_rule_version = adapt_responses_payload(payload, route)

        return await self._forward(
            "responses",
            adapted_payload,
            idempotency_key=idempotency_key,
            compatibility_rule_version=compatibility_rule_version,
        )

    async def _forward(
        self,
        path: str,
        payload: dict[str, Any],
        *,
        idempotency_key: str | None = None,
        compatibility_rule_version: str | None = None,
    ) -> BufferedUpstreamResponse | StreamedUpstreamResponse:
        client = self._client()
        request = self._build_request(
            "POST",
            path,
            headers={
                "content-type": "application/json",
                **({"idempotency-key": idempotency_key} if idempotency_key else {}),
            },
            json_payload=payload,
        )
        requested_model = payload.get("model") if isinstance(payload.get("model"), str) else None
        protocol = (
            "openai.responses"
            if path.rstrip("/").endswith("responses")
            else "openai.chat.completions"
        )
        if payload.get("stream") is not True:
            try:
                response = await client.send(request)
                return BufferedUpstreamResponse(
                    status_code=response.status_code,
                    headers=self._response_headers(response),
                    body=response.content,
                    actual_route=self._actual_route(
                        response, requested_model=requested_model, protocol=protocol
                    ),
                    compatibility_rule_version=compatibility_rule_version,
                )
            finally:
                await client.aclose()

        response = await client.send(request, stream=True)
        response_headers = self._response_headers(response)
        # Streaming response bodies are not buffered here, so only an adapter
        # attestation header can provide actual-route identity.
        actual_route = self._actual_route(response, requested_model=None, protocol=protocol)
        if response.status_code >= 400:
            try:
                buffered_body = await response.aread()
                return BufferedUpstreamResponse(
                    status_code=response.status_code,
                    headers=response_headers,
                    body=buffered_body,
                    actual_route=actual_route,
                    compatibility_rule_version=compatibility_rule_version,
                )
            finally:
                await response.aclose()
                await client.aclose()

        async def stream_body() -> AsyncIterator[bytes]:
            try:
                async for chunk in response.aiter_raw():
                    yield chunk
            finally:
                await response.aclose()
                await client.aclose()

        return StreamedUpstreamResponse(
            status_code=response.status_code,
            headers=response_headers,
            body=stream_body(),
            actual_route=actual_route,
            compatibility_rule_version=compatibility_rule_version,
        )
