"""Authenticated ai-memory (2.6.x) backend client.

SECURITY: configured project/workspace is authority. Event/query project
overrides are rejected. Hits missing scope are not relabeled. Remote paths
use full namespaced identity. Write ACK requires scoped readback. Nested
secrets are redacted before remote write. Malformed RPC is typed failure.
Token loaded from file in-process only; never printed.
"""

from __future__ import annotations

import json
import re
import fcntl
import hashlib
import math
from contextlib import contextmanager
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
    canonicalize,
    sanitize_event,
    redact_secrets,
)
from verdict_memory.policy import MemoryCapturePolicy

DEFAULT_ENDPOINT = "http://127.0.0.1:49374"
DEFAULT_TOKEN_FILE = Path.home() / ".config" / "ai-memory" / "auth-token"
DEFAULT_TIMEOUT = 5.0
MCP_PROTOCOL_VERSION = "2024-11-05"



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
        writer_lock_dir: str | Path | None = None,
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
        if urlparse(self.endpoint).scheme == "https" and not verify:
            raise MemoryProviderError(ProviderErrorCode.INVALID_REQUEST, "HTTPS verification required")
        self.verify = verify
        self.writer_lock_dir = Path(writer_lock_dir) if writer_lock_dir else Path.home() / ".cache/verdict-memory/locks"

    def load_token(self) -> str | None:
        if self._token_override is not None:
            return self._token_override
        if self.token_file is None:
            return None
        if not self.token_file.is_file():
            return None
        try:
            return self.token_file.read_text("utf-8").strip() or None
        except OSError as exc:
            raise MemoryProviderError(ProviderErrorCode.AUTH_FAILED, "auth token file unreadable") from exc


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
        if 300 <= response.status_code < 400:
            raise MemoryProviderError(ProviderErrorCode.INVALID_REQUEST, "redirect refused")
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
                    init_result = self._rpc_result(init_data, init_id)
                    if (not isinstance(init_result, dict)
                            or not isinstance(init_result.get("capabilities"), dict)
                            or not isinstance(init_result.get("serverInfo"), dict)
                            or init_result.get("protocolVersion") != MCP_PROTOCOL_VERSION):
                        raise MemoryProviderError(ProviderErrorCode.MALFORMED, "initialize malformed")
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
        try:
            result = self._rpc_result(data, req_id)
        except MemoryProviderError as exc:
            self._record_error(method, exc)
            raise
        self.last_error = None
        return result

    @staticmethod
    def _rpc_result(data: Any, req_id: int | None) -> Any:
        if (not isinstance(data, dict) or data.get("jsonrpc") != "2.0"
                or type(data.get("id")) is not int or data["id"] != req_id):
            raise MemoryProviderError(ProviderErrorCode.MALFORMED, "mcp response identity malformed")
        if "error" in data:
            error = data["error"]
            if not isinstance(error, dict) or type(error.get("code")) is not int or not isinstance(error.get("message"), str):
                raise MemoryProviderError(ProviderErrorCode.MALFORMED, "mcp error malformed")
            # Preserve the exact absence signal privately. Never echo remote text to logs.
            exc = MemoryProviderError(ProviderErrorCode.SERVER_ERROR, "ai-memory RPC failed")
            exc.rpc_error = error
            raise exc
        if "result" not in data:
            raise MemoryProviderError(ProviderErrorCode.MALFORMED, "mcp response missing result")
        return data["result"]

    def _tool(self, name: str, arguments: dict[str, Any]) -> Any:
        result = self._mcp("tools/call", {"name": name, "arguments": arguments})
        if not isinstance(result, dict):
            raise MemoryProviderError(ProviderErrorCode.MALFORMED, "tool result is not an object")
        if result.get("isError"):
            raise MemoryProviderError(ProviderErrorCode.SERVER_ERROR, "ai-memory tool failed")
        content_items = result.get("content") or []
        if not isinstance(content_items, list) or len(content_items) != 1:
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
        except (MemoryProviderError, httpx.RequestError, ValueError) as error:
            if isinstance(error, MemoryProviderError):
                exc = error
            elif isinstance(error, httpx.TimeoutException):
                exc = MemoryProviderError(ProviderErrorCode.TIMEOUT, "ai-memory health timed out")
            elif isinstance(error, httpx.RequestError):
                exc = MemoryProviderError(ProviderErrorCode.UNREACHABLE, "ai-memory health unreachable")
            else:
                exc = MemoryProviderError(ProviderErrorCode.MALFORMED, "ai-memory health malformed")
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

    @contextmanager
    def _writer_lock(self):
        """Serialize cooperating writers on this host; cross-host writers unsupported."""
        namespace = canonicalize([self.config.endpoint, self.config.workspace, self.config.project])
        key = hashlib.sha256(namespace.encode()).hexdigest()
        self.config.writer_lock_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        with (self.config.writer_lock_dir / (key + ".lock")).open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise MemoryProviderError(ProviderErrorCode.DEGRADED, "memory writer busy") from exc
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def _scope_args(self) -> dict[str, str]:
        return {"workspace": self.config.workspace, "project": self.config.project}

    @staticmethod
    def _page_path(event: LifecycleEvent) -> str:
        return f"verdict-memory/events/{event.idempotency_key}.md"

    @staticmethod
    def _validate_path(path: str) -> None:
        if not isinstance(path, str) or not re.fullmatch(r"verdict-memory/events/[0-9a-f]{64}\.md", path):
            raise MemoryProviderError(ProviderErrorCode.INVALID_REQUEST, "not a canonical package path")

    def _validate_scope(self, event: LifecycleEvent) -> None:
        if (event.identity.project != self.config.project
                or event.identity.workspace != self.config.workspace):
            raise MemoryProviderError(ProviderErrorCode.INVALID_REQUEST, "event scope differs from config")

    def _prepare(self, event: LifecycleEvent) -> LifecycleEvent:
        self._validate_scope(event)
        decision = self._policy.evaluate(event)
        if not decision.eligible:
            raise MemoryProviderError(ProviderErrorCode.INVALID_REQUEST, f"capture denied: {decision.reason}")
        return sanitize_event(event)

    @staticmethod
    def _body(event: LifecycleEvent) -> str:
        return "# Verdict memory event\n\n" + canonicalize({
            "schema": "verdict-memory-page/v1", "event": event.to_dict(),
            "event_digest": event.digest(), "content_fingerprint": event.content_fingerprint(),
        }) + "\n"

    def _read(self, path: str) -> LifecycleEvent | None:
        self._validate_path(path)
        try:
            page = self._tool("memory_read_page", {"path": path, **self._scope_args()})
        except MemoryProviderError as exc:
            rpc = getattr(exc, "rpc_error", {})
            if (rpc.get("code") == -32603 and isinstance(rpc.get("message"), str)
                    and rpc["message"].startswith(f"page {path} not found in resolved scope ")):
                return None
            raise
        try:
            if (page.get("path") != path or not isinstance(page.get("frontmatter"), dict)
                    or not isinstance(page.get("body"), str)):
                raise ValueError("page shape")
            prefix = "# Verdict memory event\n\n"
            body = page["body"]
            if not body.startswith(prefix):
                raise ValueError("page format")
            envelope = json.loads(body[len(prefix):])
            if not isinstance(envelope, dict) or envelope.get("schema") != "verdict-memory-page/v1":
                raise ValueError("envelope schema")
            event = LifecycleEvent.from_dict(envelope["event"])
            raw = event.to_dict()
            raw["idempotency_key"] = ""
            computed = LifecycleEvent.from_dict(raw)
            if (computed.idempotency_key != event.idempotency_key or self._page_path(event) != path
                    or envelope.get("event_digest") != event.digest()
                    or envelope.get("content_fingerprint") != event.content_fingerprint()
                    or body != self._body(event) or sanitize_event(event).to_dict() != event.to_dict()):
                raise ValueError("envelope integrity")
            self._validate_scope(event)
            if not self._policy.evaluate(event).eligible:
                raise ValueError("capture denied")
            return event
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            raise MemoryProviderError(ProviderErrorCode.MALFORMED, "stored memory envelope invalid") from exc

    def put(self, event: LifecycleEvent) -> ExternalMemoryRef:
        event = self._prepare(event)
        path = self._page_path(event)
        with self._writer_lock():
            existing = self._read(path)
            if existing is not None:
                if existing.content_fingerprint() != event.content_fingerprint():
                    raise MemoryProviderError(ProviderErrorCode.CONFLICT, "source identity payload conflict")
                return ExternalMemoryRef(self.provider_id, path, existing.digest())
            page = self._tool("memory_write_page", {
                "path": path, "body": self._body(event), "kind": event.memory_kind,
                **self._scope_args(),
            })
            if page.get("path") != path or not isinstance(page.get("page_id"), str) or not page["page_id"]:
                raise MemoryProviderError(ProviderErrorCode.MALFORMED, "write acknowledgement path or page id mismatch")
            persisted = self._read(path)
            if persisted is None or persisted.to_dict() != event.to_dict():
                raise MemoryProviderError(ProviderErrorCode.MALFORMED, "write readback mismatch")
            return ExternalMemoryRef(self.provider_id, path, persisted.digest())

    def search(self, query: RecallQuery) -> RecallResult:
        try:
            if query.project != self.config.project or query.workspace != self.config.workspace:
                raise MemoryProviderError(ProviderErrorCode.INVALID_REQUEST, "query scope differs from config")
            data = self._tool("memory_query", {
                "query": query.query, "limit": query.limit, "answer": False, **self._scope_args(),
            })
            if not isinstance(data.get("hits"), list):
                raise MemoryProviderError(ProviderErrorCode.MALFORMED, "search hits missing or malformed")
            hits: list[MemoryHit] = []
            seen: set[str] = set()
            for candidate in data["hits"][:query.limit]:
                if not isinstance(candidate, dict) or not isinstance(candidate.get("path"), str):
                    raise MemoryProviderError(ProviderErrorCode.MALFORMED, "search hit malformed")
                path = candidate["path"]
                if (candidate.get("project", self.config.project) != self.config.project
                        or candidate.get("workspace", self.config.workspace) != self.config.workspace):
                    continue
                if not re.fullmatch(r"verdict-memory/events/[0-9a-f]{64}\.md", path) or path in seen:
                    continue
                seen.add(path)
                event = self._read(path)
                if event is None or event.scope != query.scope or event.identity.tenant != query.tenant:
                    continue
                score = candidate.get("rank", 0.0)
                if type(score) not in (int, float) or not math.isfinite(score):
                    raise MemoryProviderError(ProviderErrorCode.MALFORMED, "search rank malformed")
                hits.append(MemoryHit(ExternalMemoryRef(self.provider_id, path, event.digest()), event, float(score), untrusted=True))
            self.clear_error()
            return RecallResult(ProviderResultStatus.AVAILABLE, tuple(hits), capability="partial")
        except MemoryProviderError as exc:
            self._record_error("memory_query", exc)
            status = {
                ProviderErrorCode.TIMEOUT: ProviderResultStatus.TIMEOUT,
                ProviderErrorCode.AUTH_FAILED: ProviderResultStatus.AUTH_FAILED,
                ProviderErrorCode.MALFORMED: ProviderResultStatus.MALFORMED,
                ProviderErrorCode.INVALID_REQUEST: ProviderResultStatus.MALFORMED,
            }.get(exc.code, ProviderResultStatus.UNAVAILABLE)
            return RecallResult(status, error_code=exc.code, message=exc.safe_message, capability="partial")

    def delete(self, ref: ExternalMemoryRef) -> bool:
        if ref.provider_id != self.provider_id:
            return False
        path = ref.external_id
        self._validate_path(path)
        try:
            with self._writer_lock():
                event = self._read(path)
                if event is None or (ref.revision is not None and ref.revision != event.digest()):
                    return False
                ack = self._tool("memory_delete_page", {"path": path, **self._scope_args()})
                if ack.get("path") != path or ack.get("deleted") is not True:
                    return False
                return self._read(path) is None
        except MemoryProviderError as exc:
            self._record_error("memory_delete_page", exc)
            return False


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
