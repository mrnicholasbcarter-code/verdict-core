"""Authenticated ai-memory (2.6.x) backend client.

Token is loaded from a configured file path inside the calling process only.
Never print tokens; never put them in controller env from this package.

Transport: HTTP JSON-RPC to ``{endpoint}/mcp`` with
``Accept: application/json, text/event-stream`` (required by ai-memory 2.6.6).
Static MCP clients MUST pass workspace+project on every project-scoped call.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

import httpx

from verdict_memory.contracts import (
    SHARED_MEMORY_PROTOCOL_VERSION,
    CertificationState,
    EventIdentity,
    ExternalMemoryRef,
    LifecycleEvent,
    MemoryHit,
    MemoryProviderError,
    ProviderErrorCode,
    ProviderHealth,
    ProviderResultStatus,
    RecallQuery,
    RecallResult,
    redact_secrets,
)

DEFAULT_ENDPOINT = "http://127.0.0.1:49374"
DEFAULT_TOKEN_FILE = Path.home() / ".config" / "ai-memory" / "auth-token"
DEFAULT_TIMEOUT = 5.0
MCP_PROTOCOL_VERSION = "2024-11-05"


class AiMemoryClientConfig:
    def __init__(
        self,
        *,
        endpoint: str = DEFAULT_ENDPOINT,
        token_file: str | Path | None = DEFAULT_TOKEN_FILE,
        token: str | None = None,
        workspace: str = "default",
        project: str,
        timeout: float = DEFAULT_TIMEOUT,
        transport: httpx.BaseTransport | None = None,
        verify: bool = True,
    ) -> None:
        if not project.strip():
            raise ValueError("project is required")
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self.endpoint = endpoint.strip().rstrip("/")
        # Strip trailing /mcp so we can append it once.
        if self.endpoint.endswith("/mcp"):
            self.endpoint = self.endpoint[: -len("/mcp")]
        self.token_file = Path(token_file) if token_file else None
        self._token_override = token
        self.workspace = workspace
        self.project = project
        self.timeout = timeout
        self.transport = transport
        self.verify = verify

    def load_token(self) -> str | None:
        if self._token_override is not None:
            return self._token_override
        if self.token_file is None:
            return None
        if not self.token_file.is_file():
            return None
        return self.token_file.read_text("utf-8").strip() or None


class AiMemoryClient:
    """Authenticated MemoryProvider for existing ai-memory. No LLM enrichment."""

    provider_id = "ai-memory"
    protocol_version = SHARED_MEMORY_PROTOCOL_VERSION

    def __init__(self, config: AiMemoryClientConfig) -> None:
        self.config = config
        self._rpc_id = 0
        self._initialized = False
        # Last typed failure for diagnostics (never healthy-empty swallow).
        # Shape: {op, code, message, endpoint_redacted, project, workspace} | None
        self.last_error: dict[str, str | None] | None = None

    def _next_id(self) -> int:
        self._rpc_id += 1
        return self._rpc_id

    def _record_error(self, op: str, exc: MemoryProviderError) -> None:
        self.last_error = {
            "op": op,
            "code": exc.code.value,
            "message": exc.safe_message,
            "endpoint_redacted": redact_secrets(self.config.endpoint),
            "project": self.config.project,
            "workspace": self.config.workspace,
        }

    def clear_error(self) -> None:
        self.last_error = None

    def _headers(self, token: str | None) -> dict[str, str]:
        headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "User-Agent": "verdict-memory/0.1.0a1",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    def _client(self) -> httpx.Client:
        return httpx.Client(
            timeout=self.config.timeout,
            transport=self.config.transport,
            verify=self.config.verify,
        )

    def _raise_http(self, response: httpx.Response) -> None:
        if response.status_code in {401, 403}:
            raise MemoryProviderError(ProviderErrorCode.AUTH_FAILED, "ai-memory authentication failed")
        if response.status_code == 429:
            raise MemoryProviderError(ProviderErrorCode.RATE_LIMITED, "ai-memory rate limited the request")
        if response.status_code >= 500:
            raise MemoryProviderError(ProviderErrorCode.SERVER_ERROR, "ai-memory server error")
        if response.status_code >= 400:
            raise MemoryProviderError(
                ProviderErrorCode.INVALID_REQUEST,
                f"ai-memory rejected request ({response.status_code})",
            )

    def _mcp(self, method: str, params: dict[str, Any] | None = None, *, notify: bool = False) -> Any:
        token = self.config.load_token()
        if not token and method != "initialize":
            # Still allow initialize for capability probing, but tool calls need auth.
            raise MemoryProviderError(ProviderErrorCode.AUTH_FAILED, "auth token missing")
        headers = self._headers(token)
        payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if not notify:
            payload["id"] = self._next_id()
        if params is not None:
            payload["params"] = params
        try:
            with self._client() as client:
                if method != "initialize" and not self._initialized:
                    init = client.post(
                        f"{self.config.endpoint}/mcp",
                        headers=headers,
                        json={
                            "jsonrpc": "2.0",
                            "id": self._next_id(),
                            "method": "initialize",
                            "params": {
                                "protocolVersion": MCP_PROTOCOL_VERSION,
                                "capabilities": {},
                                "clientInfo": {"name": "verdict-memory", "version": "0.1.0a1"},
                            },
                        },
                    )
                    self._raise_http(init)
                    client.post(
                        f"{self.config.endpoint}/mcp",
                        headers=headers,
                        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
                    )
                    self._initialized = True
                response = client.post(
                    f"{self.config.endpoint}/mcp", headers=headers, json=payload
                )
        except httpx.TimeoutException as exc:
            err = MemoryProviderError(ProviderErrorCode.TIMEOUT, "ai-memory request timed out")
            self._record_error(method, err)
            raise err from exc
        except httpx.RequestError as exc:
            err = MemoryProviderError(
                ProviderErrorCode.UNREACHABLE, "ai-memory service is unreachable"
            )
            self._record_error(method, err)
            raise err from exc
        if notify:
            return None
        self._raise_http(response)
        try:
            data = response.json()
        except ValueError as exc:
            raise MemoryProviderError(ProviderErrorCode.MALFORMED, "ai-memory returned non-JSON") from exc
        if "error" in data:
            err = data["error"]
            msg = redact_secrets(str(err.get("message", err))) or "mcp error"
            exc = MemoryProviderError(ProviderErrorCode.SERVER_ERROR, msg)
            self._record_error(method, exc)
            raise exc
        self.last_error = None
        return data.get("result")

    def _tool(self, name: str, arguments: dict[str, Any]) -> Any:
        result = self._mcp("tools/call", {"name": name, "arguments": arguments})
        # MCP tool result: {content:[{type:text,text:"..."}], isError?: bool}
        if not isinstance(result, dict):
            return result
        if result.get("isError"):
            text = ""
            for item in result.get("content") or []:
                if isinstance(item, dict) and item.get("type") == "text":
                    text = str(item.get("text", ""))
                    break
            raise MemoryProviderError(
                ProviderErrorCode.SERVER_ERROR, redact_secrets(text) or "tool error"
            )
        content_items = result.get("content") or []
        if not content_items:
            return result
        text = content_items[0].get("text", "") if isinstance(content_items[0], dict) else ""
        if not text:
            return result
        try:
            return json.loads(text)
        except ValueError:
            return {"text": text}

    def health(self) -> ProviderHealth:
        token = self.config.load_token()
        if not self.config.endpoint:
            raise MemoryProviderError(ProviderErrorCode.NOT_CONFIGURED, "endpoint missing")
        if not token:
            return ProviderHealth(
                self.provider_id,
                ProviderResultStatus.AUTH_FAILED,
                CertificationState.CONFIGURED,
                self.protocol_version,
                None,
                "ai-memory",
                "not-configured",
                "auth token missing",
                capability="partial",
            )
        try:
            with self._client() as client:
                response = client.get(
                    f"{self.config.endpoint}/api/v1/workspaces",
                    headers=self._headers(token),
                )
            self._raise_http(response)
            data = response.json()
            if not isinstance(data, list):
                return ProviderHealth(
                    self.provider_id,
                    ProviderResultStatus.DEGRADED,
                    CertificationState.INCOMPATIBLE,
                    self.protocol_version,
                    None,
                    "ai-memory",
                    "authenticated",
                    "unexpected /api/v1/workspaces schema",
                    capability="partial",
                )
            # Prefer admin status for version when authorized
            version = None
            try:
                with self._client() as client:
                    st = client.get(
                        f"{self.config.endpoint}/admin/status",
                        headers=self._headers(token),
                    )
                if st.status_code == 200:
                    version = st.json().get("version")
            except Exception:
                version = None
            return ProviderHealth(
                self.provider_id,
                ProviderResultStatus.AVAILABLE,
                CertificationState.HEALTHY,
                self.protocol_version,
                version,
                "ai-memory",
                "authenticated",
                None,
                capability="supported",
            )
        except MemoryProviderError as exc:
            self._record_error("health", exc)
            status_map = {
                ProviderErrorCode.TIMEOUT: ProviderResultStatus.TIMEOUT,
                ProviderErrorCode.UNREACHABLE: ProviderResultStatus.UNAVAILABLE,
                ProviderErrorCode.AUTH_FAILED: ProviderResultStatus.AUTH_FAILED,
            }
            return ProviderHealth(
                self.provider_id,
                status_map.get(exc.code, ProviderResultStatus.UNAVAILABLE),
                CertificationState.UNAVAILABLE,
                self.protocol_version,
                None,
                "ai-memory",
                "failed",
                exc.safe_message,
                capability="partial",
            )

    def _page_path(self, event: LifecycleEvent) -> str:
        # Stable path from logical identity — not adapter timestamp.
        key = event.identity.source_event_id or event.idempotency_key
        kind = event.memory_kind or "memory"
        return f"verdict-memory/{kind}/{key[:24]}.md"

    def put(self, event: LifecycleEvent) -> ExternalMemoryRef:
        # Auth/config authority: never trust event.identity.project/workspace to choose
        # the remote scope. Provenance may record them; writes always use client config.
        if event.identity.project and event.identity.project != self.config.project:
            raise MemoryProviderError(
                ProviderErrorCode.INVALID_REQUEST,
                "event project does not match authenticated client config project",
            )
        if event.identity.workspace and event.identity.workspace != self.config.workspace:
            raise MemoryProviderError(
                ProviderErrorCode.INVALID_REQUEST,
                "event workspace does not match authenticated client config workspace",
            )
        # Redact before network write
        body = redact_secrets(event.content) or ""
        if not body.lstrip().startswith("#"):
            title = f"{event.kind} {event.identity.session_id}"
            body = f"# {title}\n\n{body}"
        args: dict[str, Any] = {
            "path": self._page_path(event),
            "body": body,
            "workspace": self.config.workspace,
            "project": self.config.project,
            "kind": event.memory_kind,
        }
        # Static MCP clients must not invent session_id: ai-memory rejects UUIDs that
        # are not sessions of the target project. Omit unless caller sets
        # metadata["ai_memory_session_id"] to a verified session UUID.
        verified = (event.metadata or {}).get("ai_memory_session_id")
        if isinstance(verified, str) and re.fullmatch(
            r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}",
            verified,
        ):
            args["session_id"] = verified
        page = self._tool("memory_write_page", args)
        path = None
        if isinstance(page, dict):
            path = page.get("path") or page.get("page_path")
        return ExternalMemoryRef(
            self.provider_id,
            str(path or args["path"]),
            event.revision,
        )

    def search(self, query: RecallQuery) -> RecallResult:
        try:
            data = self._tool(
                "memory_query",
                {
                    "query": query.query,
                    "workspace": query.workspace or self.config.workspace,
                    "project": query.project or self.config.project,
                    "limit": query.limit,
                    "answer": False,
                },
            )
        except MemoryProviderError as exc:
            self._record_error("memory_query", exc)
            status_map = {
                ProviderErrorCode.TIMEOUT: ProviderResultStatus.TIMEOUT,
                ProviderErrorCode.UNREACHABLE: ProviderResultStatus.UNAVAILABLE,
                ProviderErrorCode.AUTH_FAILED: ProviderResultStatus.AUTH_FAILED,
            }
            return RecallResult(
                status_map.get(exc.code, ProviderResultStatus.UNAVAILABLE),
                error_code=exc.code,
                message=exc.safe_message,
                capability="partial",
            )
        hits_raw = []
        if isinstance(data, dict):
            hits_raw = data.get("hits") or data.get("results") or []
        hits: list[MemoryHit] = []
        wanted_project = query.project or self.config.project
        wanted_workspace = query.workspace or self.config.workspace
        for page in hits_raw:
            if not isinstance(page, dict):
                continue
            # Fail-closed client-side scope filter: ai-memory may return cross-project
            # FTS hits; never present them as in-scope for a different project.
            page_project = page.get("project") or page.get("project_name")
            page_workspace = page.get("workspace") or page.get("workspace_name")
            if page_project is not None and page_project != wanted_project:
                continue
            if page_workspace is not None and page_workspace != wanted_workspace:
                continue
            if len(hits) >= query.limit:
                break
            path = str(page.get("path") or page.get("id") or "")
            snippet = page.get("body") or page.get("snippet") or page.get("title") or ""
            if isinstance(snippet, str):
                snippet = re.sub(r"</?mark>", "", snippet)
            content = snippet or "(empty)"
            event = LifecycleEvent(
                kind="manual_capture",
                content=content,
                identity=EventIdentity(
                    source_harness="ai-memory",
                    project=wanted_project,
                    session_id=query.session_id or "unknown",
                    tenant=query.tenant,
                    workspace=query.workspace,
                ),
                created_at=0.0,
                observed_at=time.time(),
                scope=query.scope,
                trust="remote-advisory",
                metadata={
                    "ai_memory_path": path,
                    "ai_memory_title": page.get("title"),
                    "ai_memory_id": page.get("id"),
                },
                provenance={"provider": self.provider_id, "path": path},
            )
            score = float(page.get("rank") or page.get("score") or 0.0)
            hits.append(
                MemoryHit(
                    ExternalMemoryRef(self.provider_id, path or event.idempotency_key),
                    event,
                    score,
                    untrusted=True,
                )
            )
        return RecallResult(ProviderResultStatus.AVAILABLE, tuple(hits), capability="supported")

    def delete(self, ref: ExternalMemoryRef) -> bool:
        if ref.provider_id != self.provider_id:
            return False
        try:
            self._tool(
                "memory_delete_page",
                {
                    "path": ref.external_id,
                    "workspace": self.config.workspace,
                    "project": self.config.project,
                },
            )
            return True
        except MemoryProviderError:
            return False


__all__ = [
    "AiMemoryClient",
    "AiMemoryClientConfig",
    "DEFAULT_ENDPOINT",
    "DEFAULT_TIMEOUT",
    "DEFAULT_TOKEN_FILE",
    "MCP_PROTOCOL_VERSION",
]
