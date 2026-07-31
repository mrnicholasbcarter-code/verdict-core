"""Versioned passports for runtime-negotiated tools and protocol peers.

This contract describes the evidence boundary for MCP, A2A, ACP, skills,
transports, and authentication combinations without implementing any protocol
runtime.  A declaration is inventory metadata, an observation is a direct
runtime signal, and negotiation is the evidence required before a subject can
be admitted to policy.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from types import MappingProxyType
from typing import Any

from verdict.capability_passports import (
    CapabilityEvidence,
    CapabilityPassportError,
    CapabilityStatus,
    EvidenceAuthority,
)

RUNTIME_PASSPORT_SCHEMA_VERSION = "1"
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_CAPABILITY_NAME = re.compile(r"^[a-z][a-z0-9_.-]*$")


class RuntimePassportError(CapabilityPassportError):
    """Raised when a runtime passport violates its versioned contract."""


class RuntimeSubjectKind(str, Enum):
    """Runtime subject families covered by the passport contract."""

    TOOL = "tool"
    MCP_SERVER = "mcp_server"
    MCP_RESOURCE = "mcp_resource"
    MCP_PROMPT = "mcp_prompt"
    A2A_PEER = "a2a_peer"
    ACP_AGENT = "acp_agent"
    SKILL = "skill"
    TRANSPORT = "transport"
    AUTH = "auth"


@dataclass(frozen=True)
class RuntimeSubjectIdentity:
    """Non-secret identity for one executable runtime subject."""

    kind: RuntimeSubjectKind
    subject_id: str
    provider: str
    protocol: str
    protocol_version: str
    transport: str
    auth_mode: str
    endpoint_digest: str
    scope: str = "default"
    declared_schema_digest: str | None = None

    def __post_init__(self) -> None:
        if isinstance(self.kind, str):
            try:
                object.__setattr__(self, "kind", RuntimeSubjectKind(self.kind))
            except ValueError as exc:
                raise RuntimePassportError("subject.kind is invalid") from exc
        if not isinstance(self.kind, RuntimeSubjectKind):
            raise RuntimePassportError("subject.kind is invalid")
        for name in (
            "subject_id",
            "provider",
            "protocol",
            "protocol_version",
            "transport",
            "auth_mode",
            "scope",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise RuntimePassportError(f"subject.{name} must be non-empty")
        _validate_digest(self.endpoint_digest, "subject.endpoint_digest")
        if self.declared_schema_digest is not None:
            _validate_digest(self.declared_schema_digest, "subject.declared_schema_digest")

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> RuntimeSubjectIdentity:
        payload = _strict_mapping(
            value,
            required={
                "kind",
                "subject_id",
                "provider",
                "protocol",
                "protocol_version",
                "transport",
                "auth_mode",
                "endpoint_digest",
            },
            optional={"scope", "declared_schema_digest"},
            field_name="subject",
        )
        return cls(
            kind=payload["kind"],
            subject_id=payload["subject_id"],
            provider=payload["provider"],
            protocol=payload["protocol"],
            protocol_version=payload["protocol_version"],
            transport=payload["transport"],
            auth_mode=payload["auth_mode"],
            endpoint_digest=payload["endpoint_digest"],
            scope=payload.get("scope", "default"),
            declared_schema_digest=payload.get("declared_schema_digest"),
        )

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "kind": self.kind.value,
            "subject_id": self.subject_id,
            "provider": self.provider,
            "protocol": self.protocol,
            "protocol_version": self.protocol_version,
            "transport": self.transport,
            "auth_mode": self.auth_mode,
            "endpoint_digest": self.endpoint_digest,
            "scope": self.scope,
        }
        if self.declared_schema_digest is not None:
            payload["declared_schema_digest"] = self.declared_schema_digest
        return payload

    @property
    def key(self) -> str:
        """Stable identity digest that never contains a raw endpoint."""

        return _digest(self.to_dict())


@dataclass(frozen=True)
class RuntimeCapabilityDecision:
    """Explainable result of resolving one negotiated runtime capability."""

    capability: str
    status: CapabilityStatus
    reason: str
    declared: CapabilityEvidence | None = None
    observed: CapabilityEvidence | None = None
    negotiated: CapabilityEvidence | None = None

    @property
    def admitted(self) -> bool:
        return self.status is CapabilityStatus.SUPPORTED


@dataclass(frozen=True)
class RuntimeCapabilityPassport:
    """Fail-closed passport for one non-model runtime subject.

    ``negotiated`` is intentionally separate from ``observed``.  A direct
    observation can show that a peer exists or responds, but policy admission
    requires a fresh direct negotiation for the requested capability.
    """

    subject: RuntimeSubjectIdentity
    qualified_at: datetime
    expires_at: datetime
    declared: Mapping[str, CapabilityEvidence] = field(default_factory=dict)
    observed: Mapping[str, CapabilityEvidence] = field(default_factory=dict)
    negotiated: Mapping[str, CapabilityEvidence] = field(default_factory=dict)
    limitations: tuple[str, ...] = ()
    schema_version: str = RUNTIME_PASSPORT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != RUNTIME_PASSPORT_SCHEMA_VERSION:
            raise RuntimePassportError("schema_version must be '1'")
        qualified_at = _utc(self.qualified_at, "qualified_at")
        expires_at = _utc(self.expires_at, "expires_at")
        if expires_at <= qualified_at:
            raise RuntimePassportError("expires_at must be after qualified_at")
        if not isinstance(self.subject, RuntimeSubjectIdentity):
            raise RuntimePassportError("subject must be a runtime subject identity")
        object.__setattr__(self, "qualified_at", qualified_at)
        object.__setattr__(self, "expires_at", expires_at)
        for name in ("declared", "observed", "negotiated"):
            object.__setattr__(self, name, _evidence_map(getattr(self, name), name))
        if not isinstance(self.limitations, (list, tuple)) or any(
            not isinstance(item, str) or not item.strip() for item in self.limitations
        ):
            raise RuntimePassportError("limitations must contain non-empty strings")
        object.__setattr__(self, "limitations", tuple(self.limitations))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> RuntimeCapabilityPassport:
        payload = _strict_mapping(
            value,
            required={
                "schema_version",
                "subject",
                "qualified_at",
                "expires_at",
                "declared",
                "observed",
                "negotiated",
                "limitations",
            },
            optional=set(),
            field_name="runtime_passport",
        )
        return cls(
            schema_version=payload["schema_version"],
            subject=RuntimeSubjectIdentity.from_dict(payload["subject"]),
            qualified_at=_parse_datetime(payload["qualified_at"], "qualified_at"),
            expires_at=_parse_datetime(payload["expires_at"], "expires_at"),
            declared=_parse_evidence_map(payload["declared"], "declared"),
            observed=_parse_evidence_map(payload["observed"], "observed"),
            negotiated=_parse_evidence_map(payload["negotiated"], "negotiated"),
            limitations=tuple(payload["limitations"]),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "subject": self.subject.to_dict(),
            "qualified_at": _format_datetime(self.qualified_at),
            "expires_at": _format_datetime(self.expires_at),
            "declared": _evidence_dict(self.declared),
            "observed": _evidence_dict(self.observed),
            "negotiated": _evidence_dict(self.negotiated),
            "limitations": list(self.limitations),
        }

    @property
    def digest(self) -> str:
        return _digest(self.to_dict())

    def resolve(self, capability: str, *, at: datetime | None = None) -> RuntimeCapabilityDecision:
        name = _capability_name(capability)
        current = _utc(at or datetime.now(timezone.utc), "at")
        declared = _fresh(self.declared.get(name), current)
        observed_item = self.observed.get(name)
        observed = _fresh(observed_item, current)
        negotiated_item = self.negotiated.get(name)
        negotiated = _fresh(negotiated_item, current)
        if current >= self.expires_at:
            return RuntimeCapabilityDecision(
                name, CapabilityStatus.UNKNOWN, "passport expired", declared, observed, negotiated
            )
        if observed is not None and observed.status is CapabilityStatus.UNSUPPORTED:
            return RuntimeCapabilityDecision(
                name,
                CapabilityStatus.UNSUPPORTED,
                "fresh observation is unsupported",
                declared,
                observed,
                negotiated,
            )
        if negotiated is None:
            reason = "negotiation expired" if negotiated_item is not None else "negotiation missing"
            return RuntimeCapabilityDecision(
                name, CapabilityStatus.UNKNOWN, reason, declared, observed, negotiated_item
            )
        if negotiated.authority not in {EvidenceAuthority.OBSERVED, EvidenceAuthority.VERIFIED}:
            return RuntimeCapabilityDecision(
                name,
                CapabilityStatus.UNKNOWN,
                f"negotiation authority {negotiated.authority.value} is not direct",
                declared,
                observed,
                negotiated,
            )
        return RuntimeCapabilityDecision(
            name,
            negotiated.status,
            f"fresh negotiation is {negotiated.status.value}",
            declared,
            observed,
            negotiated,
        )

    def satisfies(self, required: set[str] | frozenset[str], *, at: datetime | None = None) -> bool:
        """Return true only when every capability has fresh direct negotiation."""

        return all(self.resolve(capability, at=at).admitted for capability in required)


def _strict_mapping(
    value: Mapping[str, Any], *, required: set[str], optional: set[str], field_name: str
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise RuntimePassportError(f"{field_name} must be an object")
    keys = set(value)
    missing = required - keys
    unknown = keys - required - optional
    if missing:
        raise RuntimePassportError(f"{field_name} missing field(s): {', '.join(sorted(missing))}")
    if unknown:
        raise RuntimePassportError(
            f"{field_name} has unknown field(s): {', '.join(sorted(unknown))}"
        )
    return dict(value)


def _evidence_map(
    value: Mapping[str, CapabilityEvidence], field_name: str
) -> Mapping[str, CapabilityEvidence]:
    if not isinstance(value, Mapping):
        raise RuntimePassportError(f"{field_name} must be an object")
    normalized: dict[str, CapabilityEvidence] = {}
    for capability, evidence in value.items():
        name = _capability_name(capability)
        if not isinstance(evidence, CapabilityEvidence):
            raise RuntimePassportError(f"{field_name}.{name} must be capability evidence")
        normalized[name] = evidence
    return MappingProxyType(normalized)


def _parse_evidence_map(value: Any, field_name: str) -> dict[str, CapabilityEvidence]:
    if not isinstance(value, Mapping):
        raise RuntimePassportError(f"{field_name} must be an object")
    return {
        _capability_name(capability): CapabilityEvidence.from_dict(evidence)
        for capability, evidence in value.items()
    }


def _evidence_dict(value: Mapping[str, CapabilityEvidence]) -> dict[str, Any]:
    return {capability: evidence.to_dict() for capability, evidence in sorted(value.items())}


def _fresh(item: CapabilityEvidence | None, at: datetime) -> CapabilityEvidence | None:
    return item if item is not None and item.is_current(at) else None


def _capability_name(value: Any) -> str:
    if not isinstance(value, str) or not _CAPABILITY_NAME.fullmatch(value):
        raise RuntimePassportError("capability names must match ^[a-z][a-z0-9_.-]*$")
    return value


def _validate_digest(value: Any, field_name: str) -> None:
    if not isinstance(value, str) or not _DIGEST.fullmatch(value):
        raise RuntimePassportError(f"{field_name} must be a lowercase sha256 digest")


def _parse_datetime(value: Any, field_name: str) -> datetime:
    if not isinstance(value, str):
        raise RuntimePassportError(f"{field_name} must be an ISO-8601 string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RuntimePassportError(f"{field_name} must be an ISO-8601 string") from exc
    return _utc(parsed, field_name)


def _utc(value: datetime, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise RuntimePassportError(f"{field_name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _format_datetime(value: datetime) -> str:
    return _utc(value, "timestamp").isoformat().replace("+00:00", "Z")


def _digest(value: Mapping[str, Any]) -> str:
    canonical = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


__all__ = [
    "RUNTIME_PASSPORT_SCHEMA_VERSION",
    "RuntimeCapabilityDecision",
    "RuntimeCapabilityPassport",
    "RuntimePassportError",
    "RuntimeSubjectIdentity",
    "RuntimeSubjectKind",
]
