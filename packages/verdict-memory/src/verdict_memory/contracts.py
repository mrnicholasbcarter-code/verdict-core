"""Versioned lifecycle/memory contracts (language-neutral JSON seam).

Schema family: lifecycle-event/v1 (experimental until a validated vertical).
Serialization convention matches existing Verdict shared-memory digests:
``json.dumps(..., sort_keys=True, separators=(",", ":"), ensure_ascii=False)``
then UTF-8 + SHA-256.

Logical digests exclude ``observed_at`` and ``deadline_at``. Stable
``identity.source_event_id`` (harness-supplied) is preferred over adapter
wall-clocks for replay/compaction dedupe.

Float note: JSON numbers are serialized with Python's default (shortest
round-trip) form. Golden vectors pin exact decimal spellings for
cross-language checks; Rust consumers must emit the same digit string for
the fixture values (no scientific notation for the pinned integers/floats).
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Literal

CONTRACT_API_VERSION = "0.1.0a1"
LIFECYCLE_EVENT_SCHEMA = "lifecycle-event/v1"
ENVELOPE_SCHEMA_VERSION = "shared-memory-envelope/v1"
SHARED_MEMORY_PROTOCOL_VERSION = "1"

CAPABILITY_STATUS_SUPPORTED = "supported"
CAPABILITY_STATUS_PARTIAL = "partial"
CAPABILITY_STATUS_UNSUPPORTED = "unsupported"
CAPABILITY_STATUS_UNKNOWN = "unknown"

# BOD-80 canonical lifecycle kinds (ticket mandatory list).
# Provisional aliases kept for early API compatibility:
#   before_turn -> before_first_model_turn
#   compaction  -> before_compact
EventKind = Literal[
    "session_start",
    "before_first_model_turn",
    "before_tool",
    "after_tool",
    "after_edit",
    "subagent_start",
    "subagent_end",
    "before_compact",
    "before_yield",
    "verification_result",
    "session_end",
    "resume",
    # package-level / non-harness capture kinds (still v1 family):
    "handoff",
    "manual_capture",
    # provisional aliases (normalized at construction):
    "before_turn",
    "compaction",
]

EVENT_KIND_ALIASES: dict[str, str] = {
    "before_turn": "before_first_model_turn",
    "compaction": "before_compact",
}

CANONICAL_EVENT_KINDS: frozenset[str] = frozenset(
    {
        "session_start",
        "before_first_model_turn",
        "before_tool",
        "after_tool",
        "after_edit",
        "subagent_start",
        "subagent_end",
        "before_compact",
        "before_yield",
        "verification_result",
        "session_end",
        "resume",
    }
)

ALLOWED_EVENT_KINDS: frozenset[str] = CANONICAL_EVENT_KINDS | {
    "handoff",
    "manual_capture",
}

LIFECYCLE_EVENT_KNOWN_FIELDS: frozenset[str] = frozenset(
    {
        "schema_version",
        "protocol_version",
        "kind",
        "content",
        "content_hash",
        "identity",
        "created_at",
        "observed_at",
        "scope",
        "memory_kind",
        "sensitivity",
        "trust",
        "classification",
        "derived_from",
        "provenance",
        "payload",
        "metadata",
        "revision",
        "idempotency_key",
        "deadline_at",
    }
)

EVENT_IDENTITY_KNOWN_FIELDS: frozenset[str] = frozenset(
    {
        "source_harness",
        "project",
        "session_id",
        "parent_session_id",
        "attempt_id",
        "source_event_id",
        "tenant",
        "workspace",
        "source_agent",
    }
)


class ProviderResultStatus(str, Enum):
    """Outcome of an operation; healthy-empty is AVAILABLE with zero hits."""

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
    QUEUE_FULL = "queue_full"
    MALFORMED = "malformed"
    CONFLICT = "conflict"
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


def redact_secrets(value: str | None) -> str | None:
    """Redact common credentials and URL user-info without echoing a canary."""
    if value is None:
        return None
    redacted = re.sub(r"(?i)(bearer|basic)\s+[^\s,;]+", r"\1 [REDACTED]", value)
    redacted = re.sub(
        r"(?i)(token|secret|password|api[_-]?key)(\s*[=:]\s*)[^\s,;&]+",
        r"\1\2[REDACTED]",
        redacted,
    )
    redacted = re.sub(r"\bsk-[A-Za-z0-9]{16,}\b", "[REDACTED]", redacted)
    redacted = re.sub(r"(https?://)[^/@\s]+@", r"\1[REDACTED]@", redacted)
    return redacted


def redact_mapping(value: Any) -> Any:
    """Recursively redact secret-shaped strings in mappings/lists.

    Values under secret-named keys are fully redacted even when the value itself
    does not match a content pattern.
    """
    from collections.abc import Mapping as MappingType

    _SECRET_KEYS = re.compile(r"(?i)(token|secret|password|api[_-]?key|authorization|auth)$")

    if isinstance(value, str):
        return redact_secrets(value) or ""
    if isinstance(value, MappingType):
        out: dict[str, Any] = {}
        for k, v in value.items():
            key = str(k)
            if _SECRET_KEYS.search(key):
                out[key] = "[REDACTED]"
            else:
                out[key] = redact_mapping(v)
        return out
    if isinstance(value, (list, tuple)):
        return [redact_mapping(v) for v in value]
    return value


def _json_default(value: Any) -> Any:
    raise TypeError(f"value is not JSON-serializable: {type(value)!r}")


def canonicalize(value: Any) -> str:
    """Canonical JSON as Unicode (UTF-8 safe). Rejects non-finite floats."""

    def scrub(obj: Any) -> Any:
        if isinstance(obj, float):
            if not math.isfinite(obj):
                raise ValueError("non-finite floats are not allowed in canonical JSON")
            return obj
        if isinstance(obj, dict):
            return {k: scrub(v) for k, v in obj.items()}
        if isinstance(obj, (list, tuple)):
            return [scrub(v) for v in obj]
        return obj

    return json.dumps(
        scrub(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
        default=_json_default,
    )


def digest_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def event_digest(payload: Mapping[str, Any]) -> str:
    return digest_bytes(canonicalize(payload).encode("utf-8"))


class IdempotencyConflictError(ValueError):
    """Same source_event_id with a conflicting logical payload.

    Replay with the same content is idempotent. A different content_hash /
    kind / scope for the same source identity is an explicit conflict — never
    a second silent memory.
    """

    def __init__(self, source_event_id: str, existing_digest: str, new_digest: str):
        self.source_event_id = source_event_id
        self.existing_digest = existing_digest
        self.new_digest = new_digest
        super().__init__(
            f"idempotency conflict for source_event_id={source_event_id!r}: "
            f"existing={existing_digest[:16]}… new={new_digest[:16]}…"
        )


def source_idempotency_key(
    *,
    schema_version: str,
    protocol_version: str,
    source_harness: str,
    project: str,
    workspace: str,
    tenant: str,
    session_id: str,
    source_event_id: str,
) -> str:
    """Stable enqueue key when harness supplies source_event_id.

    Namespace rule (explicit): (schema, protocol, source_harness, project,
    workspace, tenant, session_id, source_event_id). Same source_event_id in a
    different session does NOT collide. Excludes created_at, observed_at,
    content, and adapter clocks so replay with a rewritten timestamp does not
    fork the key.
    """
    if not source_event_id.strip():
        raise ValueError("source_event_id is required for source_idempotency_key")
    if not session_id.strip():
        raise ValueError("session_id is required for source_idempotency_key")
    if not project.strip() or not source_harness.strip():
        raise ValueError("project and source_harness are required for source_idempotency_key")
    return event_digest(
        {
            "schema_version": schema_version,
            "protocol_version": protocol_version,
            "source_harness": source_harness,
            "project": project,
            "workspace": workspace,
            "tenant": tenant,
            "session_id": session_id,
            "source_event_id": source_event_id,
        }
    )


@dataclass(frozen=True)
class EventIdentity:
    """Harness-provided provenance. Auth/project authority comes from client config.

    ``source_event_id`` is the stable logical source identity supplied by the
    harness (not an adapter wall-clock). Replay/compaction must reuse it so
    the same source event enqueues once.
    """

    source_harness: str
    project: str
    session_id: str
    parent_session_id: str | None = None
    attempt_id: str | None = None
    source_event_id: str | None = None
    tenant: str = "default"
    workspace: str = "default"
    source_agent: str = "unknown"

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_harness": self.source_harness,
            "project": self.project,
            "session_id": self.session_id,
            "parent_session_id": self.parent_session_id,
            "attempt_id": self.attempt_id,
            "source_event_id": self.source_event_id,
            "tenant": self.tenant,
            "workspace": self.workspace,
            "source_agent": self.source_agent,
        }


@dataclass(frozen=True)
class LifecycleEvent:
    """Versioned lifecycle event. Logical identity excludes observed_at."""

    kind: EventKind
    content: str
    identity: EventIdentity
    created_at: float
    content_hash: str = ""
    observed_at: float | None = None
    scope: str = "default"
    memory_kind: str = "memory"
    sensitivity: str = "standard"
    trust: str = "remote-advisory"
    classification: Literal["source", "derived"] = "source"
    derived_from: tuple[str, ...] = ()
    provenance: Mapping[str, Any] = field(default_factory=dict)
    payload: Mapping[str, Any] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)
    revision: str = "1"
    schema_version: str = LIFECYCLE_EVENT_SCHEMA
    protocol_version: str = SHARED_MEMORY_PROTOCOL_VERSION
    idempotency_key: str = ""
    deadline_at: float | None = None

    def __post_init__(self) -> None:
        canon_kind = EVENT_KIND_ALIASES.get(self.kind, self.kind)
        if canon_kind != self.kind:
            object.__setattr__(self, "kind", canon_kind)
        if self.kind not in ALLOWED_EVENT_KINDS:
            raise ValueError(f"unsupported event kind: {self.kind}")
        if self.schema_version != LIFECYCLE_EVENT_SCHEMA:
            raise ValueError(f"unsupported schema_version: {self.schema_version}")
        if self.protocol_version != SHARED_MEMORY_PROTOCOL_VERSION:
            raise ValueError(f"unsupported protocol_version: {self.protocol_version}")
        if not self.content or not self.identity.project or not self.identity.session_id:
            raise ValueError("content, project, and session_id are required")
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
            # Prefer harness source_event_id so replay with a rewritten created_at
            # does not fork the enqueue key. Content conflicts are detected separately.
            sid = self.identity.source_event_id
            if sid and str(sid).strip():
                key = source_idempotency_key(
                    schema_version=self.schema_version,
                    protocol_version=self.protocol_version,
                    source_harness=self.identity.source_harness,
                    project=self.identity.project,
                    workspace=self.identity.workspace,
                    tenant=self.identity.tenant,
                    session_id=self.identity.session_id,
                    source_event_id=str(sid),
                )
            else:
                key = self.digest()
            object.__setattr__(self, "idempotency_key", key)

    def _digest_payload(self) -> dict[str, Any]:
        """Stable logical identity — excludes observation clock and deadline."""
        return {
            "schema_version": self.schema_version,
            "protocol_version": self.protocol_version,
            "kind": self.kind,
            "content_hash": self.content_hash,
            "identity": self.identity.to_dict(),
            "created_at": self.created_at,
            "scope": self.scope,
            "memory_kind": self.memory_kind,
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
        return event_digest(self._digest_payload())

    def content_fingerprint(self) -> str:
        """Logical payload fingerprint for conflict detection.

        When ``source_event_id`` is set, excludes ``created_at`` so a replay that
        rewrites observation/receipt clocks (or mistakenly rewrites created_at)
        does not false-conflict. Content, kind, scope, and identity still bind.
        Without ``source_event_id``, equals ``digest()`` (created_at included).
        """
        payload = self._digest_payload()
        if self.identity.source_event_id and str(self.identity.source_event_id).strip():
            payload = dict(payload)
            payload.pop("created_at", None)
        return event_digest(payload)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "protocol_version": self.protocol_version,
            "kind": self.kind,
            "content": self.content,
            "content_hash": self.content_hash,
            "identity": self.identity.to_dict(),
            "created_at": self.created_at,
            "observed_at": self.observed_at,
            "scope": self.scope,
            "memory_kind": self.memory_kind,
            "sensitivity": self.sensitivity,
            "trust": self.trust,
            "classification": self.classification,
            "derived_from": list(self.derived_from),
            "provenance": dict(self.provenance),
            "payload": dict(self.payload),
            "metadata": dict(self.metadata),
            "revision": self.revision,
            "idempotency_key": self.idempotency_key,
            "deadline_at": self.deadline_at,
        }

    def to_canonical_json(self) -> str:
        return canonicalize(self.to_dict())

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> LifecycleEvent:
        """Load event. Unknown optional fields are ignored (documented additive policy).

        Unsupported ``kind`` values raise ``ValueError`` after alias normalization
        (explicit error, not silent success).
        """
        incoming = dict(value)
        raw = {k: v for k, v in incoming.items() if k in LIFECYCLE_EVENT_KNOWN_FIELDS}
        identity = raw.pop("identity", None)
        if not isinstance(identity, Mapping):
            raise TypeError("identity must be an object")
        id_raw = {k: v for k, v in dict(identity).items() if k in EVENT_IDENTITY_KNOWN_FIELDS}
        raw["identity"] = EventIdentity(**id_raw)
        raw["derived_from"] = tuple(raw.get("derived_from") or ())
        return cls(**raw)  # type: ignore[arg-type]


def loads_lifecycle_event(text: str | bytes) -> LifecycleEvent:
    if isinstance(text, bytes):
        text = text.decode("utf-8")
    return LifecycleEvent.from_dict(json.loads(text))


@dataclass(frozen=True)
class CaptureDecision:
    """Mechanical capture eligibility. Reasons are stable audit labels."""

    eligible: bool
    reason: str
    event_digest: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "eligible": self.eligible,
            "reason": self.reason,
            "event_digest": self.event_digest,
        }


def sanitize_event(event: LifecycleEvent) -> LifecycleEvent:
    """One sanitized logical object for both outbox and remote persistence."""
    raw = redact_mapping(event.to_dict())
    raw["content_hash"] = ""
    raw["idempotency_key"] = ""
    return LifecycleEvent.from_dict(raw)


@dataclass(frozen=True)
class ExternalMemoryRef:
    provider_id: str
    external_id: str
    revision: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RecallQuery:
    query: str
    project: str
    scope: str = "default"
    tenant: str = "default"
    workspace: str = "default"
    session_id: str | None = None
    limit: int = 10
    deadline_at: float | None = None

    def __post_init__(self) -> None:
        if not self.project or not self.scope or not self.tenant:
            raise ValueError("project, scope, and tenant are required")
        if not 1 <= self.limit <= 100:
            raise ValueError("limit must be between 1 and 100")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class MemoryHit:
    ref: ExternalMemoryRef
    event: LifecycleEvent
    score: float = 0.0
    untrusted: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "ref": self.ref.to_dict(),
            "event": self.event.to_dict(),
            "score": self.score,
            "untrusted": self.untrusted,
        }


@dataclass(frozen=True)
class RecallResult:
    status: ProviderResultStatus
    hits: tuple[MemoryHit, ...] = ()
    error_code: ProviderErrorCode | None = None
    message: str | None = None
    capability: str = CAPABILITY_STATUS_SUPPORTED

    def __post_init__(self) -> None:
        object.__setattr__(self, "hits", tuple(self.hits))
        if self.status != ProviderResultStatus.AVAILABLE and self.hits:
            raise ValueError("failed provider results cannot contain hits")
        if self.message is not None:
            object.__setattr__(self, "message", redact_secrets(self.message))

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "hits": [h.to_dict() for h in self.hits],
            "error_code": self.error_code.value if self.error_code else None,
            "message": self.message,
            "capability": self.capability,
        }


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
    capability: str = CAPABILITY_STATUS_SUPPORTED

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
            "capability": self.capability,
        }


class MemoryProviderError(RuntimeError):
    def __init__(self, code: ProviderErrorCode, message: str):
        self.code = code
        self.safe_message = redact_secrets(message) or ""
        super().__init__(f"{code.value}: {self.safe_message}")


__all__ = [
    "ALLOWED_EVENT_KINDS",
    "CANONICAL_EVENT_KINDS",
    "CAPABILITY_STATUS_PARTIAL",
    "CAPABILITY_STATUS_SUPPORTED",
    "CAPABILITY_STATUS_UNKNOWN",
    "CAPABILITY_STATUS_UNSUPPORTED",
    "CONTRACT_API_VERSION",
    "ENVELOPE_SCHEMA_VERSION",
    "EVENT_IDENTITY_KNOWN_FIELDS",
    "EVENT_KIND_ALIASES",
    "LIFECYCLE_EVENT_KNOWN_FIELDS",
    "LIFECYCLE_EVENT_SCHEMA",
    "SHARED_MEMORY_PROTOCOL_VERSION",
    "CaptureDecision",
    "CertificationState",
    "EventIdentity",
    "ExternalMemoryRef",
    "IdempotencyConflictError",
    "LifecycleEvent",
    "MemoryHit",
    "MemoryProviderError",
    "ProviderErrorCode",
    "ProviderHealth",
    "ProviderResultStatus",
    "RecallQuery",
    "RecallResult",
    "canonicalize",
    "digest_bytes",
    "event_digest",
    "loads_lifecycle_event",
    "redact_mapping",
    "redact_secrets",
    "source_idempotency_key",
]
