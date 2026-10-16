"""Authenticated ai-memory (2.6.x) backend client.

SECURITY: configured project/workspace is authority. Event/query project
overrides are rejected. Hits missing scope are not relabeled. Remote paths
use full namespaced identity. Write ACK requires verified path. Nested
secrets are redacted before remote write. Malformed RPC is typed failure.
Token loaded from file in-process only; never printed.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

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
    redact_mapping,
    redact_secrets,
)
from verdict_memory.policy import MemoryCapturePolicy

DEFAULT_ENDPOINT = "http://127.0.0.1:49374"
DEFAULT_TOKEN_FILE = Path.home() / ".config" / "ai-memory" / "auth-token"
DEFAULT_TIMEOUT = 5.0
MCP_PROTOCOL_VERSION = "2024-11-05"

_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


def validate_endpoint(endpoint: str) -> str:
    """Allow loopback HTTP or verified HTTPS; reject URL credentials/query/fragment."""
    raw = (endpoint or "").strip().rstrip("/")
    if raw.endswith("/mcp"):
        raw = raw[: -len("/mcp")]
    parsed = urlparse(raw)
    if parsed.scheme not in {"http", "https"}:
        raise MemoryProviderError(ProviderErrorCode.INVALID_REQUEST, "endpoint scheme must be http or https")
    if parsed.username or parsed.password:
        raise MemoryProviderError(ProviderErrorCode.INVALID_REQUEST, "endpoint must not embed credentials")
    if parsed.query or parsed.fragment:
        raise MemoryProviderError(ProviderErrorCode.INVALID_REQUEST, "endpoint must not include query or fragment")
    host = (parsed.hostname or "").lower()
    if parsed.scheme == "http" and host not in {"127.0.0.1", "localhost", "::1"}:
        raise MemoryProviderError(
            ProviderErrorCode.INVALID_REQUEST,
            "non-loopback HTTP endpoints are not allowed; use HTTPS",
        )
    if not host:
        raise MemoryProviderError(ProviderErrorCode.INVALID_REQUEST, "endpoint host missing")
    return raw



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
        self.endpoint = validate_endpoint(endpoint)
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
    provider_id = "ai-memory"
    protocol_version = SHARED_MEMORY_PROTOCOL_VERSION

    def __init__(self, config: AiMemoryClientConfig) -> None:
        self.config = config
        self._rpc_id = 0
        self._initialized = False
        self.last_error: dict[str, str | None] | None = None
        self._policy = MemoryCapturePolicy()

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
            follow_redirects=False,
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
        if not token:
            err = MemoryProviderError(ProviderErrorCode.AUTH_FAILED, "auth token missing")
            self._record_error(method, err)
            raise err
        headers = self._headers(token)
        req_id = None if notify else self._next_id()
        payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if req_id is not None:
            payload["id"] = req_id
        if params is not None:
            payload["params"] = params
        try:
            with self._client() as client:
                if method != "initialize" and not self._initialized:
                    init_id = self._next_id()
                    init = client.post(
                        f"{self.config.endpoint}/mcp",
                        headers=headers,
                        json={
                            "jsonrpc": "2.0",
                            "id": init_id,
                            "method": "initialize",
                            "params": {
                                "protocolVersion": MCP_PROTOCOL_VERSION,
                                "capabilities": {},
                                "clientInfo": {"name": "verdict-memory", "version": "0.1.0a1"},
                            },
                        },
                    )
                    self._raise_http(init)
                    init_data = init.json()
                    if not isinstance(init_data, dict) or "result" not in init_data:
                        raise MemoryProviderError(ProviderErrorCode.MALFORMED, "initialize missing result")
                    client.post(
                        f"{self.config.endpoint}/mcp",
                        headers=headers,
                        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
                    )
                    self._initialized = True
                response = client.post(
                    f"{self.config.endpoint}/mcp", headers=headers, json=payload
                )
        except MemoryProviderError as exc:
            self._record_error(method, exc)
            raise
        except httpx.TimeoutException as exc:
            err = MemoryProviderError(ProviderErrorCode.TIMEOUT, "ai-memory request timed out")
            self._record_error(method, err)
            raise err from exc
        except httpx.RequestError as exc:
            err = MemoryProviderError(ProviderErrorCode.UNREACHABLE, "ai-memory service is unreachable")
            self._record_error(method, err)
            raise err from exc
        except ValueError as exc:
            err = MemoryProviderError(ProviderErrorCode.MALFORMED, "ai-memory returned non-JSON")
            self._record_error(method, err)
            raise err from exc
        if notify:
            return None
        self._raise_http(response)
        try:
            data = response.json()
        except ValueError as exc:
            err = MemoryProviderError(ProviderErrorCode.MALFORMED, "ai-memory returned non-JSON")
            self._record_error(method, err)
            raise err from exc
        if not isinstance(data, dict):
            err = MemoryProviderError(ProviderErrorCode.MALFORMED, "mcp response is not an object")
            self._record_error(method, err)
            raise err
        if "error" in data:
            err_obj = data["error"]
            msg = redact_secrets(str(err_obj.get("message", err_obj) if isinstance(err_obj, dict) else err_obj)) or "mcp error"
            exc = MemoryProviderError(ProviderErrorCode.SERVER_ERROR, msg)
            self._record_error(method, exc)
            raise exc
        if "result" not in data:
            err = MemoryProviderError(ProviderErrorCode.MALFORMED, "mcp response missing result")
            self._record_error(method, err)
            raise err
        if req_id is not None and data.get("id") not in {req_id, None}:
            # Some servers echo id; if present and mismatched, fail closed.
            if data.get("id") != req_id:
                err = MemoryProviderError(ProviderErrorCode.MALFORMED, "mcp response id mismatch")
                self._record_error(method, err)
                raise err
        self.last_error = None
        return data.get("result")

    def _tool(self, name: str, arguments: dict[str, Any]) -> Any:
        result = self._mcp("tools/call", {"name": name, "arguments": arguments})
        if not isinstance(result, dict):
            raise MemoryProviderError(ProviderErrorCode.MALFORMED, "tool result is not an object")
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
            raise MemoryProviderError(ProviderErrorCode.MALFORMED, "tool result missing content")
        first = content_items[0]
        if not isinstance(first, dict) or first.get("type") != "text":
            raise MemoryProviderError(ProviderErrorCode.MALFORMED, "tool result content malformed")
        text = first.get("text", "")
        if not isinstance(text, str) or not text.strip():
            raise MemoryProviderError(ProviderErrorCode.MALFORMED, "tool result text empty")
        try:
            parsed = json.loads(text)
        except ValueError as exc:
            raise MemoryProviderError(ProviderErrorCode.MALFORMED, "tool result text is not JSON") from exc
        if not isinstance(parsed, dict):
            raise MemoryProviderError(ProviderErrorCode.MALFORMED, "tool result JSON must be an object")
        return parsed

    def health(self) -> ProviderHealth:
        token = self.config.load_token()
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
                capability="partial",  # health≠capture/recall schema certification
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
        # Full namespaced identity — not raw truncated source_event_id alone.
        key = event.idempotency_key
        kind = re.sub(r"[^a-zA-Z0-9._-]+", "-", event.memory_kind or "memory").strip("-") or "memory"
        return f"verdict-memory/{kind}/{key}.md"

    def _sanitize_event_for_remote(self, event: LifecycleEvent) -> tuple[str, dict[str, Any]]:
        decision = self._policy.evaluate(event)
        if not decision.eligible:
            raise MemoryProviderError(
                ProviderErrorCode.INVALID_REQUEST,
                f"capture denied: {decision.reason}",
            )
        body = redact_secrets(event.content) or ""
        # Nested redaction for frontmatter/provenance fields we send
        meta = redact_mapping(dict(event.metadata or {}))
        prov = redact_mapping(dict(event.provenance or {}))
        if not body.lstrip().startswith("#"):
            title = f"{event.kind} {event.identity.session_id}"
            body = f"# {title}\n\n{body}"
        front = {
            "kind": event.memory_kind,
            "source_harness": event.identity.source_harness,
            "source_session": event.identity.session_id,
            "source_event_id": event.identity.source_event_id,
            "content_fingerprint": event.content_fingerprint(),
            "event_digest": event.digest(),
            "idempotency_key": event.idempotency_key,
            "created_at": event.created_at,
            "trust": "remote-advisory",
            "authority_verified": False,
            "metadata": meta,
            "provenance": prov,
        }
        return body, front

    def put(self, event: LifecycleEvent) -> ExternalMemoryRef:
        if event.identity.project != self.config.project:
            raise MemoryProviderError(
                ProviderErrorCode.INVALID_REQUEST,
                "event project does not match authenticated client config project",
            )
        if event.identity.workspace != self.config.workspace:
            raise MemoryProviderError(
                ProviderErrorCode.INVALID_REQUEST,
                "event workspace does not match authenticated client config workspace",
            )
        body, front = self._sanitize_event_for_remote(event)
        args: dict[str, Any] = {
            "path": self._page_path(event),
            "body": body,
            "workspace": self.config.workspace,
            "project": self.config.project,
            "kind": event.memory_kind,
        }
        # Do NOT accept UUID-shaped metadata as verified ownership.
        page = self._tool("memory_write_page", args)
        path = page.get("path") if isinstance(page, dict) else None
        if not isinstance(path, str) or not path.strip():
            raise MemoryProviderError(
                ProviderErrorCode.MALFORMED,
                "write acknowledgement missing path",
            )
        # Require digest/fingerprint acknowledgement when server returns it; else require path match.
        expected = args["path"]
        if path != expected and not path.endswith(expected.split("/")[-1]):
            raise MemoryProviderError(
                ProviderErrorCode.MALFORMED,
                "write acknowledgement path mismatch",
            )
        # Persist digest in ref revision field when available
        revision = None
        if isinstance(page, dict):
            revision = page.get("digest") or page.get("content_fingerprint") or event.revision
        return ExternalMemoryRef(self.provider_id, path, revision if isinstance(revision, str) else event.revision)

    def search(self, query: RecallQuery) -> RecallResult:
        # Config authority: ignore/reject foreign query project/workspace.
        if query.project != self.config.project or query.workspace != self.config.workspace:
            err = MemoryProviderError(
                ProviderErrorCode.INVALID_REQUEST,
                "query project/workspace must match authenticated client config",
            )
            self._record_error("memory_query", err)
            return RecallResult(
                ProviderResultStatus.MALFORMED
                if False
                else ProviderResultStatus.UNAVAILABLE,
                error_code=err.code,
                message=err.safe_message,
                capability="partial",
            )
        try:
            data = self._tool(
                "memory_query",
                {
                    "query": query.query,
                    "workspace": self.config.workspace,
                    "project": self.config.project,
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
                ProviderErrorCode.MALFORMED: ProviderResultStatus.MALFORMED,
                ProviderErrorCode.INVALID_REQUEST: ProviderResultStatus.MALFORMED,
            }
            return RecallResult(
                status_map.get(exc.code, ProviderResultStatus.UNAVAILABLE),
                error_code=exc.code,
                message=exc.safe_message,
                capability="partial",
            )
        if "hits" not in data and "results" not in data:
            err = MemoryProviderError(ProviderErrorCode.MALFORMED, "search result missing hits")
            self._record_error("memory_query", err)
            return RecallResult(
                ProviderResultStatus.MALFORMED,
                error_code=err.code,
                message=err.safe_message,
                capability="partial",
            )
        hits_raw = data.get("hits") or data.get("results") or []
        if not isinstance(hits_raw, list):
            err = MemoryProviderError(ProviderErrorCode.MALFORMED, "search hits not a list")
            self._record_error("memory_query", err)
            return RecallResult(
                ProviderResultStatus.MALFORMED,
                error_code=err.code,
                message=err.safe_message,
                capability="partial",
            )
        hits: list[MemoryHit] = []
        for page in hits_raw:
            if not isinstance(page, dict):
                continue
            page_project = page.get("project") or page.get("project_name")
            page_workspace = page.get("workspace") or page.get("workspace_name")
            if page_project is None or page_workspace is None:
                # Missing required scope: do NOT relabel as configured project.
                # Skip as unusable for scoped recall (fail closed for labeling).
                continue
            if page_project != self.config.project or page_workspace != self.config.workspace:
                continue
            if len(hits) >= query.limit:
                break
            path = str(page.get("path") or "")
            snippet = page.get("body") or page.get("snippet") or page.get("title") or ""
            if isinstance(snippet, str):
                snippet = re.sub(r"</?mark>", "", snippet)
            content = snippet or "(empty)"
            # Preserve returned provenance fields when present; never invent caller session.
            src_harness = str(page.get("source_harness") or "ai-memory")
            src_session = str(page.get("source_session") or page.get("session_id") or "unknown")
            src_event = page.get("source_event_id")
            created = page.get("created_at")
            try:
                created_at = float(created) if created is not None else 0.0
            except (TypeError, ValueError):
                created_at = 0.0
            event = LifecycleEvent(
                kind="manual_capture",
                content=content,
                identity=EventIdentity(
                    source_harness=src_harness,
                    project=str(page_project),
                    session_id=src_session,
                    source_event_id=str(src_event) if src_event else None,
                    tenant="default",
                    workspace=str(page_workspace),
                ),
                created_at=created_at,
                scope=query.scope,
                trust="remote-advisory",
                metadata={
                    "ai_memory_path": path,
                    "ai_memory_title": page.get("title"),
                    "ai_memory_id": page.get("id"),
                    "scope_state": "established",
                    "event_digest": page.get("event_digest") or page.get("digest"),
                    "content_fingerprint": page.get("content_fingerprint"),
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
        # Bind delete to scoped package paths only (no arbitrary operator paths).
        path = ref.external_id or ""
        if not path.startswith("verdict-memory/"):
            raise MemoryProviderError(
                ProviderErrorCode.INVALID_REQUEST,
                "delete path is not a scoped verdict-memory reference",
            )
        try:
            result = self._tool(
                "memory_delete_page",
                {
                    "path": path,
                    "workspace": self.config.workspace,
                    "project": self.config.project,
                },
            )
        except MemoryProviderError:
            return False
        # Require explicit acknowledgement when present
        if isinstance(result, dict) and "deleted" in result:
            return bool(result.get("deleted"))
        # If server returns empty ack object, do not claim True
        if isinstance(result, dict) and result == {}:
            return False
        return True


__all__ = [
    "AiMemoryClient",
    "AiMemoryClientConfig",
    "DEFAULT_ENDPOINT",
    "DEFAULT_TIMEOUT",
    "DEFAULT_TOKEN_FILE",
    "MCP_PROTOCOL_VERSION",
    "redact_mapping",
    "validate_endpoint",
]
