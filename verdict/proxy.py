"""OpenAI-compatible upstream transport for the HTTP proxy.

The transport is intentionally protocol-oriented: it does not deserialize chat
responses or discard fields, so upstream usage, tool calls, errors, and future
response extensions can pass through unchanged.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import httpx

from verdict.security import host_is_allowed, validate_upstream_url

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


@dataclass(frozen=True)
class BufferedUpstreamResponse:
    """A fully buffered upstream response suitable for Starlette ``Response``."""

    status_code: int
    headers: list[tuple[str, str]]
    body: bytes


@dataclass(frozen=True)
class StreamedUpstreamResponse:
    """An upstream response whose body remains an arbitrary byte stream."""

    status_code: int
    headers: list[tuple[str, str]]
    body: AsyncIterator[bytes]


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

    @staticmethod
    def _response_headers(response: httpx.Response) -> list[tuple[str, str]]:
        return [
            (name, value)
            for name, value in response.headers.multi_items()
            if name.lower() not in _HOP_BY_HOP_HEADERS
        ]

    async def models(self) -> BufferedUpstreamResponse:
        """Fetch the configured upstream model catalog without reshaping it."""
        client = self._client()
        try:
            self._validate_destination()
            response = await client.get(self._url("models"), headers=self._headers())
            return BufferedUpstreamResponse(
                status_code=response.status_code,
                headers=self._response_headers(response),
                body=response.content,
            )
        finally:
            await client.aclose()

    async def chat(
        self, payload: dict[str, Any]
    ) -> BufferedUpstreamResponse | StreamedUpstreamResponse:
        """Forward a chat request while preserving the upstream wire format."""
        client = self._client()
        self._validate_destination()
        request = client.build_request(
            "POST",
            self._url("chat/completions"),
            headers={**self._headers(), "content-type": "application/json"},
            json=payload,
        )

        if payload.get("stream") is not True:
            try:
                response = await client.send(request)
                return BufferedUpstreamResponse(
                    status_code=response.status_code,
                    headers=self._response_headers(response),
                    body=response.content,
                )
            finally:
                await client.aclose()

        response = await client.send(request, stream=True)
        response_headers = self._response_headers(response)
        if response.status_code >= 400:
            try:
                buffered_body = await response.aread()
                return BufferedUpstreamResponse(
                    status_code=response.status_code, headers=response_headers, body=buffered_body
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
            status_code=response.status_code, headers=response_headers, body=stream_body()
        )
