"""Deterministic, fail-closed compatibility reports for runtime passports.

This module deliberately does not discover or execute runtime subjects.  It
turns already-issued :class:`RuntimeCapabilityPassport` evidence into a safe
compatibility matrix that callers can inspect before selecting a tool or peer.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from verdict.capability_passports import CapabilityEvidence, CapabilityStatus
from verdict.runtime_passports import RuntimeCapabilityDecision, RuntimeCapabilityPassport

RUNTIME_COMPATIBILITY_SCHEMA_VERSION = "1"
_SAFE_LABEL = re.compile(r"[^A-Za-z0-9_.:-]+")


class RuntimeCompatibilityStatus(str, Enum):
    """Policy-neutral classification of one passport against requirements."""

    COMPATIBLE = "compatible"
    DEGRADED = "degraded"
    UNKNOWN = "unknown"
    INCOMPATIBLE = "incompatible"


@dataclass(frozen=True)
class RuntimeCapabilityAssessment:
    """Payload-free status for one required runtime capability."""

    capability: str
    status: str
    reason: str
    declared: str
    observed: str
    negotiated: str

    def to_dict(self) -> dict[str, str]:
        return {
            "capability": self.capability,
            "status": self.status,
            "reason": self.reason,
            "declared": self.declared,
            "observed": self.observed,
            "negotiated": self.negotiated,
        }


@dataclass(frozen=True)
class RuntimeCompatibilityEntry:
    """One exact runtime subject's compatibility result."""

    subject_key: str
    passport_digest: str
    subject_kind: str
    provider: str
    protocol: str
    protocol_version: str
    transport: str
    auth_mode: str
    scope: str
    endpoint_digest: str
    status: RuntimeCompatibilityStatus
    required: tuple[str, ...]
    assessments: tuple[RuntimeCapabilityAssessment, ...]
    limitations: tuple[str, ...]
    remediation: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "subject_key": self.subject_key,
            "passport_digest": self.passport_digest,
            "subject": {
                "kind": self.subject_kind,
                "provider": self.provider,
                "protocol": self.protocol,
                "protocol_version": self.protocol_version,
                "transport": self.transport,
                "auth_mode": self.auth_mode,
                "scope": self.scope,
                "endpoint_digest": self.endpoint_digest,
            },
            "status": self.status.value,
            "required": list(self.required),
            "assessments": [item.to_dict() for item in self.assessments],
            "limitations": list(self.limitations),
            "remediation": list(self.remediation),
        }


