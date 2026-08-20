"""Durable, provenance-aware write gate for the local MemoryPlane.

The gate is intentionally provider-neutral.  It verifies a small, explicit
authority registry, applies namespace policy, redacts sensitive values, and
persists both accepted writes and rejected/conflicting attempts as evidence in
the same SQLite plane.  No caller-supplied authority level is trusted as
proof; the level is derived from the registered authority identifier.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from verdict.enforcement import EnforcementContext, check_enforcement
from verdict.guidance import GuidanceControlPlane
from verdict.memory_plane import MemoryPlane, MemoryRecord

GATE_VERSION = "1"
_SECRET_KEY = re.compile(
    r"(?:api[_-]?key|access[_-]?key|secret|password|passwd|token|credential|authorization|cookie|private[_-]?key)",
    re.IGNORECASE,
)
_PROMPT_KEY = re.compile(r"(?:^|[_-])(prompt|messages|conversation|transcript)(?:$|[_-])", re.I)
_SECRET_VALUE = re.compile(
    r"(?i)(?:bearer\s+|basic\s+|sk-[a-z0-9_-]{8,}|gh[pousr]_[a-z0-9_]{8,}|xox[baprs]-[a-z0-9-]{8,})"
)


class AuthorityLevel(str, Enum):
    """Ordered authority identities used by the local gate."""

    SYSTEM = "system"
    VERDICT_CORE = "verdict-core"
    RUFLO = "ruflo"
    OMNIROUTE = "omniroute"
    HUMAN = "human"
    AGENT = "agent"


class MemoryNamespace(str, Enum):
    """Built-in namespaces with conservative retention and trust policies."""

    PATTERNS = "patterns"
    TRAJECTORIES = "trajectories"
    DECISIONS = "decisions"
    FEEDBACK = "feedback"
    CONFIG = "config"
    EVIDENCE = "evidence"
    AUDIT = "audit"


@dataclass(frozen=True)
class MemoryPolicy:
    """Write constraints for one namespace."""

    namespace: str
    min_authority_level: AuthorityLevel = AuthorityLevel.AGENT
    max_ttl_seconds: int = 2_592_000
    default_ttl_seconds: int = 604_800
    min_confidence: float = 0.5
    requires_provenance: bool = True
    allowed_tags: tuple[str, ...] = ()
    forbidden_tags: tuple[str, ...] = ()
    max_key_length: int = 256
    max_value_size_bytes: int = 1_048_576


@dataclass(frozen=True)
class MemoryWriteRequest:
    """A proposed durable memory write."""

    namespace: str
    key: str
    value: Any
    authority: str
    authority_level: AuthorityLevel | None = None
    ttl_seconds: int | None = None
    confidence: float = 1.0
    provenance: dict[str, Any] | None = None
    tags: tuple[str, ...] = ()
    supersedes: str | None = None
    scope: str = "default"
    source: str | None = None
    trust: str = "gated-local-observation"
    metadata: dict[str, Any] | None = None
    sensitivity: str = "internal"
    expires_at: float | None = None
    # Feature 004 (VER-008 #225): the provider the write intends to act for.
    # Additive, optional; unchanged for all pre-004 construction sites.
    provider: str | None = None


@dataclass(frozen=True)
class MemoryWriteResult:
    """Stable result for an evaluated or executed write."""

    allowed: bool
    reason: str
    write_id: str | None = None
    contradiction_detected: bool = False
    contradiction_details: str | None = None
    authority_verified: bool = False
    ttl_set: int | None = None
    record: MemoryRecord | None = None
    event_id: str | None = None
    # Feature 004 (VER-008 #225): additive audit surface for enforcement
    # denials. ``decision_id`` traces a denial back to the feature-003
    # authority receipt (SC-004); ``enforcement_reason`` is the stable
    # flavor of the denial. Both default None so pre-004 callers are untouched.
    decision_id: str | None = None
    enforcement_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "gate_version": GATE_VERSION,
            "allowed": self.allowed,
            "reason": self.reason,
            "write_id": self.write_id,
            "contradiction_detected": self.contradiction_detected,
            "contradiction_details": self.contradiction_details,
            "authority_verified": self.authority_verified,
            "ttl_set": self.ttl_set,
            "record_id": self.record.record_id if self.record else None,
            "event_id": self.event_id,
            "decision_id": self.decision_id,
            "enforcement_reason": self.enforcement_reason,
        }


@dataclass(frozen=True)
class MemoryGateEvent:
    """Redacted, durable audit projection of one gate decision."""

    event_id: str
    timestamp: float
    request: dict[str, Any]
    result: dict[str, Any]


_AUTHORITY_RANK = {
    AuthorityLevel.AGENT: 1,
    AuthorityLevel.HUMAN: 2,
    AuthorityLevel.OMNIROUTE: 3,
    AuthorityLevel.RUFLO: 4,
    AuthorityLevel.VERDICT_CORE: 5,
    AuthorityLevel.SYSTEM: 6,
}


class MemoryGate:
    """Persisted policy gate for writes to a :class:`MemoryPlane`.

    ``MemoryGate(guidance_plane)`` remains accepted for compatibility with
    the earlier experimental API.  New callers should pass an explicit plane
    so the durability boundary is visible and testable.
    """

    def __init__(
        self,
        plane: MemoryPlane | GuidanceControlPlane | None = None,
        guidance_plane: GuidanceControlPlane | None = None,
        *,
        db_path: str | Path | None = None,
    ) -> None:
        if isinstance(plane, GuidanceControlPlane):
            guidance_plane = plane
            plane = None
        self.guidance = guidance_plane
        self._owns_plane = plane is None
        self.plane = plane or MemoryPlane(db_path or _default_memory_path())
        self._policies = _default_policies()
        self._authorities = _default_authorities()

    def close(self) -> None:
        if self._owns_plane:
            self.plane.close()

    def __enter__(self) -> MemoryGate:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def register_policy(self, policy: MemoryPolicy) -> None:
        _validate_policy(policy)
        self._policies[policy.namespace] = policy

    def register_authority(self, authority_id: str, level: AuthorityLevel) -> None:
        if not authority_id or not authority_id.strip():
            raise ValueError("authority_id is required")
        self._authorities[authority_id] = level

    def get_policy(self, namespace: str) -> MemoryPolicy:
        return self._policies.get(namespace, MemoryPolicy(namespace=namespace))

    def _get_authority_level(self, authority: str) -> AuthorityLevel:
        """Derive authority from the registered identifier."""
        return self._authorities.get(authority, AuthorityLevel.AGENT)

    def _get_policy(self, namespace: str) -> MemoryPolicy:
        return self.get_policy(namespace)

    def _check_authority(self, actual: AuthorityLevel, required: AuthorityLevel) -> bool:
        return _AUTHORITY_RANK[actual] >= _AUTHORITY_RANK[required]

    def list_policies(self) -> dict[str, MemoryPolicy]:
        return dict(self._policies)

    def list_authorities(self) -> dict[str, AuthorityLevel]:
        return dict(self._authorities)

    async def evaluate_write(
        self, request: MemoryWriteRequest, *, context: EnforcementContext | None = None
    ) -> MemoryWriteResult:
        """Async-compatible evaluation API retained for older integrations."""

        return self._evaluate(request, context=context)

    def evaluate_write_sync(
        self, request: MemoryWriteRequest, *, context: EnforcementContext | None = None
    ) -> MemoryWriteResult:
        return self._evaluate(request, context=context)

    async def execute_write(
        self, request: MemoryWriteRequest, *, context: EnforcementContext | None = None
    ) -> MemoryWriteResult:
        """Evaluate and, if allowed, persist one record and its audit event."""

        return self.write(request, context=context)

    def write(
        self, request: MemoryWriteRequest, *, context: EnforcementContext | None = None
    ) -> MemoryWriteResult:
        """Evaluate and persist a request without requiring an event loop."""

        result = self._evaluate(request, context=context)
        if not result.allowed:
            event_id = self._persist_event(request, result)
            return MemoryWriteResult(**{**result.__dict__, "event_id": event_id})

        record = self._record_for(request, result.write_id or "")
        stored = (
            self.plane.put_verified(record) if record.authority_verified else self.plane.put(record)
        )
        persisted = MemoryWriteResult(**{**result.__dict__, "record": stored})
        event_id = self._persist_event(request, persisted)
        return MemoryWriteResult(**{**persisted.__dict__, "event_id": event_id})

    def get_write_history(
        self, namespace: str | None = None, limit: int = 100
    ) -> list[MemoryGateEvent]:
        """Read redacted gate events from durable MemoryPlane state."""

        events: list[MemoryGateEvent] = []
        for record in self.plane.records(
            namespace="memory_gate_events", scope=None, include_history=False
        ):
            try:
                payload = json.loads(record.content)
                request = payload.get("request", {})
                if namespace is not None and request.get("namespace") != namespace:
                    continue
                events.append(
                    MemoryGateEvent(
                        event_id=record.record_id,
                        timestamp=record.created_at,
                        request=dict(request),
                        result=dict(payload.get("result", {})),
                    )
                )
            except (TypeError, ValueError, AttributeError):
                continue
        return sorted(events, key=lambda event: (event.timestamp, event.event_id))[-max(limit, 0) :]

    def _evaluate(
        self, request: MemoryWriteRequest, *, context: EnforcementContext | None = None
    ) -> MemoryWriteResult:
        policy = self.get_policy(request.namespace)
        authority = self._get_authority_level(request.authority)
        if request.authority_level is not None and request.authority_level != authority:
            return self._deny("authority_level_mismatch")
        if not self._check_authority(authority, policy.min_authority_level):
            return self._deny(
                f"authority_insufficient:{request.authority}:{policy.min_authority_level.value}"
            )
        if not request.namespace or not request.key or not request.scope:
            return self._deny("namespace_key_scope_required")
        if len(request.key) > policy.max_key_length:
            return self._deny("key_too_long")
        if not isinstance(request.confidence, (int, float)) or not 0 <= request.confidence <= 1:
            return self._deny("confidence_invalid")
        if request.confidence < policy.min_confidence:
            return self._deny("confidence_below_policy")
        ttl = request.ttl_seconds if request.ttl_seconds is not None else policy.default_ttl_seconds
        if ttl <= 0 or ttl > policy.max_ttl_seconds:
            return self._deny("ttl_out_of_range", authority_verified=True)
        if policy.requires_provenance and not request.provenance:
            return self._deny("provenance_required", authority_verified=True)
        if request.supersedes and not request.supersedes.strip():
            return self._deny("supersedes_invalid", authority_verified=True)
        if any(tag in policy.forbidden_tags for tag in request.tags):
            return self._deny("tag_forbidden", authority_verified=True)
        if policy.allowed_tags and any(tag not in policy.allowed_tags for tag in request.tags):
            return self._deny("tag_not_allowed", authority_verified=True)

        safe_value = _redact_value(request.value)
        content = _content_for(safe_value)
        if len(content.encode("utf-8")) > policy.max_value_size_bytes:
            return self._deny("value_too_large", authority_verified=True)
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        current = self.plane.get(request.namespace, request.key, scope=request.scope)
        if current is not None and current.content_hash == digest:
            return MemoryWriteResult(
                allowed=True,
                reason="already_present",
                write_id=current.record_id,
                authority_verified=True,
                ttl_set=ttl,
                record=current,
            )
        if current is not None and request.supersedes not in {current.record_id, current.key}:
            return self._deny(
                "contradiction_requires_explicit_supersession",
                authority_verified=True,
                contradiction=True,
                details=f"active_record:{current.record_id}",
            )
        if request.supersedes and current is None:
            return self._deny("superseded_record_not_active", authority_verified=True)

        write_id = (
            "gate_"
            + hashlib.sha256(
                f"{request.namespace}:{request.scope}:{request.key}:{digest}".encode()
            ).hexdigest()[:24]
        )
        allowed_result = MemoryWriteResult(
            allowed=True, reason="allowed", write_id=write_id, authority_verified=True, ttl_set=ttl
        )
        # Feature 004 (FR-007): the hard decision-bound guard runs LAST, only
        # after MemoryPolicy has already allowed the write. A denial here
        # overrides the allow without weakening any earlier policy. ``context
        # is None`` keeps pre-004 behavior unchanged (the opt-in seam).
        if context is not None:
            enforced = check_enforcement(context, request.provider)
            if not enforced.allowed:
                assert enforced.reason is not None  # invariant from EnforcementResult
                return MemoryWriteResult(
                    allowed=False,
                    reason=enforced.reason.value,
                    authority_verified=True,
                    decision_id=enforced.decision_id,
                    enforcement_reason=enforced.reason.value,
                )
        return allowed_result

    def _record_for(self, request: MemoryWriteRequest, write_id: str) -> MemoryRecord:
        safe_value = _redact_value(request.value)
        content = _content_for(safe_value)
        current = self.plane.get(request.namespace, request.key, scope=request.scope)
        provenance = _redact_value(request.provenance or {})
        if not isinstance(provenance, dict):
            provenance = {"source": "memory-gate"}
        provenance.update(
            {
                "gate_version": GATE_VERSION,
                "authority_id": request.authority,
                "authority_verified_by": "memory_gate_registry",
            }
        )
        return MemoryRecord(
            record_id=write_id,
            namespace=request.namespace,
            key=request.key,
            content=content,
            source=request.source or f"memory-gate:{request.authority}",
            trust=request.trust,
            scope=request.scope,
            metadata={
                **dict(request.metadata or {}),
                "tags": list(request.tags),
                "value_type": type(safe_value).__name__,
            },
            expires_at=request.expires_at
            or time.time()
            + (request.ttl_seconds or self.get_policy(request.namespace).default_ttl_seconds),
            supersedes=current.record_id if current is not None else None,
            authority=request.authority,
            authority_verified=True,
            confidence=float(request.confidence),
            sensitivity=request.sensitivity,
            provenance=provenance,
        )

    def _detect_contradiction(self, request: MemoryWriteRequest) -> str | None:
        """Compatibility helper exposing the durable contradiction decision."""
        result = self._evaluate(request)
        return result.contradiction_details if result.contradiction_detected else None

    @staticmethod
    def _values_contradict(existing: Any, new: Any) -> bool:
        """Conservative structural contradiction check for legacy callers."""
        if isinstance(existing, dict) and isinstance(new, dict):
            return any(
                existing[key] != new[key]
                for key in set(existing) & set(new)
                if key in {"decision", "verdict", "outcome", "result"}
            )
        return False

    @staticmethod
    def _generate_write_id(request: MemoryWriteRequest) -> str:
        content = _content_for(_redact_value(request.value))
        digest = hashlib.sha256(content.encode()).hexdigest()
        return (
            "gate_"
            + hashlib.sha256(
                f"{request.namespace}:{request.scope}:{request.key}:{digest}".encode()
            ).hexdigest()[:24]
        )

    def _persist_event(self, request: MemoryWriteRequest, result: MemoryWriteResult) -> str:
        now = time.time()
        payload = {
            "gate_version": GATE_VERSION,
            "request": {
                "namespace": request.namespace,
                "key": request.key,
                "authority": request.authority,
                "scope": request.scope,
                "ttl_seconds": request.ttl_seconds,
                "confidence": request.confidence,
                "tags": list(request.tags),
                "supersedes": request.supersedes,
                "provenance": _redact_value(request.provenance or {}),
            },
            "result": result.to_dict(),
        }
        event_id = (
            "gate_event_"
            + hashlib.sha256(
                f"{now}:{json.dumps(payload, sort_keys=True, default=str)}".encode()
            ).hexdigest()[:24]
        )
        record = MemoryRecord(
            record_id=event_id,
            namespace="memory_gate_events",
            key=event_id,
            content=json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str),
            source="memory-gate",
            trust="local-audit",
            scope=request.scope,
            metadata={"decision": result.reason, "allowed": result.allowed},
            confidence=1.0,
            sensitivity="internal",
            provenance={"source": "memory-gate", "gate_version": GATE_VERSION},
        )
        self.plane.put(record)
        return event_id

    @staticmethod
    def _deny(
        reason: str,
        *,
        authority_verified: bool = False,
        contradiction: bool = False,
        details: str | None = None,
    ) -> MemoryWriteResult:
        return MemoryWriteResult(
            allowed=False,
            reason=reason,
            authority_verified=authority_verified,
            contradiction_detected=contradiction,
            contradiction_details=details,
        )


class MemoryGovernor:
    """Small convenience facade retaining the historical async API."""

    def __init__(self, gate: MemoryGate):
        self.gate = gate

    async def write_pattern(
        self,
        pattern_name: str,
        pattern_data: dict[str, Any],
        authority: str,
        *,
        confidence: float = 0.8,
        ttl_days: int = 30,
        provenance: dict[str, Any] | None = None,
        tags: tuple[str, ...] = ("learned",),
    ) -> MemoryWriteResult:
        return await self.gate.execute_write(
            MemoryWriteRequest(
                "patterns",
                pattern_name,
                pattern_data,
                authority,
                confidence=confidence,
                ttl_seconds=ttl_days * 86400,
                provenance=provenance,
                tags=tags,
            )
        )

    async def write_decision(
        self,
        decision_id: str,
        decision_data: dict[str, Any],
        authority: str,
        *,
        confidence: float = 0.95,
        provenance: dict[str, Any] | None = None,
    ) -> MemoryWriteResult:
        return await self.gate.execute_write(
            MemoryWriteRequest(
                "decisions",
                decision_id,
                decision_data,
                authority,
                confidence=confidence,
                ttl_seconds=90 * 86400,
                provenance=provenance,
                tags=("routing",),
            )
        )

    async def write_trajectory(
        self,
        trajectory_id: str,
        trajectory_data: dict[str, Any],
        authority: str,
        *,
        outcome: str = "success",
        confidence: float = 0.9,
        provenance: dict[str, Any] | None = None,
    ) -> MemoryWriteResult:
        return await self.gate.execute_write(
            MemoryWriteRequest(
                "trajectories",
                trajectory_id,
                trajectory_data,
                authority,
                confidence=confidence,
                ttl_seconds=3 * 86400,
                provenance=provenance,
                tags=(outcome,),
            )
        )

    async def write_feedback(
        self,
        feedback_id: str,
        feedback_data: dict[str, Any],
        authority: str = "human",
        *,
        confidence: float = 1.0,
        provenance: dict[str, Any] | None = None,
    ) -> MemoryWriteResult:
        return await self.gate.execute_write(
            MemoryWriteRequest(
                "feedback",
                feedback_id,
                feedback_data,
                authority,
                confidence=confidence,
                ttl_seconds=30 * 86400,
                provenance=provenance,
                tags=("correction",),
            )
        )

    async def write_evidence(
        self,
        evidence_id: str,
        evidence_data: dict[str, Any],
        authority: str,
        *,
        confidence: float = 0.95,
        provenance: dict[str, Any] | None = None,
    ) -> MemoryWriteResult:
        return await self.gate.execute_write(
            MemoryWriteRequest(
                "evidence",
                evidence_id,
                evidence_data,
                authority,
                confidence=confidence,
                ttl_seconds=30 * 86400,
                provenance=provenance,
                tags=("verification",),
            )
        )


def _default_memory_path() -> Path:
    import os

    return Path(
        os.getenv("VERDICT_MEMORY_PLANE_PATH", str(Path.home() / ".verdict" / "memory.db"))
    ).expanduser()


def _default_authorities() -> dict[str, AuthorityLevel]:
    return {
        "system": AuthorityLevel.SYSTEM,
        "verdict-core": AuthorityLevel.VERDICT_CORE,
        "ruflo": AuthorityLevel.RUFLO,
        "omniroute": AuthorityLevel.OMNIROUTE,
        "human": AuthorityLevel.HUMAN,
        "agent": AuthorityLevel.AGENT,
        "session_adapter": AuthorityLevel.AGENT,
        "document_adapter": AuthorityLevel.AGENT,
    }


def _default_policies() -> dict[str, MemoryPolicy]:
    day = 86400
    return {
        "patterns": MemoryPolicy(
            "patterns", max_ttl_seconds=90 * day, default_ttl_seconds=30 * day, min_confidence=0.6
        ),
        "trajectories": MemoryPolicy(
            "trajectories", max_ttl_seconds=7 * day, default_ttl_seconds=3 * day
        ),
        "decisions": MemoryPolicy(
            "decisions", AuthorityLevel.VERDICT_CORE, 365 * day, 90 * day, 0.8
        ),
        "feedback": MemoryPolicy("feedback", AuthorityLevel.HUMAN, 365 * day, 30 * day, 0.9),
        "config": MemoryPolicy("config", AuthorityLevel.SYSTEM, 365 * day, 365 * day, 1.0),
        "evidence": MemoryPolicy("evidence", AuthorityLevel.SYSTEM, 365 * day, 30 * day, 0.95),
        "audit": MemoryPolicy("audit", AuthorityLevel.SYSTEM, 7 * 365 * day, 7 * 365 * day, 1.0),
    }


def _validate_policy(policy: MemoryPolicy) -> None:
    if policy.max_ttl_seconds <= 0 or policy.default_ttl_seconds <= 0:
        raise ValueError("policy TTL must be positive")
    if policy.default_ttl_seconds > policy.max_ttl_seconds:
        raise ValueError("policy default TTL exceeds maximum")
    if not 0 <= policy.min_confidence <= 1:
        raise ValueError("policy confidence must be between 0 and 1")


def _content_for(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def _redact_value(value: Any, *, key: str = "") -> Any:
    if _PROMPT_KEY.search(key) or _SECRET_KEY.search(key):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {str(k): _redact_value(v, key=str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_value(item, key=key) for item in value]
    if isinstance(value, tuple):
        return [_redact_value(item, key=key) for item in value]
    if isinstance(value, str):
        return _SECRET_VALUE.sub("[REDACTED]", value)
    return value


__all__ = [
    "GATE_VERSION",
    "AuthorityLevel",
    "MemoryGate",
    "MemoryGateEvent",
    "MemoryGovernor",
    "MemoryNamespace",
    "MemoryPolicy",
    "MemoryWriteRequest",
    "MemoryWriteResult",
]
