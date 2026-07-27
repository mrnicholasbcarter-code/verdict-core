"""Versioned, fail-closed capability passports for exact model routes.

Catalog claims and runtime observations are deliberately represented as
different evidence maps.  Only a fresh observation can satisfy a hard
capability requirement; a claim is useful provenance but never permission.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from types import MappingProxyType
from typing import Any

CAPABILITY_PASSPORT_SCHEMA_VERSION = "1"

_CAPABILITY_NAME = re.compile(r"^[a-z][a-z0-9_.-]*$")
_SHA256_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")


class CapabilityPassportError(ValueError):
    """Raised when a capability passport violates its public contract."""


class CapabilityStatus(str, Enum):
    """Three-valued capability state used by qualification policy."""

    SUPPORTED = "supported"
    UNSUPPORTED = "unsupported"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class RouteIdentity:
    """Identity of one executable route, not merely a model-family name."""

    gateway: str
    provider: str
    connection: str
    endpoint: str
    protocol: str
    model_id: str
    model_revision: str | None = None

    def __post_init__(self) -> None:
        for name in ("gateway", "provider", "connection", "endpoint", "protocol", "model_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise CapabilityPassportError(f"route_identity.{name} must be non-empty")
        if self.model_revision is not None and (
            not isinstance(self.model_revision, str) or not self.model_revision.strip()
        ):
            raise CapabilityPassportError(
                "route_identity.model_revision must be non-empty when supplied"
            )

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> RouteIdentity:
        payload = _strict_mapping(
            value,
            required={"gateway", "provider", "connection", "endpoint", "protocol", "model_id"},
            optional={"model_revision"},
            field_name="route_identity",
        )
        return cls(
            gateway=payload["gateway"],
            provider=payload["provider"],
            connection=payload["connection"],
            endpoint=payload["endpoint"],
            protocol=payload["protocol"],
            model_id=payload["model_id"],
            model_revision=payload.get("model_revision"),
        )

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "gateway": self.gateway,
            "provider": self.provider,
            "connection": self.connection,
            "endpoint": self.endpoint,
            "protocol": self.protocol,
            "model_id": self.model_id,
        }
        if self.model_revision is not None:
            payload["model_revision"] = self.model_revision
        return payload

    @property
    def key(self) -> str:
        """Stable key that preserves every route-identity component."""

        return _digest(self.to_dict())


@dataclass(frozen=True)
class CapabilityEvidence:
    """One provenance-bearing and expiring capability signal."""

    status: CapabilityStatus
    source: str
    observed_at: datetime
    expires_at: datetime
    confidence: float
    evidence_digest: str
    limitations: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if isinstance(self.status, str):
            try:
                object.__setattr__(self, "status", CapabilityStatus(self.status))
            except ValueError as exc:
                raise CapabilityPassportError("evidence.status is invalid") from exc
        if not isinstance(self.source, str) or not self.source.strip():
            raise CapabilityPassportError("evidence.source must be non-empty")
        observed_at = _utc_datetime(self.observed_at, "evidence.observed_at")
        expires_at = _utc_datetime(self.expires_at, "evidence.expires_at")
        if expires_at <= observed_at:
            raise CapabilityPassportError("evidence.expires_at must be after observed_at")
        object.__setattr__(self, "observed_at", observed_at)
        object.__setattr__(self, "expires_at", expires_at)
        if (
            isinstance(self.confidence, bool)
            or not isinstance(self.confidence, (int, float))
            or not math.isfinite(float(self.confidence))
            or not 0 <= float(self.confidence) <= 1
        ):
            raise CapabilityPassportError("evidence.confidence must be between 0 and 1")
        object.__setattr__(self, "confidence", float(self.confidence))
        if not isinstance(self.evidence_digest, str) or not _SHA256_DIGEST.fullmatch(
            self.evidence_digest
        ):
            raise CapabilityPassportError(
                "evidence.evidence_digest must be a lowercase sha256 digest"
            )
        limitations = _string_tuple(self.limitations, "evidence.limitations")
        object.__setattr__(self, "limitations", limitations)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> CapabilityEvidence:
        payload = _strict_mapping(
            value,
            required={
                "status",
                "source",
                "observed_at",
                "expires_at",
                "confidence",
                "evidence_digest",
            },
            optional={"limitations"},
            field_name="evidence",
        )
        return cls(
            status=payload["status"],
            source=payload["source"],
            observed_at=_parse_datetime(payload["observed_at"], "evidence.observed_at"),
            expires_at=_parse_datetime(payload["expires_at"], "evidence.expires_at"),
            confidence=payload["confidence"],
            evidence_digest=payload["evidence_digest"],
            limitations=tuple(payload.get("limitations", ())),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "source": self.source,
            "observed_at": _format_datetime(self.observed_at),
            "expires_at": _format_datetime(self.expires_at),
            "confidence": self.confidence,
            "evidence_digest": self.evidence_digest,
            "limitations": list(self.limitations),
        }

    def is_current(self, at: datetime) -> bool:
        return _utc_datetime(at, "at") < self.expires_at


@dataclass(frozen=True)
class CapabilityDecision:
    """Explainable result of resolving one capability."""

    capability: str
    status: CapabilityStatus
    reason: str
    claimed: CapabilityEvidence | None = None
    observed: CapabilityEvidence | None = None

    @property
    def admitted(self) -> bool:
        return self.status is CapabilityStatus.SUPPORTED


@dataclass(frozen=True)
class CapabilityPassport:
    """An expiring capability qualification for one exact route."""

    route_identity: RouteIdentity
    qualified_at: datetime
    expires_at: datetime
    claimed: Mapping[str, CapabilityEvidence] = field(default_factory=dict)
    observed: Mapping[str, CapabilityEvidence] = field(default_factory=dict)
    limitations: tuple[str, ...] = ()
    schema_version: str = CAPABILITY_PASSPORT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != CAPABILITY_PASSPORT_SCHEMA_VERSION:
            raise CapabilityPassportError(
                f"schema_version must be {CAPABILITY_PASSPORT_SCHEMA_VERSION!r}"
            )
        qualified_at = _utc_datetime(self.qualified_at, "qualified_at")
        expires_at = _utc_datetime(self.expires_at, "expires_at")
        if expires_at <= qualified_at:
            raise CapabilityPassportError("expires_at must be after qualified_at")
        object.__setattr__(self, "qualified_at", qualified_at)
        object.__setattr__(self, "expires_at", expires_at)
        object.__setattr__(self, "claimed", _evidence_map(self.claimed, "claimed"))
        object.__setattr__(self, "observed", _evidence_map(self.observed, "observed"))
        object.__setattr__(self, "limitations", _string_tuple(self.limitations, "limitations"))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> CapabilityPassport:
        payload = _strict_mapping(
            value,
            required={
                "schema_version",
                "route_identity",
                "qualified_at",
                "expires_at",
                "claimed",
                "observed",
                "limitations",
            },
            optional=set(),
            field_name="capability_passport",
        )
        claimed = _parse_evidence_map(payload["claimed"], "claimed")
        observed = _parse_evidence_map(payload["observed"], "observed")
        return cls(
            schema_version=payload["schema_version"],
            route_identity=RouteIdentity.from_dict(payload["route_identity"]),
            qualified_at=_parse_datetime(payload["qualified_at"], "qualified_at"),
            expires_at=_parse_datetime(payload["expires_at"], "expires_at"),
            claimed=claimed,
            observed=observed,
            limitations=tuple(payload["limitations"]),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "route_identity": self.route_identity.to_dict(),
            "qualified_at": _format_datetime(self.qualified_at),
            "expires_at": _format_datetime(self.expires_at),
            "claimed": {
                capability: evidence.to_dict()
                for capability, evidence in sorted(self.claimed.items())
            },
            "observed": {
                capability: evidence.to_dict()
                for capability, evidence in sorted(self.observed.items())
            },
            "limitations": list(self.limitations),
        }

    @property
    def digest(self) -> str:
        """Canonical integrity digest of the complete passport."""

        return _digest(self.to_dict())

    def resolve(self, capability: str, *, at: datetime | None = None) -> CapabilityDecision:
        """Resolve a capability with observed-only, fail-closed semantics."""

        name = _capability_name(capability)
        current = _utc_datetime(at or datetime.now(timezone.utc), "at")
        claim = self.claimed.get(name)
        observation = self.observed.get(name)
        fresh_claim = claim if claim is not None and claim.is_current(current) else None
        fresh_observation = (
            observation if observation is not None and observation.is_current(current) else None
        )
        if current >= self.expires_at:
            return CapabilityDecision(
                name, CapabilityStatus.UNKNOWN, "passport expired", fresh_claim, fresh_observation
            )
        if fresh_observation is None:
            reason = "observation expired" if observation is not None else "observation missing"
            return CapabilityDecision(
                name, CapabilityStatus.UNKNOWN, reason, fresh_claim, observation
            )
        return CapabilityDecision(
            name,
            fresh_observation.status,
            f"fresh observation is {fresh_observation.status.value}",
            fresh_claim,
            fresh_observation,
        )

    def satisfies(self, required: set[str] | frozenset[str], *, at: datetime | None = None) -> bool:
        """Return true only when every required capability is freshly observed."""

        return all(self.resolve(capability, at=at).admitted for capability in required)


def _strict_mapping(
    value: Mapping[str, Any], *, required: set[str], optional: set[str], field_name: str
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise CapabilityPassportError(f"{field_name} must be an object")
    keys = set(value)
    missing = required - keys
    unknown = keys - required - optional
    if missing:
        raise CapabilityPassportError(
            f"{field_name} missing field(s): {', '.join(sorted(missing))}"
        )
    if unknown:
        raise CapabilityPassportError(
            f"{field_name} has unknown field(s): {', '.join(sorted(unknown))}"
        )
    return dict(value)


def _parse_evidence_map(value: Any, field_name: str) -> dict[str, CapabilityEvidence]:
    if not isinstance(value, Mapping):
        raise CapabilityPassportError(f"{field_name} must be an object")
    return {
        _capability_name(capability): CapabilityEvidence.from_dict(evidence)
        for capability, evidence in value.items()
    }


def _evidence_map(
    value: Mapping[str, CapabilityEvidence], field_name: str
) -> Mapping[str, CapabilityEvidence]:
    if not isinstance(value, Mapping):
        raise CapabilityPassportError(f"{field_name} must be an object")
    normalized: dict[str, CapabilityEvidence] = {}
    for capability, evidence in value.items():
        name = _capability_name(capability)
        if not isinstance(evidence, CapabilityEvidence):
            raise CapabilityPassportError(f"{field_name}.{name} must be capability evidence")
        normalized[name] = evidence
    return MappingProxyType(normalized)


def _capability_name(value: Any) -> str:
    if not isinstance(value, str) or not _CAPABILITY_NAME.fullmatch(value):
        raise CapabilityPassportError("capability names must match ^[a-z][a-z0-9_.-]*$")
    return value


def _string_tuple(value: Any, field_name: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        raise CapabilityPassportError(f"{field_name} must contain non-empty strings")
    return tuple(value)


def _parse_datetime(value: Any, field_name: str) -> datetime:
    if not isinstance(value, str):
        raise CapabilityPassportError(f"{field_name} must be an ISO-8601 string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CapabilityPassportError(f"{field_name} must be an ISO-8601 string") from exc
    return _utc_datetime(parsed, field_name)


def _utc_datetime(value: Any, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise CapabilityPassportError(f"{field_name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _format_datetime(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _digest(value: Mapping[str, Any]) -> str:
    canonical = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return f"sha256:{hashlib.sha256(canonical).hexdigest()}"


__all__ = [
    "CAPABILITY_PASSPORT_SCHEMA_VERSION",
    "CapabilityDecision",
    "CapabilityEvidence",
    "CapabilityPassport",
    "CapabilityPassportError",
    "CapabilityStatus",
    "RouteIdentity",
]
