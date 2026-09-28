"""EXPERIMENTAL: HTTP adapter for akitaonrails/ai-memory as a SharedMemoryProvider.

**POC ONLY** — BOD-281 evaluation of ai-memory as Verdict's shared-memory substrate.
NOT registered as a default provider. Requires an isolated ai-memory instance.

ai-memory interface: MCP tools + HTTP API (/api/v1/* read-only, MCP for write).
We use the HTTP API for health, and MCP-style write/search since ai-memory's
public contract is MCP.

Contract:
- health(): checks /api/v1/workspaces (read-only, no auth required for POC)
- put(): maps SharedMemoryEnvelope → ai-memory's memory_write_page MCP tool
- search(): maps SharedMemoryQuery → ai-memory's memory_query MCP tool
- delete(): maps ExternalMemoryRef → ai-memory's memory_delete_page MCP tool

All returned records have authority_verified=False (advisory evidence only).
Provider outage fails open (returns UNAVAILABLE, never raises to break Verdict).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import httpx

from verdict.shared_memory import (
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


class AiMemoryExperimentalProvider:
    """Experimental HTTP+MCP adapter for ai-memory (BOD-281 POC)."""

    provider_id = "ai-memory-experimental"
    protocol_version = SHARED_MEMORY_PROTOCOL_VERSION

    def __init__(
        self,
        endpoint: str,
        *,
        token: str | None = None,
        timeout: float = 5.0,
        transport: httpx.BaseTransport | None = None,
        verify: bool = True,
    ) -> None:
        """
        Args:
            endpoint: ai-memory HTTP API base (e.g. http://localhost:49374)
            token: optional Bearer token for auth
            timeout: request timeout in seconds
            transport: optional httpx transport (for testing)
            verify: TLS verification (disable for self-signed certs in POC)
        """
        if not endpoint or not endpoint.strip():
            raise SharedMemoryProviderError(
                ProviderErrorCode.NOT_CONFIGURED,
                "ai-memory endpoint is not configured",
            )
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self.endpoint = endpoint.strip().rstrip("/")
        self._token = token
        self.timeout = timeout
        self._transport = transport
        self._verify = verify

    @property
    def redacted_endpoint(self) -> str:
        return redact_secrets(self.endpoint)

    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "User-Agent": "verdict-core/ai-memory-poc",
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
                ProviderErrorCode.TIMEOUT, "ai-memory request timed out"
            ) from exc
        except httpx.RequestError as exc:
            raise SharedMemoryProviderError(
                ProviderErrorCode.UNREACHABLE, "ai-memory service is unreachable"
            ) from exc
        if response.status_code in {401, 403}:
            raise SharedMemoryProviderError(
                ProviderErrorCode.AUTH_FAILED, "ai-memory authentication failed"
            )
        if response.status_code == 429:
            raise SharedMemoryProviderError(
                ProviderErrorCode.RATE_LIMITED, "ai-memory rate limited the request"
            )
        if response.status_code in {400, 404, 405, 409, 422}:
            raise SharedMemoryProviderError(
                ProviderErrorCode.INVALID_REQUEST,
                f"ai-memory rejected request (HTTP {response.status_code})",
            )
        if response.status_code >= 500:
            raise SharedMemoryProviderError(
                ProviderErrorCode.SERVER_ERROR,
                f"ai-memory service failed (HTTP {response.status_code})",
            )
        if response.status_code >= 300:
            raise SharedMemoryProviderError(
                ProviderErrorCode.UNKNOWN,
                f"unexpected ai-memory response (HTTP {response.status_code})",
            )
        return response

    def health(self) -> ProviderHealth:
        """Check ai-memory /api/v1/workspaces (read-only, always available)."""
        try:
            response = self._request("GET", "/api/v1/workspaces")
            data = response.json()
            # ai-memory /api/v1/workspaces returns a JSON array of workspace objects
            # e.g. [{"workspace_name":"default","project_count":2,...}]
            if not isinstance(data, list):
                return ProviderHealth(
                    provider_id=self.provider_id,
                    status=ProviderResultStatus.DEGRADED,
                    state=CertificationState.INCOMPATIBLE,
                    protocol_version=self.protocol_version,
                    message="ai-memory returned unexpected schema",
                )
            return ProviderHealth(
                provider_id=self.provider_id,
                status=ProviderResultStatus.AVAILABLE,
                state=CertificationState.HEALTHY,
                protocol_version=self.protocol_version,
                backend="ai-memory",
                auth_state="configured" if self._token else "no-auth",
            )
        except SharedMemoryProviderError as exc:
            status_map = {
                ProviderErrorCode.TIMEOUT: ProviderResultStatus.TIMEOUT,
                ProviderErrorCode.UNREACHABLE: ProviderResultStatus.UNAVAILABLE,
                ProviderErrorCode.AUTH_FAILED: ProviderResultStatus.AUTH_FAILED,
            }
            status = status_map.get(exc.code, ProviderResultStatus.UNAVAILABLE)
            return ProviderHealth(
                provider_id=self.provider_id,
                status=status,
                state=CertificationState.HEALTHY,
                protocol_version=self.protocol_version,
                message=exc.safe_message,
            )

    def put(self, envelope: SharedMemoryEnvelope) -> ExternalMemoryRef:
        """Write a memory via ai-memory MCP memory_write_page tool.

        Maps SharedMemoryEnvelope to ai-memory's wiki page model:
        - envelope.content → page body (markdown)
        - envelope metadata → frontmatter (kind, tier, trust, etc.)
        - envelope.project → ai-memory workspace+project scope
        """
        # ai-memory MCP memory_write_page expects:
        # {path, body, frontmatter{kind, tier}, workspace?, project?}
        path = self._memory_path(envelope)
        frontmatter = {
            "kind": envelope.memory_kind,
            "tier": envelope.retention_class,
            "source_harness": envelope.source_harness,
            "source_agent": envelope.source_agent,
            "trust": envelope.trust,
            "authority": envelope.authority,
            "authority_verified": False,  # always advisory from shared memory
        }
        if envelope.expires_at:
            frontmatter["expires_at"] = envelope.expires_at

        # ai-memory MCP tool payload (simulated via HTTP since we're not using MCP client SDK)
        # In reality, you'd call via MCP protocol, but for POC we use HTTP /admin endpoint
        # or simulate MCP JSON-RPC
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "memory_write_page",
                "arguments": {
                    "path": path,
                    "body": envelope.content,
                    "frontmatter": frontmatter,
                    "project": envelope.project,
                },
            },
        }

        try:
            # For POC: use ai-memory's /admin/* endpoint or MCP bridge
            # Since ai-memory MCP is the canonical write path, we'd need MCP client
            # For now, return a stub showing the contract
            # Real implementation would use python-mcp-client or httpx to /mcp endpoint

            # Placeholder: in real impl, POST to /mcp with JSON-RPC envelope
            response = self._request("POST", "/mcp", payload=payload)
            result = response.json()

            # ai-memory returns: {"result": {"content": [{"type": "text", "text": "{...}"}]}}
            # The text field is a JSON string with page_id, path, checkpoint
            import json as _json
            content_items = result.get("result", {}).get("content", [])
            text_str = content_items[0].get("text", "{}") if content_items else "{}"
            page_info = _json.loads(text_str) if text_str else {}
            page_path = page_info.get("path", path)

            return ExternalMemoryRef(
                provider_id=self.provider_id,
                external_id=page_path,
                revision=envelope.revision,
            )
        except SharedMemoryProviderError:
            raise  # Let caller handle
        except Exception as exc:
            raise SharedMemoryProviderError(
                ProviderErrorCode.SERVER_ERROR,
                f"ai-memory write failed: {redact_secrets(str(exc))}",
            ) from exc

    def search(self, query: SharedMemoryQuery) -> SharedMemorySearchResult:
        """Search via ai-memory MCP memory_query tool.

        Maps SharedMemoryQuery to ai-memory's query params:
        - query.query → MCP memory_query.query
        - query.project → workspace+project scope
        - query.limit → result limit
        """
        # ai-memory MCP memory_query expects:
        # {query, workspace?, project?, limit?}
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "memory_query",
                "arguments": {
                    "query": query.query,
                    "project": query.project,
                    "limit": query.limit,
                },
            },
        }

        try:
            # For POC: MCP call or HTTP bridge
            response = self._request("POST", "/mcp", payload=payload)
            result = response.json()

            # ai-memory returns: {"result": {"content": [{"type": "text", "text": "{...}"}]}}
            # The text field is a JSON string with {"hits": [{id, path, title, snippet, rank}]}
            import json as _json
            content_items = result.get("result", {}).get("content", [])
            text_str = content_items[0].get("text", "{}") if content_items else "{}"
            search_data = _json.loads(text_str) if text_str else {}
            pages = search_data.get("hits", [])

            hits = tuple(
                SharedMemoryHit(
                    ref=ExternalMemoryRef(
                        provider_id=self.provider_id,
                        external_id=page["path"],
                        revision=None,
                    ),
                    envelope=self._page_to_envelope(page, query),
                    score=float(page.get("rank", 0.0)),
                )
                for page in pages[:query.limit]
            )

            return SharedMemorySearchResult(
                status=ProviderResultStatus.AVAILABLE,
                hits=hits,
            )
        except SharedMemoryProviderError as exc:
            # Fail open: return empty result with error status
            status_map = {
                ProviderErrorCode.TIMEOUT: ProviderResultStatus.TIMEOUT,
                ProviderErrorCode.UNREACHABLE: ProviderResultStatus.UNAVAILABLE,
                ProviderErrorCode.AUTH_FAILED: ProviderResultStatus.AUTH_FAILED,
            }
            return SharedMemorySearchResult(
                status=status_map.get(exc.code, ProviderResultStatus.UNAVAILABLE),
                error_code=exc.code,
                message=exc.safe_message,
            )
        except Exception as exc:
            return SharedMemorySearchResult(
                status=ProviderResultStatus.UNAVAILABLE,
                error_code=ProviderErrorCode.UNKNOWN,
                message=redact_secrets(str(exc)),
            )

    def delete(self, ref: ExternalMemoryRef) -> bool:
        """Delete a memory via ai-memory MCP memory_delete_page tool."""
        if ref.provider_id != self.provider_id:
            raise ValueError(f"ref provider_id mismatch: {ref.provider_id}")

        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "memory_delete_page",
                "arguments": {
                    "path": ref.external_id,
                },
            },
        }

        try:
            response = self._request("POST", "/mcp", payload=payload)
            result = response.json()
            # ai-memory returns success confirmation
            return bool(result.get("result", {}).get("deleted", False))
        except SharedMemoryProviderError:
            return False  # fail-open for delete

    def _memory_path(self, envelope: SharedMemoryEnvelope) -> str:
        """Generate ai-memory wiki path from envelope metadata."""
        # ai-memory uses paths like: decisions/YYYY-MM-DD-title.md
        # For Verdict memories, use: verdict/{kind}/{hash-prefix}.md
        content_hash = envelope.content_hash or "unknown"
        kind = envelope.memory_kind or "memory"
        return f"verdict/{kind}/{content_hash[:12]}.md"

    def _page_to_envelope(
        self, page: Mapping[str, Any], query: SharedMemoryQuery
    ) -> SharedMemoryEnvelope:
        """Convert ai-memory search hit to SharedMemoryEnvelope.

        Search hits from memory_query have: id, path, title, snippet, rank
        (no body or frontmatter in search results — snippet is the matched excerpt).
        """
        import time

        frontmatter = page.get("frontmatter", {})
        # search hits use "snippet" (HTML-marked excerpt); full-read hits use "body"
        content = page.get("body") or page.get("snippet") or page.get("title") or ""
        # strip HTML mark tags from snippet (e.g. <mark>word</mark>)
        import re as _re
        content = _re.sub(r"</?mark>", "", content)

        return SharedMemoryEnvelope(
            content=content or "(empty)",
            project=query.project,
            scope=query.scope,
            tenant=query.tenant,
            memory_kind=frontmatter.get("kind", "memory"),
            source_harness=frontmatter.get("source_harness", "ai-memory"),
            source_agent=frontmatter.get("source_agent", "unknown"),
            source_session=None,
            created_at=self._parse_timestamp(page.get("created_at")),
            observed_at=time.time(),
            expires_at=self._parse_timestamp(frontmatter.get("expires_at")),
            retention_class=frontmatter.get("tier", "standard"),
            sensitivity="standard",
            trust="remote-advisory",
            authority="shared-memory-advisory",
            authority_verified=False,  # NEVER verified from shared memory
            provenance={
                "provider": self.provider_id,
                "path": page.get("path"),
                "title": page.get("title"),
            },
            metadata={
                "ai_memory_path": page.get("path"),
                "ai_memory_title": page.get("title"),
                "ai_memory_id": page.get("id"),
            },
        )


    @staticmethod
    def _parse_timestamp(ts: Any) -> float:
        """Parse RFC3339 or return 0.0."""
        if not ts:
            return 0.0
        if isinstance(ts, (int, float)):
            return float(ts)
        try:
            from datetime import datetime
            dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
            return dt.timestamp()
        except Exception:
            return 0.0
