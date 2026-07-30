"""Versioned, deterministic hard-eligibility policy compilation.

Policy is deliberately separate from ranking.  A candidate must first produce
an ``allow`` decision here; scores, cost and preference can only order that
already-filtered set.  ``unknown`` is an exclusion for execution and is never
coerced to ``allow`` by defaults, averages, or learned values.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from verdict.availability import AvailabilityState
from verdict.capability_passports import (
    CapabilityPassport,
    CapabilityStatus,
    EvidenceAuthority,
    RouteIdentity,
)
from verdict.contracts import TaskSpec

POLICY_SCHEMA_VERSION = "1"
POLICY_VERSION = "policy-1"


class PolicyValidationError(ValueError):
    """Raised when a versioned policy or candidate is malformed."""


class DecisionState(str, Enum):
    """The only hard-policy outcomes."""

    ALLOW = "allow"
    DENY = "deny"
    UNKNOWN = "unknown"


def _utc(value: datetime | None = None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise PolicyValidationError("policy timestamps must be timezone-aware")
    return current.astimezone(timezone.utc)


def _strings(values: Iterable[str], field_name: str) -> frozenset[str]:
    if isinstance(values, (str, bytes)):
        raise PolicyValidationError(f"{field_name} must be an array of strings")
    try:
        result = frozenset(values)
    except TypeError as exc:
        raise PolicyValidationError(f"{field_name} must be an array of strings") from exc
    if any(not isinstance(value, str) or not value.strip() for value in result):
        raise PolicyValidationError(f"{field_name} must contain non-empty strings")
    return result


def _string_sequence(value: Any, field_name: str) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise PolicyValidationError(f"{field_name} must be an array of strings")
    result = tuple(value)
    if any(not isinstance(item, str) or not item.strip() for item in result):
        raise PolicyValidationError(f"{field_name} must contain non-empty strings")
    return result


def _canonical(value: Any) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    except (TypeError, ValueError) as exc:
        raise PolicyValidationError("policy value must be JSON-compatible") from exc


@dataclass(frozen=True)
class PolicyCandidate:
    """The policy-visible facts for one exact executable route."""

    candidate_id: str
    route_identity: RouteIdentity | None = None
    passport: CapabilityPassport | None = None
    availability: str = AvailabilityState.UNKNOWN.value
    evidence_ids: tuple[str, ...] = ()
    cost: float | None = None
    quality_score: float | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    requested_alias: str | None = None
    selected_route: RouteIdentity | None = None
    actual_route: RouteIdentity | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.candidate_id, str) or not self.candidate_id.strip():
            raise PolicyValidationError("candidate_id must be non-empty")
        if not isinstance(self.availability, str) or not self.availability.strip():
            raise PolicyValidationError("availability must be non-empty")
        object.__setattr__(self, "availability", self.availability.lower())
        object.__setattr__(
            self, "evidence_ids", _string_sequence(self.evidence_ids, "evidence_ids")
        )
        for name in ("cost", "quality_score"):
            value = getattr(self, name)
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, (int, float))
            ):
                raise PolicyValidationError(f"{name} must be numeric when supplied")
        if not isinstance(self.metadata, Mapping):
            raise PolicyValidationError("metadata must be an object")
        if self.requested_alias is not None and (
            not isinstance(self.requested_alias, str) or not self.requested_alias.strip()
        ):
            raise PolicyValidationError("requested_alias must be non-empty when supplied")
        for name in ("selected_route", "actual_route"):
            if getattr(self, name) is not None and not isinstance(
                getattr(self, name), RouteIdentity
            ):
                raise PolicyValidationError(f"{name} must be a route identity")
        if (
            self.selected_route is not None
            and self.route_identity is not None
            and self.selected_route != self.route_identity
        ):
            raise PolicyValidationError(
                "selected_route must exactly match route_identity when both are supplied"
            )
        if (
            self.passport is not None
            and self.route_identity is not None
            and self.passport.route_identity != self.route_identity
        ):
            raise PolicyValidationError(
                "candidate route_identity must exactly match passport route_identity"
            )

    @property
    def route_key(self) -> str | None:
        route = self.actual_route or self.route_identity
        return route.key if route is not None else None

    @property
    def effective_route(self) -> RouteIdentity | None:
        """Return actual served identity when known, otherwise selected identity."""
        return self.actual_route or self.route_identity

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> PolicyCandidate:
        if not isinstance(value, Mapping):
            raise PolicyValidationError("policy candidate must be an object")
        allowed = {
            "candidate_id",
            "route_identity",
            "passport",
            "availability",
            "evidence_ids",
            "cost",
            "quality_score",
            "metadata",
            "requested_alias",
            "selected_route",
            "actual_route",
        }
        unknown = set(value) - allowed
        if unknown:
            raise PolicyValidationError(f"candidate has unknown field(s): {sorted(unknown)}")
        route = value.get("route_identity")
        passport = value.get("passport")
        candidate_id = value.get("candidate_id")
        availability = value.get("availability", AvailabilityState.UNKNOWN.value)
        evidence_ids = value.get("evidence_ids", ())
        requested_alias = value.get("requested_alias")
        selected = value.get("selected_route")
        actual = value.get("actual_route")
        if not isinstance(candidate_id, str) or not candidate_id.strip():
            raise PolicyValidationError("candidate_id must be non-empty")
        if not isinstance(availability, str):
            raise PolicyValidationError("availability must be a string")
        return cls(
            candidate_id=candidate_id,
            route_identity=(
                route if isinstance(route, RouteIdentity) else RouteIdentity.from_dict(route)
            )
            if route is not None
            else None,
            passport=(
                passport
                if isinstance(passport, CapabilityPassport)
                else CapabilityPassport.from_dict(passport)
            )
            if passport is not None
            else None,
            availability=availability,
            evidence_ids=_string_sequence(evidence_ids, "evidence_ids"),
            cost=value.get("cost"),
            quality_score=value.get("quality_score"),
            metadata=value.get("metadata", {}),
            requested_alias=requested_alias,
            selected_route=(
                selected
                if isinstance(selected, RouteIdentity)
                else RouteIdentity.from_dict(selected)
            )
            if selected is not None
            else None,
            actual_route=(
                actual if isinstance(actual, RouteIdentity) else RouteIdentity.from_dict(actual)
            )
            if actual is not None
            else None,
        )


@dataclass(frozen=True)
class PolicyDecision:
    """Explainable hard-policy result for one candidate."""

    candidate_id: str
    decision: DecisionState
    policy_version: str
    checked_at: datetime
    reasons: tuple[str, ...] = ()
    remediation: tuple[str, ...] = ()
    evidence_ids: tuple[str, ...] = ()
    route_key: str | None = None
    stale_evidence: bool = False

    def __post_init__(self) -> None:
        try:
            object.__setattr__(self, "decision", DecisionState(self.decision))
        except ValueError as exc:
            raise PolicyValidationError("decision is invalid") from exc
        if not isinstance(self.candidate_id, str) or not self.candidate_id.strip():
            raise PolicyValidationError("decision candidate_id must be non-empty")
        if not isinstance(self.policy_version, str) or not self.policy_version.strip():
            raise PolicyValidationError("decision policy_version must be non-empty")
        if self.checked_at.tzinfo is None:
            raise PolicyValidationError("decision checked_at must be timezone-aware")
        if type(self.stale_evidence) is not bool:
            raise PolicyValidationError("decision stale_evidence must be boolean")

    @property
    def admitted(self) -> bool:
        return self.decision is DecisionState.ALLOW

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "decision": self.decision.value,
            "policy_version": self.policy_version,
            "checked_at": self.checked_at.isoformat().replace("+00:00", "Z"),
            "reasons": list(self.reasons),
            "remediation": list(self.remediation),
            "evidence_ids": list(self.evidence_ids),
            "route_key": self.route_key,
            "stale_evidence": self.stale_evidence,
        }


@dataclass(frozen=True)
class EligibilityCompilation:
    """All candidate decisions, plus the only candidates safe to rank."""

    policy_version: str
    decisions: tuple[PolicyDecision, ...]
    eligible: tuple[PolicyCandidate, ...]
    compiled_at: datetime

    @property
    def exclusions(self) -> tuple[PolicyDecision, ...]:
        return tuple(item for item in self.decisions if not item.admitted)

    def to_dict(self) -> dict[str, Any]:
        return {
            "policy_version": self.policy_version,
            "compiled_at": self.compiled_at.isoformat().replace("+00:00", "Z"),
            "decisions": [item.to_dict() for item in self.decisions],
            "eligible": [item.candidate_id for item in self.eligible],
            "exclusions": [item.to_dict() for item in self.exclusions],
        }

    @property
    def digest(self) -> str:
        canonical = _canonical(self.to_dict())
        return "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()


@dataclass(frozen=True)
class Policy:
    """Immutable v1 policy DSL for hard route eligibility and retry safety."""

    policy_id: str = "default"
    version: str = POLICY_VERSION
    required_capabilities: frozenset[str] = frozenset()
    allowed_providers: frozenset[str] = frozenset()
    denied_providers: frozenset[str] = frozenset()
    allowed_protocols: frozenset[str] = frozenset()
    protected: bool = False
    allow_degraded: bool = False
    allow_stale_evidence: bool = False
    require_route_identity: bool = True
    require_actual_identity: bool = True
    require_fresh_evidence: bool = True
    allow_fallback: bool = True
    retry_safe_required: bool = True
    require_idempotency_key: bool = True
    max_attempts: int = 3
    schema_version: str = POLICY_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != POLICY_SCHEMA_VERSION:
            raise PolicyValidationError(f"schema_version must be {POLICY_SCHEMA_VERSION!r}")
        for name in ("policy_id", "version"):
            if not isinstance(getattr(self, name), str) or not getattr(self, name).strip():
                raise PolicyValidationError(f"{name} must be non-empty")
        for name in (
            "required_capabilities",
            "allowed_providers",
            "denied_providers",
            "allowed_protocols",
        ):
            object.__setattr__(self, name, _strings(getattr(self, name), name))
        if self.protected and self.allow_stale_evidence:
            raise PolicyValidationError("protected policy cannot allow stale evidence")
        if type(self.max_attempts) is not int or self.max_attempts < 1:
            raise PolicyValidationError("max_attempts must be a positive integer")
        for name in (
            "protected",
            "allow_degraded",
            "allow_stale_evidence",
            "require_route_identity",
            "require_actual_identity",
            "require_fresh_evidence",
            "allow_fallback",
            "retry_safe_required",
            "require_idempotency_key",
        ):
            if type(getattr(self, name)) is not bool:
                raise PolicyValidationError(f"{name} must be boolean")

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> Policy:
        if not isinstance(value, Mapping):
            raise PolicyValidationError("policy must be an object")
        allowed = {
            "schema_version",
            "policy_id",
            "version",
            "required_capabilities",
            "allowed_providers",
            "denied_providers",
            "allowed_protocols",
            "protected",
            "allow_degraded",
            "allow_stale_evidence",
            "require_route_identity",
            "require_actual_identity",
            "require_fresh_evidence",
            "allow_fallback",
            "retry_safe_required",
            "require_idempotency_key",
            "max_attempts",
        }
        unknown = set(value) - allowed
        if unknown:
            raise PolicyValidationError(f"policy has unknown field(s): {sorted(unknown)}")
        array_fields = (
            "required_capabilities",
            "allowed_providers",
            "denied_providers",
            "allowed_protocols",
        )
        arrays = {name: _strings(value.get(name, ()), name) for name in array_fields}
        return cls(
            schema_version=value.get("schema_version", POLICY_SCHEMA_VERSION),
            policy_id=value.get("policy_id", "default"),
            version=value.get("version", POLICY_VERSION),
            required_capabilities=arrays["required_capabilities"],
            allowed_providers=arrays["allowed_providers"],
            denied_providers=arrays["denied_providers"],
            allowed_protocols=arrays["allowed_protocols"],
            protected=value.get("protected", False),
            allow_degraded=value.get("allow_degraded", False),
            allow_stale_evidence=value.get("allow_stale_evidence", False),
            require_route_identity=value.get("require_route_identity", True),
            require_actual_identity=value.get("require_actual_identity", True),
            require_fresh_evidence=value.get("require_fresh_evidence", True),
            allow_fallback=value.get("allow_fallback", True),
            retry_safe_required=value.get("retry_safe_required", True),
            require_idempotency_key=value.get("require_idempotency_key", True),
            max_attempts=value.get("max_attempts", 3),
        )

    @classmethod
    def migrate(cls, value: Mapping[str, Any]) -> Policy:
        """Map the legacy tier/floor shape without making it policy authority."""
        if not isinstance(value, Mapping):
            raise PolicyValidationError("legacy policy must be an object")
        payload = dict(value)
        if "schema_version" not in payload:
            payload["schema_version"] = POLICY_SCHEMA_VERSION
        if "policy_floor" in payload and "protected" not in payload:
            payload["protected"] = str(payload.pop("policy_floor")).lower() in {"protected", "high"}
        if "required" in payload and "required_capabilities" not in payload:
            payload["required_capabilities"] = payload.pop("required")
        return cls.from_dict(payload)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "policy_id": self.policy_id,
            "version": self.version,
            "required_capabilities": sorted(self.required_capabilities),
            "allowed_providers": sorted(self.allowed_providers),
            "denied_providers": sorted(self.denied_providers),
            "allowed_protocols": sorted(self.allowed_protocols),
            "protected": self.protected,
            "allow_degraded": self.allow_degraded,
            "allow_stale_evidence": self.allow_stale_evidence,
            "require_route_identity": self.require_route_identity,
            "require_actual_identity": self.require_actual_identity,
            "require_fresh_evidence": self.require_fresh_evidence,
            "allow_fallback": self.allow_fallback,
            "retry_safe_required": self.retry_safe_required,
            "require_idempotency_key": self.require_idempotency_key,
            "max_attempts": self.max_attempts,
        }

    @property
    def digest(self) -> str:
        return "sha256:" + hashlib.sha256(_canonical(self.to_dict()).encode()).hexdigest()

    def evaluate(self, candidate: PolicyCandidate, *, at: datetime | None = None) -> PolicyDecision:
        checked_at = _utc(at)
        reasons: list[str] = []
        remediation: list[str] = []
        unknown = False
        stale = False
        route = candidate.route_identity
        if self.require_route_identity and route is None:
            unknown = True
            reasons.append("exact route identity is unknown")
            remediation.append(
                "resolve gateway, provider, connection, endpoint, and model revision"
            )
        elif route is not None:
            if self.allowed_providers and route.provider not in self.allowed_providers:
                reasons.append(f"provider {route.provider!r} is not allowed")
                remediation.append("add the provider to the policy or select an allowed route")
            if route.provider in self.denied_providers:
                reasons.append(f"provider {route.provider!r} is denied")
            if self.allowed_protocols and route.protocol not in self.allowed_protocols:
                reasons.append(f"protocol {route.protocol!r} is not allowed")
                remediation.append("qualify the required protocol surface")
        if self.protected and self.require_actual_identity and candidate.actual_route is None:
            unknown = True
            reasons.append("actual served route identity is unknown")
            remediation.append(
                "observe and attest the served gateway, provider, connection, and model"
            )
        if reasons and any("not allowed" in item or "is denied" in item for item in reasons):
            return self._decision(candidate, DecisionState.DENY, checked_at, reasons, remediation)

        availability = candidate.availability
        if availability in {
            "denied",
            "unavailable",
            "unauthorized",
            "quota_exhausted",
            "rate_limited",
            "timeout",
            "malformed",
            "policy_denied",
            "capability_mismatch",
        }:
            reasons.append(f"availability is {availability}")
            remediation.append("refresh live availability and resolve the reported failure")
            return self._decision(candidate, DecisionState.DENY, checked_at, reasons, remediation)
        if availability == "degraded" and not (self.allow_degraded and not self.protected):
            reasons.append("degraded availability is not permitted")
            remediation.append("refresh the route or use an explicit non-protected degraded policy")
            return self._decision(candidate, DecisionState.DENY, checked_at, reasons, remediation)
        if availability in {"unknown", "stale", "error", "circuit_open", "locked_out"}:
            unknown = True
            reasons.append(f"availability is {availability}")
            remediation.append("obtain a fresh direct runtime observation")

        if self.required_capabilities:
            passport = candidate.passport
            if passport is None:
                unknown = True
                reasons.append("capability passport is missing")
                remediation.append("qualify the exact route and publish a fresh passport")
            else:
                candidate_route = candidate.effective_route
                if candidate_route is not None and passport.route_identity != candidate_route:
                    unknown = True
                    reasons.append("capability passport identity does not match route")
                    remediation.append("publish a passport for the selected exact route")
                for capability in sorted(self.required_capabilities):
                    resolved = passport.resolve(capability, at=checked_at)
                    if resolved.status is CapabilityStatus.UNSUPPORTED:
                        reasons.append(f"capability {capability!r} is unsupported")
                        remediation.append(f"select a route with a fresh {capability} observation")
                    elif resolved.status is CapabilityStatus.UNKNOWN:
                        stale_item = resolved.observed
                        if (
                            self.allow_stale_evidence
                            and not self.protected
                            and stale_item is not None
                            and stale_item.status is CapabilityStatus.SUPPORTED
                            and stale_item.authority
                            in {EvidenceAuthority.OBSERVED, EvidenceAuthority.VERIFIED}
                        ):
                            stale = True
                            reasons.append(
                                f"capability {capability!r} allowed by explicit stale mode"
                            )
                        else:
                            unknown = True
                            reasons.append(f"capability {capability!r} is unknown")
                            remediation.append(f"refresh direct evidence for {capability}")

        if any("unsupported" in item for item in reasons):
            return self._decision(
                candidate, DecisionState.DENY, checked_at, reasons, remediation, stale
            )
        if unknown:
            return self._decision(
                candidate, DecisionState.UNKNOWN, checked_at, reasons, remediation, stale
            )
        return self._decision(
            candidate,
            DecisionState.ALLOW,
            checked_at,
            reasons or ["all hard predicates passed"],
            remediation,
            stale,
        )

    def _decision(
        self,
        candidate: PolicyCandidate,
        state: DecisionState,
        checked_at: datetime,
        reasons: Sequence[str],
        remediation: Sequence[str],
        stale: bool = False,
    ) -> PolicyDecision:
        return PolicyDecision(
            candidate_id=candidate.candidate_id,
            decision=state,
            policy_version=self.version,
            checked_at=checked_at,
            reasons=tuple(dict.fromkeys(reasons)),
            remediation=tuple(dict.fromkeys(remediation)),
            evidence_ids=candidate.evidence_ids,
            route_key=candidate.route_key,
            stale_evidence=stale,
        )

    def compile(
        self,
        candidates: Iterable[PolicyCandidate],
        *,
        at: datetime | None = None,
        ranking: Mapping[str, float] | None = None,
    ) -> EligibilityCompilation:
        compiled_at = _utc(at)
        candidates_tuple = tuple(candidates)
        ids = [item.candidate_id for item in candidates_tuple]
        if len(ids) != len(set(ids)):
            raise PolicyValidationError("candidate_id values must be unique")
        decisions = tuple(self.evaluate(item, at=compiled_at) for item in candidates_tuple)
        allowed = [
            item
            for item, decision in zip(candidates_tuple, decisions, strict=True)
            if decision.admitted
        ]
        scores = ranking or {}
        # Ranking is applied only after the hard policy pass.
        allowed.sort(
            key=lambda item: (
                -scores.get(item.candidate_id, item.quality_score or 0.0),
                item.cost if item.cost is not None else float("inf"),
                item.candidate_id,
            )
        )
        return EligibilityCompilation(self.version, decisions, tuple(allowed), compiled_at)


def compile_policy(
    task_spec: TaskSpec, *, policy_id: str = "task", version: str = POLICY_VERSION
) -> Policy:
    """Compile the public TaskSpec into hard route predicates."""
    protected = bool(
        task_spec.production_impact
        or task_spec.destructive_operation
        or task_spec.risk in {"high", "critical"}
        or task_spec.privacy in {"restricted", "trusted_upstream"}
    )
    return Policy(
        policy_id=policy_id,
        version=version,
        required_capabilities=frozenset(task_spec.required_capabilities or task_spec.capabilities),
        protected=protected,
        allow_degraded=task_spec.degraded_mode_policy == "allow_with_penalty" and not protected,
        allow_stale_evidence=False,
        require_actual_identity=protected,
    )


def explain_policy(
    policy: Policy, candidates: Iterable[PolicyCandidate], *, at: datetime | None = None
) -> dict[str, Any]:
    """Return deterministic, secret-free explanations without executing a route."""
    return policy.compile(candidates, at=at).to_dict()


def migrate_task_policy(value: Mapping[str, Any]) -> Policy:
    """Compatibility adapter for legacy policy-floor and requirement fields."""
    return Policy.migrate(value)


__all__ = [
    "POLICY_SCHEMA_VERSION",
    "POLICY_VERSION",
    "DecisionState",
    "EligibilityCompilation",
    "Policy",
    "PolicyCandidate",
    "PolicyDecision",
    "PolicyValidationError",
    "compile_policy",
    "explain_policy",
    "migrate_task_policy",
]
