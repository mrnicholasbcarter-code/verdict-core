"""HTTP anti-corruption adapter for doobidoo/mcp-memory-service.

The service may use SQLite-vec and local embeddings internally. Those details do
not cross this boundary, and no generative API is used by any operation here.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast
from urllib.parse import quote

import httpx

from verdict.shared_memory import (
    ENVELOPE_SCHEMA_VERSION,
    SHARED_MEMORY_PROTOCOL_VERSION,
    CertificationState,
    ExternalMemoryRef,
    ProviderErrorCode,
    ProviderHealth,
    ProviderResultStatus,
    SharedMemoryEnvelope,
    SharedMemoryHit,
    SharedMemoryProviderError,
    SharedMemoryQuery,
    SharedMemorySearchResult,
    redact_secrets,
)


class MCPMemoryServiceProvider:
    """Bounded synchronous HTTP adapter for MCP Memory Service."""

    provider_id = "mcp-memory-service"
    protocol_version = SHARED_MEMORY_PROTOCOL_VERSION

    def __init__(
        self,
        endpoint: str,
        *,
        token_env: str = "VERDICT_SHARED_MEMORY_TOKEN",
        env: Mapping[str, str] | None = None,
        timeout: float = 5.0,
        transport: httpx.BaseTransport | None = None,
        verify: bool = True,
    ) -> None:
        import os

        if not endpoint or not endpoint.strip():
            raise SharedMemoryProviderError(
                ProviderErrorCode.NOT_CONFIGURED, "shared memory endpoint is not configured"
            )
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        values = os.environ if env is None else env
        self.endpoint = endpoint.strip().rstrip("/")
        self.token_env = token_env
        self._token = values.get(token_env)
        self.timeout = timeout
        self._transport = transport
        self._verify = verify

    @property
    def redacted_endpoint(self) -> str:
        return redact_secrets(self.endpoint)

    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "verdict-core/shared-memory-v1",
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    def _request(
        self, method: str, path: str, *, payload: Mapping[str, Any] | None = None
    ) -> httpx.Response:
        try:
            with httpx.Client(
                timeout=self.timeout, transport=self._transport, verify=self._verify
            ) as client:
                response = client.request(
                    method, f"{self.endpoint}{path}", headers=self._headers(), json=payload
                )
        except httpx.TimeoutException as exc:
            raise SharedMemoryProviderError(
                ProviderErrorCode.TIMEOUT, "shared memory request timed out"
            ) from exc
        except httpx.RequestError as exc:
            # Do not include request URL or headers: either may contain credentials.
            raise SharedMemoryProviderError(
                ProviderErrorCode.UNREACHABLE, "shared memory service is unreachable"
            ) from exc
        if response.status_code in {401, 403}:
            raise SharedMemoryProviderError(
                ProviderErrorCode.AUTH_FAILED, "shared memory authentication failed"
            )
        if response.status_code == 429:
            raise SharedMemoryProviderError(
                ProviderErrorCode.RATE_LIMITED, "shared memory service rate limited the request"
            )
        if response.status_code in {400, 404, 405, 409, 422}:
            raise SharedMemoryProviderError(
                ProviderErrorCode.INVALID_REQUEST,
                f"shared memory request was rejected (HTTP {response.status_code})",
            )
        if response.status_code >= 500:
            raise SharedMemoryProviderError(
                ProviderErrorCode.SERVER_ERROR,
                f"shared memory service failed (HTTP {response.status_code})",
            )
        if response.status_code >= 300:
            raise SharedMemoryProviderError(
                ProviderErrorCode.UNKNOWN,
                f"unexpected shared memory response (HTTP {response.status_code})",
            )
        return response

    @staticmethod
    def _json(response: httpx.Response) -> Mapping[str, Any]:
        try:
            payload = response.json()
        except ValueError as exc:
            raise SharedMemoryProviderError(
                ProviderErrorCode.SCHEMA_INCOMPATIBLE, "shared memory response is not valid JSON"
            ) from exc
        if not isinstance(payload, Mapping):
            raise SharedMemoryProviderError(
                ProviderErrorCode.SCHEMA_INCOMPATIBLE,
                "shared memory response must be a JSON object",
            )
        return cast(Mapping[str, Any], payload)

    @staticmethod
    def _compatible(payload: Mapping[str, Any]) -> None:
        advertised_schema = payload.get("schema_version")
        if advertised_schema is not None and advertised_schema not in {
            ENVELOPE_SCHEMA_VERSION,
            "1",
            1,
        }:
            raise SharedMemoryProviderError(
                ProviderErrorCode.SCHEMA_INCOMPATIBLE,
                f"unsupported provider schema: {advertised_schema}",
            )
        advertised_protocol = payload.get("protocol_version")
        if advertised_protocol is not None and str(advertised_protocol) not in {
            SHARED_MEMORY_PROTOCOL_VERSION,
            "shared-memory/v1",
        }:
            raise SharedMemoryProviderError(
                ProviderErrorCode.PROTOCOL_INCOMPATIBLE,
                f"unsupported provider protocol: {advertised_protocol}",
            )

    def health(self) -> ProviderHealth:
        try:
            payload = self._json(self._request("GET", "/api/health"))
            self._compatible(payload)
        except SharedMemoryProviderError as exc:
            if exc.code == ProviderErrorCode.INVALID_REQUEST:
                try:
                    payload = self._json(self._request("GET", "/health"))
                    self._compatible(payload)
                except SharedMemoryProviderError as fallback_exc:
                    return self._failed_health(fallback_exc)
            else:
                return self._failed_health(exc)
        raw_status = str(payload.get("status", "healthy")).casefold()
        degraded = raw_status in {"degraded", "warning", "partial"}
        unhealthy = raw_status in {"failed", "error", "unavailable", "unhealthy"}
        if unhealthy:
            status = ProviderResultStatus.UNAVAILABLE
            state = CertificationState.UNAVAILABLE
        elif degraded:
            status = ProviderResultStatus.DEGRADED
            state = CertificationState.DEGRADED
        else:
            status = ProviderResultStatus.AVAILABLE
            state = CertificationState.HEALTHY
        backend_raw = payload.get("backend") or payload.get("storage")
        backend = str(backend_raw) if backend_raw is not None else None
        version_raw = payload.get("version") or payload.get("provider_version")
        return ProviderHealth(
            provider_id=self.provider_id,
            status=status,
            state=state,
            protocol_version=str(payload.get("protocol_version", self.protocol_version)),
            provider_version=str(version_raw) if version_raw is not None else None,
            backend=backend,
            auth_state="authenticated" if self._token else "not-configured",
            message=None if status == ProviderResultStatus.AVAILABLE else "provider is degraded",
        )

    def _failed_health(self, exc: SharedMemoryProviderError) -> ProviderHealth:
        incompatible = exc.code in {
            ProviderErrorCode.SCHEMA_INCOMPATIBLE,
            ProviderErrorCode.PROTOCOL_INCOMPATIBLE,
        }
        status = (
            ProviderResultStatus.INCOMPATIBLE
            if incompatible
            else ProviderResultStatus.TIMEOUT
            if exc.code == ProviderErrorCode.TIMEOUT
            else ProviderResultStatus.AUTH_FAILED
            if exc.code == ProviderErrorCode.AUTH_FAILED
            else ProviderResultStatus.UNAVAILABLE
        )
        return ProviderHealth(
            self.provider_id,
            status,
            CertificationState.INCOMPATIBLE if incompatible else CertificationState.UNAVAILABLE,
            auth_state="failed"
            if exc.code == ProviderErrorCode.AUTH_FAILED
            else "configured"
            if self._token
            else "not-configured",
            message=exc.safe_message,
        )

    def put(self, envelope: SharedMemoryEnvelope) -> ExternalMemoryRef:
        payload = self._json(self._request("POST", "/api/memories", payload=envelope.to_dict()))
        self._compatible(payload)
        external_id = payload.get("id") or payload.get("memory_id") or payload.get("external_id")
        if external_id is None and isinstance(payload.get("data"), Mapping):
            data = cast(Mapping[str, Any], payload["data"])
            external_id = data.get("id") or data.get("memory_id")
        if not isinstance(external_id, (str, int)) or not str(external_id):
            raise SharedMemoryProviderError(
                ProviderErrorCode.SCHEMA_INCOMPATIBLE, "store response is missing a memory id"
            )
        revision = payload.get("revision", envelope.revision)
        return ExternalMemoryRef(self.provider_id, str(external_id), str(revision))

    def search(self, query: SharedMemoryQuery) -> SharedMemorySearchResult:
        request_payload = {
            "query": query.query,
            "project": query.project,
            "scope": query.scope,
            "tenant": query.tenant,
            "limit": query.limit,
            "filters": {"project": query.project, "scope": query.scope, "tenant": query.tenant},
        }
        try:
            payload = self._json(
                self._request("POST", "/api/memories/search", payload=request_payload)
            )
            self._compatible(payload)
            raw_hits: Any = payload.get("hits", payload.get("results", payload.get("memories", [])))
            if isinstance(raw_hits, Mapping):
                raw_hits = raw_hits.get("items", raw_hits.get("data", []))
            if not isinstance(raw_hits, list):
                raise SharedMemoryProviderError(
                    ProviderErrorCode.SCHEMA_INCOMPATIBLE,
                    "search response is missing a result list",
                )
            hits: list[SharedMemoryHit] = []
            for raw in raw_hits:
                hit = self._decode_hit(raw)
                # Defense in depth: a server must not broaden caller filters.
                if (
                    hit.envelope.project == query.project
                    and hit.envelope.scope == query.scope
                    and hit.envelope.tenant == query.tenant
                ):
                    hits.append(hit)
                if len(hits) >= query.limit:
                    break
            return SharedMemorySearchResult(ProviderResultStatus.AVAILABLE, tuple(hits))
        except SharedMemoryProviderError as exc:
            status = {
                ProviderErrorCode.TIMEOUT: ProviderResultStatus.TIMEOUT,
                ProviderErrorCode.AUTH_FAILED: ProviderResultStatus.AUTH_FAILED,
                ProviderErrorCode.SCHEMA_INCOMPATIBLE: ProviderResultStatus.INCOMPATIBLE,
                ProviderErrorCode.PROTOCOL_INCOMPATIBLE: ProviderResultStatus.INCOMPATIBLE,
            }.get(exc.code, ProviderResultStatus.UNAVAILABLE)
            return SharedMemorySearchResult(status, error_code=exc.code, message=exc.safe_message)

    def _decode_hit(self, raw: Any) -> SharedMemoryHit:
        if not isinstance(raw, Mapping):
            raise SharedMemoryProviderError(
                ProviderErrorCode.SCHEMA_INCOMPATIBLE, "search result entry must be an object"
            )
        envelope_raw: Any = raw.get("envelope", raw.get("memory", raw))
        if not isinstance(envelope_raw, Mapping):
            raise SharedMemoryProviderError(
                ProviderErrorCode.SCHEMA_INCOMPATIBLE, "search result envelope is malformed"
            )
        try:
            envelope = SharedMemoryEnvelope.from_dict(envelope_raw)
        except SharedMemoryProviderError:
            raise
        except (TypeError, ValueError) as exc:
            raise SharedMemoryProviderError(
                ProviderErrorCode.SCHEMA_INCOMPATIBLE, "search result envelope is incompatible"
            ) from exc
        external_id = raw.get("id") or raw.get("memory_id") or raw.get("external_id")
        if external_id is None:
            external_id = envelope.idempotency_key
        score_raw = raw.get("score", raw.get("similarity", 0.0))
        try:
            score = float(score_raw)
        except (TypeError, ValueError):
            score = 0.0
        return SharedMemoryHit(
            ExternalMemoryRef(
                self.provider_id, str(external_id), str(raw.get("revision", envelope.revision))
            ),
            envelope,
            score,
        )

    def delete(self, ref: ExternalMemoryRef) -> bool:
        if ref.provider_id != self.provider_id:
            return False
        self._request("DELETE", f"/api/memories/{quote(ref.external_id, safe='')}")
        return True


__all__ = ["MCPMemoryServiceProvider"]