@dataclass(frozen=True)
class RuntimeCompatibilityReport:
    """Versioned compatibility matrix with a canonical digest."""

    required: tuple[str, ...]
    checked_at: datetime
    entries: tuple[RuntimeCompatibilityEntry, ...]
    schema_version: str = RUNTIME_COMPATIBILITY_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != RUNTIME_COMPATIBILITY_SCHEMA_VERSION:
            raise ValueError("unsupported runtime compatibility schema version")
        if self.checked_at.tzinfo is None:
            raise ValueError("checked_at must be timezone-aware")
        object.__setattr__(self, "checked_at", self.checked_at.astimezone(timezone.utc))
        object.__setattr__(self, "required", tuple(self.required))
        object.__setattr__(self, "entries", tuple(self.entries))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "checked_at": _format_datetime(self.checked_at),
            "required": list(self.required),
            "entries": [entry.to_dict() for entry in self.entries],
            "digest": self.digest,
        }

    @property
    def digest(self) -> str:
        payload = {
            "schema_version": self.schema_version,
            "checked_at": _format_datetime(self.checked_at),
            "required": list(self.required),
            "entries": [entry.to_dict() for entry in self.entries],
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return "sha256:" + hashlib.sha256(encoded).hexdigest()


def build_runtime_compatibility_report(
    passports: Mapping[str, RuntimeCapabilityPassport],
    required: set[str] | frozenset[str] | Sequence[str],
    *,
    at: datetime | None = None,
) -> RuntimeCompatibilityReport:
    """Build a deterministic matrix from exact passport evidence.

    Mapping keys are treated as caller-side indexes; the report identity is
    always derived from each passport's subject.  Unsupported evidence is
    ``incompatible``; missing, stale, expired, or non-direct evidence is
    ``unknown``.  A compatible passport with explicit limitations is marked
    ``degraded`` so limitations remain visible to callers.
    """

    if not isinstance(passports, Mapping):
        raise ValueError("passports must be a mapping")
    if isinstance(required, (str, bytes)):
        raise ValueError("required must be a collection of capability names")
    try:
        required_names = tuple(sorted(set(required)))
    except TypeError as exc:
        raise ValueError("required must be a collection of capability names") from exc
    current = (at or datetime.now(timezone.utc)).astimezone(timezone.utc)
    entries: list[RuntimeCompatibilityEntry] = []
    for passport in passports.values():
        if not isinstance(passport, RuntimeCapabilityPassport):
            raise ValueError("passports must contain RuntimeCapabilityPassport values")
        assessments = tuple(
            _assessment(passport, capability, current) for capability in required_names
        )
        decisions = tuple(passport.resolve(capability, at=current) for capability in required_names)
        status = _classify(passport, decisions)
        entries.append(
            RuntimeCompatibilityEntry(
                subject_key=passport.subject.key,
                passport_digest=passport.digest,
                subject_kind=passport.subject.kind.value,
                provider=_safe_label(passport.subject.provider),
                protocol=_safe_label(passport.subject.protocol),
                protocol_version=_safe_label(passport.subject.protocol_version),
                transport=_safe_label(passport.subject.transport),
                auth_mode=_safe_label(passport.subject.auth_mode),
                scope=_safe_label(passport.subject.scope),
                endpoint_digest=passport.subject.endpoint_digest,
                status=status,
                required=required_names,
                assessments=assessments,
                limitations=tuple(_safe_label(item, limit=160) for item in passport.limitations),
                remediation=_remediation(status, decisions),
            )
        )
    entries.sort(key=lambda entry: entry.subject_key)
    return RuntimeCompatibilityReport(required_names, current, tuple(entries))


def _assessment(
    passport: RuntimeCapabilityPassport, capability: str, at: datetime
) -> RuntimeCapabilityAssessment:
    decision = passport.resolve(capability, at=at)
    return RuntimeCapabilityAssessment(
        capability=capability,
        status=decision.status.value,
        reason=decision.reason,
        declared=_evidence_state(passport.declared.get(capability), at),
        observed=_evidence_state(passport.observed.get(capability), at),
        negotiated=_evidence_state(passport.negotiated.get(capability), at),
    )


def _evidence_state(item: CapabilityEvidence | None, at: datetime) -> str:
    if item is None:
        return "missing"
    if not item.is_current(at):
        return "expired"
    return item.status.value


def _classify(
    passport: RuntimeCapabilityPassport, decisions: Sequence[RuntimeCapabilityDecision]
) -> RuntimeCompatibilityStatus:
    if any(item.status is CapabilityStatus.UNSUPPORTED for item in decisions):
        return RuntimeCompatibilityStatus.INCOMPATIBLE
    if any(item.status is CapabilityStatus.UNKNOWN for item in decisions):
        return RuntimeCompatibilityStatus.UNKNOWN
    if passport.limitations:
        return RuntimeCompatibilityStatus.DEGRADED
    return RuntimeCompatibilityStatus.COMPATIBLE


def _remediation(
    status: RuntimeCompatibilityStatus, decisions: Sequence[RuntimeCapabilityDecision]
) -> tuple[str, ...]:
    if status is RuntimeCompatibilityStatus.INCOMPATIBLE:
        return ("select a runtime subject with direct support for every required capability",)
    if status is RuntimeCompatibilityStatus.UNKNOWN:
        if any("passport expired" in item.reason for item in decisions):
            return ("requalify the runtime subject and publish a fresh passport",)
        return (
            "perform direct negotiation and publish fresh evidence for each required capability",
        )
    if status is RuntimeCompatibilityStatus.DEGRADED:
        return ("review passport limitations before enabling the runtime subject",)
    return ()


def _safe_label(value: str, *, limit: int = 96) -> str:
    normalized = _SAFE_LABEL.sub("_", value).strip("_")
    return normalized[:limit] or "redacted"


def _format_datetime(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


__all__ = [
    "RUNTIME_COMPATIBILITY_SCHEMA_VERSION",
    "RuntimeCapabilityAssessment",
    "RuntimeCompatibilityEntry",
    "RuntimeCompatibilityReport",
    "RuntimeCompatibilityStatus",
    "build_runtime_compatibility_report",
]
