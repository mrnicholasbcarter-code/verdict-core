"""Deterministic, secret-safe explanations for capability qualification.

The report is a projection over an already-issued capability passport.  It
does not turn catalog claims into observations, execute a live request, or
persist payloads.  Its purpose is to give operators and a CLI a stable answer
to: why is this exact route qualified (or not) for these capabilities?
"""

from __future__ import annotations

import hashlib
import json
import urllib.parse
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from verdict.capability_passports import (
    CapabilityEvidence,
    CapabilityPassport,
    CapabilityStatus,
    RouteIdentity,
)

QUALIFICATION_REPORT_VERSION = "1"


@dataclass(frozen=True)
class QualificationEvidenceSummary:
    """A redacted projection of one evidence item."""

    status: CapabilityStatus
    authority: str
    source: str
    observed_at: datetime
    expires_at: datetime
    confidence: float
    evidence_digest: str
    limitations: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "authority": self.authority,
            "source": self.source,
            "observed_at": _format_datetime(self.observed_at),
            "expires_at": _format_datetime(self.expires_at),
            "confidence": self.confidence,
            "evidence_digest": self.evidence_digest,
            "limitations": list(self.limitations),
        }


@dataclass(frozen=True)
class QualificationDecision:
    """The fail-closed decision and its human-readable reason."""

    capability: str
    status: CapabilityStatus
    admitted: bool
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "capability": self.capability,
            "status": self.status.value,
            "admitted": self.admitted,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class QualificationReport:
    """Stable, payload-free qualification explanation for one exact route."""

    route_identity: RouteIdentity
    passport_digest: str
    qualified_at: datetime
    expires_at: datetime
    claimed: Mapping[str, QualificationEvidenceSummary]
    observed: Mapping[str, QualificationEvidenceSummary]
    decisions: tuple[QualificationDecision, ...]
    limitations: tuple[str, ...]
    schema_version: str = QUALIFICATION_REPORT_VERSION

    @property
    def passed(self) -> bool:
        return all(item.admitted for item in self.decisions)

    @property
    def digest(self) -> str:
        payload = self.to_dict(include_digest=False)
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return "sha256:" + hashlib.sha256(canonical).hexdigest()

    def to_dict(self, *, include_digest: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "qualification_report_version": self.schema_version,
            "route_identity": _safe_route(self.route_identity).to_dict(),
            "passport_digest": self.passport_digest,
            "qualified_at": _format_datetime(self.qualified_at),
            "expires_at": _format_datetime(self.expires_at),
            "passed": self.passed,
            "claimed": {key: self.claimed[key].to_dict() for key in sorted(self.claimed)},
            "observed": {key: self.observed[key].to_dict() for key in sorted(self.observed)},
            "decisions": [item.to_dict() for item in self.decisions],
            "limitations": list(self.limitations),
        }
        if include_digest:
            payload["report_digest"] = self.digest
        return payload


def build_qualification_report(
    passport: CapabilityPassport,
    *,
    required_capabilities: Iterable[str] = (),
    at: datetime | None = None,
) -> QualificationReport:
    """Build a deterministic report without making a live call.

    Requirements are sorted and de-duplicated.  With no requirements the
    report remains an inventory projection and is marked passed; callers that
    need admission must provide the hard capabilities they require.
    """

    if not isinstance(passport, CapabilityPassport):
        raise TypeError("passport must be a CapabilityPassport")
    current = _utc_datetime(at or datetime.now(timezone.utc))
    required = tuple(sorted(set(required_capabilities)))
    decisions = tuple(_decision(passport, capability, current) for capability in required)
    return QualificationReport(
        route_identity=passport.route_identity,
        passport_digest=passport.digest,
        qualified_at=passport.qualified_at,
        expires_at=passport.expires_at,
        claimed={key: _summary(item) for key, item in passport.claimed.items()},
        observed={key: _summary(item) for key, item in passport.observed.items()},
        decisions=decisions,
        limitations=(
            (*passport.limitations, "no hard requirements supplied")
            if not required
            else passport.limitations
        ),
    )


def _decision(passport: CapabilityPassport, capability: str, at: datetime) -> QualificationDecision:
    result = passport.resolve(capability, at=at)
    return QualificationDecision(
        capability=result.capability,
        status=result.status,
        admitted=result.admitted,
        reason=result.reason,
    )


def _summary(evidence: CapabilityEvidence) -> QualificationEvidenceSummary:
    return QualificationEvidenceSummary(
        status=evidence.status,
        authority=evidence.authority.value,
        source=evidence.source,
        observed_at=evidence.observed_at,
        expires_at=evidence.expires_at,
        confidence=evidence.confidence,
        evidence_digest=evidence.evidence_digest,
        limitations=evidence.limitations,
    )


def _safe_route(route: RouteIdentity) -> RouteIdentity:
    parsed = urllib.parse.urlsplit(route.endpoint)
    return RouteIdentity(
        gateway=route.gateway,
        provider=route.provider,
        connection=route.connection,
        endpoint=urllib.parse.urlunsplit(
            (parsed.scheme, parsed.netloc.rsplit("@", 1)[-1], parsed.path, "", "")
        ),
        protocol=route.protocol,
        model_id=route.model_id,
        model_revision=route.model_revision,
        account_class=route.account_class,
        endpoint_class=route.endpoint_class,
        transformation_chain=route.transformation_chain,
        fallback_chain=route.fallback_chain,
    )


def _utc_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(timezone.utc)


def _format_datetime(value: datetime) -> str:
    return _utc_datetime(value).isoformat().replace("+00:00", "Z")


__all__ = [
    "QUALIFICATION_REPORT_VERSION",
    "QualificationDecision",
    "QualificationEvidenceSummary",
    "QualificationReport",
    "build_qualification_report",
]
