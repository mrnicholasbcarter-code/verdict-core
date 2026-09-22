"""Versioned boundary for optional, advisory shared memory providers.

The local :class:`verdict.memory_plane.MemoryPlane` remains authoritative.
Nothing returned by this module can assert local authority.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Protocol

from verdict.memory_plane import MemoryRecord

ENVELOPE_SCHEMA_VERSION = "shared-memory-envelope/v1"
SHARED_MEMORY_PROTOCOL_VERSION = "1"
DEFAULT_TOKEN_ENV = "VERDICT_SHARED_MEMORY_TOKEN"
DEFAULT_ENDPOINT_ENV = "VERDICT_SHARED_MEMORY_URL"


class ProviderResultStatus(str, Enum):
    """Outcome of an operation; an empty available result is not a failure."""

    AVAILABLE = "available"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"
    TIMEOUT = "timeout"
    AUTH_FAILED = "auth_failed"
    INCOMPATIBLE = "incompatible"
    MALFORMED = "malformed"


class ProviderErrorCode(str, Enum):
    NOT_CONFIGURED = "not_configured"
    AUTH_FAILED = "auth_failed"
    UNREACHABLE = "unreachable"
    TIMEOUT = "timeout"
    RATE_LIMITED = "rate_limited"
    SCHEMA_INCOMPATIBLE = "schema_incompatible"
    PROTOCOL_INCOMPATIBLE = "protocol_incompatible"
    INVALID_REQUEST = "invalid_request"
    SERVER_ERROR = "server_error"
    DEGRADED = "degraded"
    UNKNOWN = "unknown"


class CertificationState(str, Enum):
    NOT_INSTALLED = "not-installed"
    INSTALLED = "installed"
    CONFIGURED = "configured"
    AUTHENTICATED = "authenticated"
    REACHABLE = "reachable"
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"
    INCOMPATIBLE = "incompatible"


class SharedMemoryProviderError(RuntimeError):
    """Named, redacted provider failure."""

    def __init__(self, code: ProviderErrorCode, message: str):
        self.code = code
        self.safe_message = redact_secrets(message)
        super().__init__(f"{code.value}: {self.safe_message}")


@dataclass(frozen=True)
class SharedMemoryEnvelope:
    """Canonical provider-neutral record passed across the shared boundary."""

    content: str
    project: str
    scope: str = "default"
    tenant: str = "default"
    memory_kind: str = "memory"
    source_harness: str = "verdict"
    source_agent: str = "unknown"
    source_session: str | None = None
    created_at: float = 0.0
    observed_at: float = 0.0
    expires_at: float | None = None
    retention_class: str = "standard"
    sensitivity: str = "standard"
    trust: str = "remote-advisory"
    authority: str = "shared-memory-advisory"
    authority_verified: bool = False
    provenance: Mapping[str, Any] = field(default_factory=dict)
    revision: str = "1"
    classification: str = "source"
    derived_from: tuple[str, ...] = ()
    payload: Mapping[str, Any] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)
    content_hash: str = ""
    idempotency_key: str = ""
    schema_version: str = ENVELOPE_SCHEMA_VERSION
    protocol_version: str = SHARED_MEMORY_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != ENVELOPE_SCHEMA_VERSION:
            raise SharedMemoryProviderError(
                ProviderErrorCode.SCHEMA_INCOMPATIBLE,
                f"unsupported envelope schema: {self.schema_version}",
            )
        if self.protocol_version != SHARED_MEMORY_PROTOCOL_VERSION:
            raise SharedMemoryProviderError(
                ProviderErrorCode.PROTOCOL_INCOMPATIBLE,
                f"unsupported shared-memory protocol: {self.protocol_version}",
            )
        if not self.content or not self.project or not self.scope or not self.tenant:
            raise ValueError("content, project, scope, and tenant are required")
        if self.classification not in {"source", "derived"}:
            raise ValueError("classification must be 'source' or 'derived'")
        actual_hash = hashlib.sha256(self.content.encode("utf-8")).hexdigest()
        if self.content_hash and self.content_hash != actual_hash:
            raise ValueError("content_hash does not match content")
        object.__setattr__(self, "content_hash", actual_hash)
        object.__setattr__(self, "provenance", dict(self.provenance))
        object.__setattr__(self, "payload", dict(self.payload))
        object.__setattr__(self, "metadata", dict(self.metadata))
        object.__setattr__(self, "derived_from", tuple(self.derived_from))
        if not self.idempotency_key:
            object.__setattr__(self, "idempotency_key", self.digest())

    def _digest_payload(self) -> dict[str, Any]:
        """Return stable logical identity, excluding the observation clock."""
        return {
            "schema_version": self.schema_version,
            "protocol_version": self.protocol_version,
            "content_hash": self.content_hash,
            "tenant": self.tenant,
            "project": self.project,
            "scope": self.scope,
            "source_harness": self.source_harness,
            "source_agent": self.source_agent,
            "source_session": self.source_session,
            "memory_kind": self.memory_kind,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "retention_class": self.retention_class,
            "sensitivity": self.sensitivity,
            "trust": self.trust,
            "classification": self.classification,
            "derived_from": list(self.derived_from),
            "revision": self.revision,
            "provenance": dict(self.provenance),
            "payload": dict(self.payload),
            "metadata": dict(self.metadata),
        }

    def digest(self) -> str:
        encoded = json.dumps(
            self._digest_payload(), sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["derived_from"] = list(self.derived_from)
        return result

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> SharedMemoryEnvelope:
        raw = dict(value)
        raw["derived_from"] = tuple(raw.get("derived_from", ()))
        return cls(**raw)


@dataclass(frozen=True)
class ExternalMemoryRef:
    provider_id: str
    external_id: str
    revision: str | None = None


@dataclass(frozen=True)
class SharedMemoryQuery:
    query: str
    project: str
    scope: str = "default"
    tenant: str = "default"
    limit: int = 10

    def __post_init__(self) -> None:
        if not self.project or not self.scope or not self.tenant:
            raise ValueError("project, scope, and tenant are required")
        if not 1 <= self.limit <= 100:
            raise ValueError("limit must be between 1 and 100")


@dataclass(frozen=True)
class SharedMemoryHit:
    ref: ExternalMemoryRef
    envelope: SharedMemoryEnvelope
    score: float = 0.0


@dataclass(frozen=True)
class SharedMemorySearchResult:
    status: ProviderResultStatus
    hits: tuple[SharedMemoryHit, ...] = ()
    error_code: ProviderErrorCode | None = None
    message: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "hits", tuple(self.hits))
        if self.status != ProviderResultStatus.AVAILABLE and self.hits:
            raise ValueError("failed provider results cannot contain hits")
        if self.message is not None:
            object.__setattr__(self, "message", redact_secrets(self.message))


@dataclass(frozen=True)
class ProviderHealth:
    provider_id: str
    status: ProviderResultStatus
    state: CertificationState
    protocol_version: str | None = None
    provider_version: str | None = None
    backend: str | None = None
    auth_state: str = "not-configured"
    message: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "status": self.status.value,
            "state": self.state.value,
            "protocol_version": self.protocol_version,
            "provider_version": self.provider_version,
            "backend": self.backend,
            "auth_state": self.auth_state,
            "message": redact_secrets(self.message) if self.message else None,
        }


class SharedMemoryProvider(Protocol):
    """Replaceable shared provider contract. Implementations require no LLM."""

    provider_id: str
    protocol_version: str

    def health(self) -> ProviderHealth: ...

    def put(self, envelope: SharedMemoryEnvelope) -> ExternalMemoryRef: ...

    def search(self, query: SharedMemoryQuery) -> SharedMemorySearchResult: ...

    def delete(self, ref: ExternalMemoryRef) -> bool: ...


class FakeSharedMemoryProvider:
    """Deterministic, offline contract implementation used by CI and consumers."""

    provider_id = "fake-shared-memory"
    protocol_version = SHARED_MEMORY_PROTOCOL_VERSION

    def __init__(
        self,
        *,
        status: ProviderResultStatus = ProviderResultStatus.AVAILABLE,
        required_token: str | None = None,
        token: str | None = None,
    ) -> None:
        self.status = status
        self._required_token = required_token
        self._token = token
        self._records: dict[str, SharedMemoryEnvelope] = {}

    def _check(self) -> None:
        if self._required_token is not None and self._token != self._required_token:
            raise SharedMemoryProviderError(ProviderErrorCode.AUTH_FAILED, "authentication failed")
        if self.status == ProviderResultStatus.TIMEOUT:
            raise SharedMemoryProviderError(ProviderErrorCode.TIMEOUT, "provider timed out")
        if self.status != ProviderResultStatus.AVAILABLE:
            raise SharedMemoryProviderError(ProviderErrorCode.UNREACHABLE, "provider unavailable")

    def health(self) -> ProviderHealth:
        try:
            self._check()
        except SharedMemoryProviderError as exc:
            status = (
                ProviderResultStatus.AUTH_FAILED
                if exc.code == ProviderErrorCode.AUTH_FAILED
                else self.status
            )
            return ProviderHealth(
                self.provider_id,
                status,
                CertificationState.UNAVAILABLE,
                self.protocol_version,
                "fake-1",
                "memory",
                "failed" if exc.code == ProviderErrorCode.AUTH_FAILED else "not-required",
                exc.safe_message,
            )
        return ProviderHealth(
            self.provider_id,
            ProviderResultStatus.AVAILABLE,
            CertificationState.HEALTHY,
            self.protocol_version,
            "fake-1",
            "memory",
            "authenticated" if self._required_token else "not-required",
        )

    def put(self, envelope: SharedMemoryEnvelope) -> ExternalMemoryRef:
        self._check()
        external_id = envelope.idempotency_key
        self._records.setdefault(external_id, envelope)
        return ExternalMemoryRef(self.provider_id, external_id, envelope.revision)

    def search(self, query: SharedMemoryQuery) -> SharedMemorySearchResult:
        try:
            self._check()
        except SharedMemoryProviderError as exc:
            status = {
                ProviderErrorCode.TIMEOUT: ProviderResultStatus.TIMEOUT,
                ProviderErrorCode.AUTH_FAILED: ProviderResultStatus.AUTH_FAILED,
            }.get(exc.code, ProviderResultStatus.UNAVAILABLE)
            return SharedMemorySearchResult(status, error_code=exc.code, message=exc.safe_message)
        words = tuple(word.casefold() for word in query.query.split() if word)
        matches: list[SharedMemoryHit] = []
        for external_id, envelope in self._records.items():
            if (
                envelope.project != query.project
                or envelope.scope != query.scope
                or envelope.tenant != query.tenant
            ):
                continue
            text = envelope.content.casefold()
            if words and not any(word in text for word in words):
                continue
            score = float(sum(word in text for word in words)) / max(1, len(words))
            matches.append(
                SharedMemoryHit(
                    ExternalMemoryRef(self.provider_id, external_id, envelope.revision),
                    envelope,
                    score,
                )
            )
        matches.sort(key=lambda hit: (-hit.score, hit.ref.external_id))
        return SharedMemorySearchResult(
            ProviderResultStatus.AVAILABLE, tuple(matches[: query.limit])
        )

    def delete(self, ref: ExternalMemoryRef) -> bool:
        self._check()
        if ref.provider_id != self.provider_id:
            return False
        return self._records.pop(ref.external_id, None) is not None


def normalize_hit_to_memory_record(hit: SharedMemoryHit) -> MemoryRecord:
    """Normalize remote data while forcibly dropping every authority assertion."""
    envelope = hit.envelope
    provenance = dict(envelope.provenance)
    provenance.update(
        {
            "provider_id": hit.ref.provider_id,
            "external_id": hit.ref.external_id,
            "revision": hit.ref.revision or envelope.revision,
            "tenant_scope": envelope.tenant,
            "project_scope": envelope.project,
            "source_harness": envelope.source_harness,
            "source_agent": envelope.source_agent,
            "observed_at": envelope.observed_at or envelope.created_at,
        }
    )
    return MemoryRecord(
        record_id=f"shared:{hit.ref.provider_id}:{hit.ref.external_id}",
        namespace=envelope.memory_kind,
        key=envelope.idempotency_key,
        content=envelope.content,
        source=f"shared-memory:{hit.ref.provider_id}",
        trust=envelope.trust,
        scope=envelope.scope,
        metadata={"shared_memory": dict(envelope.metadata), "payload": dict(envelope.payload)},
        created_at=envelope.created_at,
        updated_at=envelope.observed_at or envelope.created_at,
        expires_at=envelope.expires_at,
        authority="shared-memory-advisory",
        authority_verified=False,
        confidence=max(0.0, min(1.0, hit.score)),
        sensitivity=envelope.sensitivity,
        provenance=provenance,
        content_hash=envelope.content_hash,
    )


def redact_secrets(value: str) -> str:
    """Redact common credentials and URL user-info without echoing a canary."""
    import re

    redacted = re.sub(r"(?i)(bearer|basic)\s+[^\s,;]+", r"\1 [REDACTED]", value)
    redacted = re.sub(
        r"(?i)(token|secret|password|api[_-]?key)(\s*[=:]\s*)[^\s,;&]+", r"\1\2[REDACTED]", redacted
    )
    redacted = re.sub(r"(https?://)[^/@\s]+@", r"\1[REDACTED]@", redacted)
    return redacted


def _read_mcp_config(path: Path) -> bool:
    if not path.is_file():
        return False
    try:
        raw = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return False
    servers = raw.get("mcpServers", {}) if isinstance(raw, dict) else {}
    if not isinstance(servers, dict):
        return False
    for name, config in servers.items():
        text = json.dumps({"name": name, "config": config}).casefold()
        if "mcp-memory-service" in text or "memory-service" in text:
            return True
    return False


def discover_shared_memory_setup(
    *, env: Mapping[str, str] | None = None, home_dir: Path | None = None, cwd: Path | None = None
) -> dict[str, Any]:
    """Discover an existing provider without installing or modifying anything."""
    values = os.environ if env is None else env
    home = Path.home() if home_dir is None else home_dir
    root = Path.cwd() if cwd is None else cwd
    endpoint = values.get(DEFAULT_ENDPOINT_ENV, "").strip()
    configured = bool(endpoint)
    installed = bool(shutil.which("mcp-memory-service")) or any(
        _read_mcp_config(path) for path in (root / ".mcp.json", home / ".mcp.json")
    )
    state = (
        CertificationState.CONFIGURED
        if configured
        else CertificationState.INSTALLED
        if installed
        else CertificationState.NOT_INSTALLED
    )
    return {
        "provider_id": "mcp-memory-service",
        "state": state.value,
        "installed": installed,
        "configured": configured,
        "endpoint": redact_secrets(endpoint) if endpoint else None,
        "auth_state": "configured" if values.get(DEFAULT_TOKEN_ENV) else "not-configured",
        "token_env": DEFAULT_TOKEN_ENV,
        "install_action": "none",
        "mutated": False,
    }


def doctor_shared_memory_report(
    *,
    env: Mapping[str, str] | None = None,
    home_dir: Path | None = None,
    cwd: Path | None = None,
    provider: SharedMemoryProvider | None = None,
) -> dict[str, Any]:
    """Return truthful, redacted provider certification details."""
    discovery = discover_shared_memory_setup(env=env, home_dir=home_dir, cwd=cwd)
    if provider is None:
        if not discovery["configured"]:
            return discovery
        from verdict.shared_memory_mcp import MCPMemoryServiceProvider

        values = os.environ if env is None else env
        provider = MCPMemoryServiceProvider(
            endpoint=str(values.get(DEFAULT_ENDPOINT_ENV, "")),
            token_env=DEFAULT_TOKEN_ENV,
            env=values,
        )
    health = provider.health()
    return {**discovery, **health.to_dict(), "endpoint": discovery["endpoint"]}


__all__ = [
    "DEFAULT_ENDPOINT_ENV",
    "DEFAULT_TOKEN_ENV",
    "ENVELOPE_SCHEMA_VERSION",
    "SHARED_MEMORY_PROTOCOL_VERSION",
    "CertificationState",
    "ExternalMemoryRef",
    "FakeSharedMemoryProvider",
    "ProviderErrorCode",
    "ProviderHealth",
    "ProviderResultStatus",
    "SharedMemoryEnvelope",
    "SharedMemoryHit",
    "SharedMemoryProvider",
    "SharedMemoryProviderError",
    "SharedMemoryQuery",
    "SharedMemorySearchResult",
    "discover_shared_memory_setup",
    "doctor_shared_memory_report",
    "normalize_hit_to_memory_record",
    "redact_secrets",
]
