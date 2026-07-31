"""Evidence-gated shadow, counterfactual, and promotion evaluation.

The evaluation boundary deliberately knows nothing about provider credentials or
raw prompts.  It binds every observation to an exact route, a versioned case,
and a repeat seed.  Transport, configuration, authentication, and capability
failures are retained as operational evidence and never scored as model
quality.  Promotion requires independently verified observations and a fresh
capability passport for the same route.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from types import MappingProxyType
from typing import Any

from verdict.capability_passports import CapabilityPassport, RouteIdentity
from verdict.receipt_store import ReceiptStore

EVALUATION_SCHEMA_VERSION = "1"
_SENSITIVE_PARTS = (
    "prompt",
    "message",
    "completion",
    "output",
    "transcript",
    "argument",
    "credential",
    "authorization",
    "token",
    "secret",
    "password",
    "api_key",
)
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")


class EvaluationError(ValueError):
    """Raised when an evaluation artifact is malformed or unsafe."""


class EvaluationVariant(str, Enum):
    """Context treatment used for a paired evaluation."""

    NO_CONTEXT = "no_context"
    RAW_CONTEXT = "raw_context"
    CONTEXT_PACK = "context_pack"


class EvaluationStatus(str, Enum):
    """Outcome status for one bounded evaluation case."""

    SUCCESS = "success"
    FAILURE = "failure"
    PARTIAL = "partial"
    UNKNOWN = "unknown"


class VerificationStatus(str, Enum):
    """Independent verification result for a task outcome."""

    PASSED = "passed"
    FAILED = "failed"
    NOT_RUN = "not_run"
    UNKNOWN = "unknown"


class EvaluationFailureClass(str, Enum):
    """Failure classes kept separate from model quality."""

    NONE = "none"
    AUTHENTICATION = "authentication"
    CONFIGURATION = "configuration"
    TRANSPORT = "transport"
    QUOTA = "quota"
    CANCELLATION = "cancellation"
    CAPABILITY = "capability"
    QUALITY = "quality"
    VERIFICATION = "verification"
    POLICY = "policy"
    UNKNOWN = "unknown"


class PromotionState(str, Enum):
    """Versioned model-route lifecycle used by the evaluation controller."""

    UNQUALIFIED = "unqualified"
    SHADOW = "shadow"
    CANDIDATE = "candidate"
    CANARY = "canary"
    ACTIVE = "active"
    DEGRADED = "degraded"
    QUARANTINED = "quarantined"
    RETIRED = "retired"


_NON_QUALITY_FAILURES = frozenset(
    {
        EvaluationFailureClass.AUTHENTICATION,
        EvaluationFailureClass.CONFIGURATION,
        EvaluationFailureClass.TRANSPORT,
        EvaluationFailureClass.QUOTA,
        EvaluationFailureClass.CANCELLATION,
        EvaluationFailureClass.CAPABILITY,
        EvaluationFailureClass.POLICY,
    }
)
_SCORE_METRICS = frozenset(
    {"tool_correctness", "safety", "attribution", "contradiction", "injection_resistance"}
)


def normalize_failure_class(value: str | EvaluationFailureClass) -> EvaluationFailureClass:
    """Map qualification/transport labels onto the canonical v1 taxonomy."""

    if isinstance(value, EvaluationFailureClass):
        return value
    normalized = value.strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "": EvaluationFailureClass.NONE,
        "none": EvaluationFailureClass.NONE,
        "ok": EvaluationFailureClass.NONE,
        "success": EvaluationFailureClass.NONE,
        "unauthorized": EvaluationFailureClass.AUTHENTICATION,
        "authentication": EvaluationFailureClass.AUTHENTICATION,
        "forbidden": EvaluationFailureClass.POLICY,
        "permission_denial": EvaluationFailureClass.POLICY,
        "policy_denied": EvaluationFailureClass.POLICY,
        "rate_limited": EvaluationFailureClass.QUOTA,
        "quota_exhausted": EvaluationFailureClass.QUOTA,
        "quota": EvaluationFailureClass.QUOTA,
        "timeout": EvaluationFailureClass.TRANSPORT,
        "disconnect": EvaluationFailureClass.TRANSPORT,
        "transport_error": EvaluationFailureClass.TRANSPORT,
        "upstream_error": EvaluationFailureClass.TRANSPORT,
        "malformed_response": EvaluationFailureClass.TRANSPORT,
        "invalid_json": EvaluationFailureClass.TRANSPORT,
        "schema_invalid": EvaluationFailureClass.CAPABILITY,
        "tool_unavailable": EvaluationFailureClass.CAPABILITY,
        "capability_mismatch": EvaluationFailureClass.CAPABILITY,
        "verification_failed": EvaluationFailureClass.VERIFICATION,
        "quality_failure": EvaluationFailureClass.QUALITY,
        "quality": EvaluationFailureClass.QUALITY,
        "unknown": EvaluationFailureClass.UNKNOWN,
    }
    mapped = aliases.get(normalized)
    if mapped is not None:
        return mapped
    try:
        return EvaluationFailureClass(normalized)
    except ValueError as exc:
        raise EvaluationError(f"unsupported failure class: {value!r}") from exc


def _utc(value: datetime | None = None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise EvaluationError("timestamps must be timezone-aware")
    return current.astimezone(timezone.utc)


def _format_datetime(value: datetime) -> str:
    return _utc(value).isoformat().replace("+00:00", "Z")


def _canonical(value: Any) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    except (TypeError, ValueError) as exc:
        raise EvaluationError("evaluation data must be JSON-compatible") from exc


def _digest(value: Any) -> str:
    return f"sha256:{hashlib.sha256(_canonical(value).encode()).hexdigest()}"


def _require_digest(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise EvaluationError(f"{field_name} must be a sha256 digest")
    return value


def _parse_datetime(value: Any, field_name: str) -> datetime:
    if not isinstance(value, str):
        raise EvaluationError(f"{field_name} must be an ISO-8601 string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise EvaluationError(f"{field_name} must be an ISO-8601 string") from exc
    return _utc(parsed)


def _strict_dict(
    value: Mapping[str, Any], *, required: set[str], optional: set[str], field_name: str
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise EvaluationError(f"{field_name} must be an object")
    keys = set(value)
    missing = required - keys
    unknown = keys - required - optional
    if missing:
        raise EvaluationError(f"{field_name} is missing: {', '.join(sorted(missing))}")
    if unknown:
        raise EvaluationError(f"{field_name} has unknown fields: {', '.join(sorted(unknown))}")
    return dict(value)


def _safe_mapping(value: Mapping[str, Any], field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise EvaluationError(f"{field_name} must be an object")
    clean: dict[str, Any] = {}
    for key, child in value.items():
        if not isinstance(key, str) or not key.strip():
            raise EvaluationError(f"{field_name} keys must be non-empty strings")
        lower = key.lower().replace("-", "_")
        if any(part in lower for part in _SENSITIVE_PARTS):
            raise EvaluationError(f"{field_name}.{key} contains protected content")
        clean[key] = _safe_value(child, f"{field_name}.{key}")
    return MappingProxyType(clean)


def _safe_value(value: Any, field_name: str) -> Any:
    if isinstance(value, Mapping):
        return dict(_safe_mapping(value, field_name))
    if isinstance(value, (list, tuple)):
        return [_safe_value(item, f"{field_name}[]") for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        if isinstance(value, str) and len(value) > 512:
            raise EvaluationError(f"{field_name} is too large")
        if isinstance(value, float) and not math.isfinite(value):
            raise EvaluationError(f"{field_name} must be finite")
        return value
    raise EvaluationError(f"{field_name} must be JSON-compatible")


def _strings(value: Sequence[str], field_name: str) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise EvaluationError(f"{field_name} must be an array of strings")
    result = tuple(value)
    if any(not isinstance(item, str) or not item.strip() for item in result):
        raise EvaluationError(f"{field_name} must contain non-empty strings")
    return result


@dataclass(frozen=True)
class EvaluationCase:
    """A deterministic, payload-free task case in a suite."""

    case_id: str
    task_fingerprint: str
    variant: EvaluationVariant
    seed: int
    token_budget: int
    tool_set: tuple[str, ...] = ()
    context_digest: str | None = None
    heldout: bool = False
    schema_version: str = EVALUATION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != EVALUATION_SCHEMA_VERSION:
            raise EvaluationError("unsupported evaluation case schema version")
        for name in ("case_id", "task_fingerprint"):
            if not isinstance(getattr(self, name), str) or not getattr(self, name).strip():
                raise EvaluationError(f"{name} must be non-empty")
        try:
            object.__setattr__(self, "variant", EvaluationVariant(self.variant))
        except ValueError as exc:
            raise EvaluationError("variant is invalid") from exc
        if isinstance(self.seed, bool) or not isinstance(self.seed, int) or self.seed < 0:
            raise EvaluationError("seed must be a non-negative integer")
        if (
            isinstance(self.token_budget, bool)
            or not isinstance(self.token_budget, int)
            or self.token_budget < 1
        ):
            raise EvaluationError("token_budget must be a positive integer")
        object.__setattr__(self, "tool_set", _strings(self.tool_set, "tool_set"))
        if self.context_digest is not None and (
            not isinstance(self.context_digest, str)
            or _SHA256.fullmatch(self.context_digest) is None
        ):
            raise EvaluationError("context_digest must be a sha256 digest")
        if type(self.heldout) is not bool:
            raise EvaluationError("heldout must be boolean")

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema_version": self.schema_version,
            "case_id": self.case_id,
            "task_fingerprint": self.task_fingerprint,
            "variant": self.variant.value,
            "seed": self.seed,
            "token_budget": self.token_budget,
            "tool_set": list(self.tool_set),
            "heldout": self.heldout,
        }
        if self.context_digest is not None:
            payload["context_digest"] = self.context_digest
        return payload

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> EvaluationCase:
        payload = _strict_dict(
            value,
            required={
                "schema_version",
                "case_id",
                "task_fingerprint",
                "variant",
                "seed",
                "token_budget",
                "tool_set",
                "heldout",
            },
            optional={"context_digest"},
            field_name="evaluation_case",
        )
        return cls(
            schema_version=payload["schema_version"],
            case_id=payload["case_id"],
            task_fingerprint=payload["task_fingerprint"],
            variant=payload["variant"],
            seed=payload["seed"],
            token_budget=payload["token_budget"],
            tool_set=tuple(payload["tool_set"]),
            context_digest=payload.get("context_digest"),
            heldout=payload["heldout"],
        )


@dataclass(frozen=True)
class EvaluationSuite:
    """Versioned manifest describing a paired and repeatable evaluation."""

    suite_id: str
    cases: tuple[EvaluationCase, ...]
    repeat_seeds: tuple[int, ...] = ()
    judge_version: str = "deterministic-v1"
    verifier_version: str = "deterministic-v1"
    schema_version: str = EVALUATION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != EVALUATION_SCHEMA_VERSION:
            raise EvaluationError("unsupported evaluation suite schema version")
        if not isinstance(self.suite_id, str) or not self.suite_id.strip():
            raise EvaluationError("suite_id must be non-empty")
        cases = tuple(self.cases)
        if not cases or any(not isinstance(case, EvaluationCase) for case in cases):
            raise EvaluationError("suite must contain evaluation cases")
        if len({case.case_id for case in cases}) != len(cases):
            raise EvaluationError("suite case IDs must be unique")
        paired: dict[tuple[str, int], EvaluationCase] = {}
        for case in cases:
            pair_key = (case.task_fingerprint, case.seed)
            previous = paired.get(pair_key)
            if previous is not None and (
                previous.token_budget != case.token_budget or previous.tool_set != case.tool_set
            ):
                raise EvaluationError("paired variants must share token budget and tool set")
            paired[pair_key] = case
        object.__setattr__(self, "cases", cases)
        seeds = tuple(self.repeat_seeds) or tuple(sorted({case.seed for case in cases}))
        if any(isinstance(seed, bool) or not isinstance(seed, int) or seed < 0 for seed in seeds):
            raise EvaluationError("repeat_seeds must contain non-negative integers")
        object.__setattr__(self, "repeat_seeds", tuple(sorted(set(seeds))))
        if any(case.seed not in self.repeat_seeds for case in cases):
            raise EvaluationError("every case seed must be listed in repeat_seeds")
        for name in ("judge_version", "verifier_version"):
            if not isinstance(getattr(self, name), str) or not getattr(self, name).strip():
                raise EvaluationError(f"{name} must be non-empty")

    @property
    def digest(self) -> str:
        return _digest(self.to_dict())

    @property
    def heldout_case_ids(self) -> frozenset[str]:
        return frozenset(case.case_id for case in self.cases if case.heldout)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "suite_id": self.suite_id,
            "cases": [case.to_dict() for case in self.cases],
            "repeat_seeds": list(self.repeat_seeds),
            "judge_version": self.judge_version,
            "verifier_version": self.verifier_version,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> EvaluationSuite:
        payload = _strict_dict(
            value,
            required={
                "schema_version",
                "suite_id",
                "cases",
                "repeat_seeds",
                "judge_version",
                "verifier_version",
            },
            optional=set(),
            field_name="evaluation_suite",
        )
        cases = payload["cases"]
        if not isinstance(cases, Sequence) or isinstance(cases, (str, bytes)):
            raise EvaluationError("evaluation_suite.cases must be an array")
        return cls(
            schema_version=payload["schema_version"],
            suite_id=payload["suite_id"],
            cases=tuple(EvaluationCase.from_dict(item) for item in cases),
            repeat_seeds=tuple(payload["repeat_seeds"]),
            judge_version=payload["judge_version"],
            verifier_version=payload["verifier_version"],
        )


@dataclass(frozen=True)
class EvaluationObservation:
    """One route-bound, independently verifiable case result."""

    route_identity: RouteIdentity
    case_id: str
    task_fingerprint: str
    variant: EvaluationVariant
    seed: int
    status: EvaluationStatus
    verification: VerificationStatus
    failure_class: EvaluationFailureClass = EvaluationFailureClass.UNKNOWN
    quality_score: float | None = None
    latency_ms: float | None = None
    output_tokens: int | None = None
    metrics: Mapping[str, float | int | None] = field(default_factory=dict)
    evidence_digest: str = ""
    verification_receipt_id: str | None = None
    verifier_version: str = "deterministic-v1"
    metadata: Mapping[str, Any] = field(default_factory=dict)
    observed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    expires_at: datetime | None = None
    counterfactual: bool = False
    attempted_route_identity: RouteIdentity | None = None
    actual_route_identity: RouteIdentity | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.route_identity, RouteIdentity):
            raise EvaluationError("route_identity is required")
        for name in ("attempted_route_identity", "actual_route_identity"):
            value = getattr(self, name)
            if value is not None and not isinstance(value, RouteIdentity):
                raise EvaluationError(f"{name} must be a route identity")
        if (
            self.actual_route_identity is not None
            and self.actual_route_identity != self.route_identity
        ):
            raise EvaluationError("actual_route_identity must equal route_identity")
        for name in ("case_id", "task_fingerprint", "verifier_version"):
            if not isinstance(getattr(self, name), str) or not getattr(self, name).strip():
                raise EvaluationError(f"{name} must be non-empty")
        try:
            object.__setattr__(self, "variant", EvaluationVariant(self.variant))
            object.__setattr__(self, "status", EvaluationStatus(self.status))
            object.__setattr__(self, "verification", VerificationStatus(self.verification))
        except ValueError as exc:
            raise EvaluationError("observation enum value is invalid") from exc
        try:
            object.__setattr__(self, "failure_class", normalize_failure_class(self.failure_class))
        except (AttributeError, TypeError) as exc:
            raise EvaluationError("observation failure class is invalid") from exc
        observed_at = _utc(self.observed_at)
        expires_at = (
            _utc(self.expires_at)
            if self.expires_at is not None
            else observed_at + timedelta(days=1)
        )
        if expires_at <= observed_at:
            raise EvaluationError("observation expires_at must be after observed_at")
        object.__setattr__(self, "observed_at", observed_at)
        object.__setattr__(self, "expires_at", expires_at)
        if self.verification is VerificationStatus.PASSED and (
            not self.verification_receipt_id or not self.verification_receipt_id.strip()
        ):
            raise EvaluationError("passed verification requires a verification receipt")
        if isinstance(self.seed, bool) or not isinstance(self.seed, int) or self.seed < 0:
            raise EvaluationError("seed must be a non-negative integer")
        if self.quality_score is not None and (
            isinstance(self.quality_score, bool)
            or not isinstance(self.quality_score, (int, float))
            or not math.isfinite(float(self.quality_score))
            or not 0 <= float(self.quality_score) <= 1
        ):
            raise EvaluationError("quality_score must be between 0 and 1")
        for name in ("latency_ms", "output_tokens"):
            value = getattr(self, name)
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0
            ):
                raise EvaluationError(f"{name} must be non-negative")
        metric_values = _safe_mapping(self.metrics, "metrics")
        for name, value in metric_values.items():
            if value is not None and (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or (name in _SCORE_METRICS and not 0 <= float(value) <= 1)
                or (name not in _SCORE_METRICS and float(value) < 0)
            ):
                raise EvaluationError(f"metrics.{name} has an invalid numeric value")
        object.__setattr__(self, "metrics", metric_values)
        if not self.evidence_digest:
            object.__setattr__(self, "evidence_digest", _digest(self._stable_dict()))
        elif _SHA256.fullmatch(self.evidence_digest) is None:
            raise EvaluationError("evidence_digest must be a sha256 digest")
        object.__setattr__(self, "metadata", _safe_mapping(self.metadata, "metadata"))
        if type(self.counterfactual) is not bool:
            raise EvaluationError("counterfactual must be boolean")
        if self.counterfactual and self.failure_class not in _NON_QUALITY_FAILURES | {
            EvaluationFailureClass.QUALITY,
            EvaluationFailureClass.VERIFICATION,
            EvaluationFailureClass.UNKNOWN,
        }:
            raise EvaluationError("invalid counterfactual failure class")

    @property
    def quality_eligible(self) -> bool:
        return (
            not self.counterfactual
            and self.status is EvaluationStatus.SUCCESS
            and self.verification is VerificationStatus.PASSED
            and self.quality_score is not None
            and self.failure_class is EvaluationFailureClass.NONE
        )

    def _stable_dict(self) -> dict[str, Any]:
        payload = {
            "route_identity": self.route_identity.to_dict(),
            "case_id": self.case_id,
            "task_fingerprint": self.task_fingerprint,
            "variant": self.variant.value,
            "seed": self.seed,
            "status": self.status.value,
            "verification": self.verification.value,
            "failure_class": self.failure_class.value,
            "quality_score": self.quality_score,
            "latency_ms": self.latency_ms,
            "output_tokens": self.output_tokens,
            "metrics": dict(self.metrics),
            "observed_at": _format_datetime(self.observed_at),
            "expires_at": _format_datetime(self.expires_at or self.observed_at),
            "verifier_version": self.verifier_version,
            "verification_receipt_id": self.verification_receipt_id,
            "metadata": dict(self.metadata),
            "counterfactual": self.counterfactual,
        }
        if self.attempted_route_identity is not None:
            payload["attempted_route_identity"] = self.attempted_route_identity.to_dict()
        if self.actual_route_identity is not None:
            payload["actual_route_identity"] = self.actual_route_identity.to_dict()
        return payload

    def to_dict(self) -> dict[str, Any]:
        return {
            **self._stable_dict(),
            "evidence_digest": self.evidence_digest,
            "observed_at": _format_datetime(self.observed_at),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> EvaluationObservation:
        payload = _strict_dict(
            value,
            required={
                "route_identity",
                "case_id",
                "task_fingerprint",
                "variant",
                "seed",
                "status",
                "verification",
                "failure_class",
                "quality_score",
                "latency_ms",
                "output_tokens",
                "metrics",
                "evidence_digest",
                "verification_receipt_id",
                "verifier_version",
                "metadata",
                "observed_at",
                "expires_at",
                "counterfactual",
            },
            optional={"attempted_route_identity", "actual_route_identity"},
            field_name="evaluation_observation",
        )
        observation = cls(
            route_identity=RouteIdentity.from_dict(payload["route_identity"]),
            case_id=payload["case_id"],
            task_fingerprint=payload["task_fingerprint"],
            variant=payload["variant"],
            seed=payload["seed"],
            status=payload["status"],
            verification=payload["verification"],
            failure_class=payload["failure_class"],
            quality_score=payload["quality_score"],
            latency_ms=payload["latency_ms"],
            output_tokens=payload["output_tokens"],
            metrics=payload["metrics"],
            evidence_digest=payload["evidence_digest"],
            verification_receipt_id=payload["verification_receipt_id"],
            verifier_version=payload["verifier_version"],
            metadata=payload["metadata"],
            observed_at=_parse_datetime(payload["observed_at"], "observed_at"),
            expires_at=_parse_datetime(payload["expires_at"], "expires_at"),
            counterfactual=payload["counterfactual"],
            attempted_route_identity=(
                None
                if payload.get("attempted_route_identity") is None
                else RouteIdentity.from_dict(payload["attempted_route_identity"])
            ),
            actual_route_identity=(
                None
                if payload.get("actual_route_identity") is None
                else RouteIdentity.from_dict(payload["actual_route_identity"])
            ),
        )
        if not observation.verify():
            raise EvaluationError("observation integrity verification failed")
        return observation

    def verify(self) -> bool:
        return self.evidence_digest == _digest(self._stable_dict())


@dataclass(frozen=True)
class VariantSummary:
    """Aggregate quality and operational evidence for one suite variant."""

    variant: EvaluationVariant
    sample_count: int
    verified_count: int
    quality_count: int
    quality_score: float | None
    confidence_low: float
    confidence_high: float
    non_quality_failure_count: int
    quality_failure_count: int
    average_latency_ms: float | None

    @property
    def coverage(self) -> float:
        return self.verified_count / self.sample_count if self.sample_count else 0.0

    @property
    def non_quality_failure_rate(self) -> float:
        return self.non_quality_failure_count / self.sample_count if self.sample_count else 1.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "variant": self.variant.value,
            "sample_count": self.sample_count,
            "verified_count": self.verified_count,
            "quality_count": self.quality_count,
            "quality_score": self.quality_score,
            "confidence_low": self.confidence_low,
            "confidence_high": self.confidence_high,
            "coverage": self.coverage,
            "non_quality_failure_count": self.non_quality_failure_count,
            "non_quality_failure_rate": self.non_quality_failure_rate,
            "quality_failure_count": self.quality_failure_count,
            "average_latency_ms": self.average_latency_ms,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> VariantSummary:
        payload = _strict_dict(
            value,
            required={
                "variant",
                "sample_count",
                "verified_count",
                "quality_count",
                "quality_score",
                "confidence_low",
                "confidence_high",
                "coverage",
                "non_quality_failure_count",
                "non_quality_failure_rate",
                "quality_failure_count",
                "average_latency_ms",
            },
            optional=set(),
            field_name="variant_summary",
        )
        summary = cls(
            variant=EvaluationVariant(payload["variant"]),
            sample_count=payload["sample_count"],
            verified_count=payload["verified_count"],
            quality_count=payload["quality_count"],
            quality_score=payload["quality_score"],
            confidence_low=payload["confidence_low"],
            confidence_high=payload["confidence_high"],
            non_quality_failure_count=payload["non_quality_failure_count"],
            quality_failure_count=payload["quality_failure_count"],
            average_latency_ms=payload["average_latency_ms"],
        )
        if summary.to_dict() != dict(payload):
            raise EvaluationError("variant summary derived fields are inconsistent")
        return summary


@dataclass(frozen=True)
class EvaluationReport:
    """Replayable aggregate tied to one exact route and suite digest."""

    suite_digest: str
    route_identity: RouteIdentity
    observations: tuple[EvaluationObservation, ...]
    summaries: Mapping[EvaluationVariant, VariantSummary]
    heldout_case_count: int
    heldout_case_ids: tuple[str, ...] = ()
    passport_digest: str | None = None
    generated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    schema_version: str = EVALUATION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != EVALUATION_SCHEMA_VERSION:
            raise EvaluationError("unsupported evaluation report schema version")
        if _SHA256.fullmatch(self.suite_digest) is None:
            raise EvaluationError("suite_digest must be a sha256 digest")
        if not isinstance(self.route_identity, RouteIdentity):
            raise EvaluationError("route_identity is required")
        observations = tuple(self.observations)
        if not observations or any(
            not isinstance(item, EvaluationObservation) for item in observations
        ):
            raise EvaluationError("report must contain observations")
        if any(item.route_identity != self.route_identity for item in observations):
            raise EvaluationError("all observations must use the report route identity")
        if any(not item.verify() for item in observations):
            raise EvaluationError("report contains an observation with invalid integrity")
        object.__setattr__(self, "observations", observations)
        normalized = {EvaluationVariant(key): value for key, value in self.summaries.items()}
        if any(not isinstance(value, VariantSummary) for value in normalized.values()):
            raise EvaluationError("summaries must contain VariantSummary values")
        object.__setattr__(self, "summaries", MappingProxyType(normalized))
        if self.heldout_case_count < 0:
            raise EvaluationError("heldout_case_count must be non-negative")
        if self.passport_digest is not None:
            _require_digest(self.passport_digest, "passport_digest")
        object.__setattr__(
            self,
            "heldout_case_ids",
            _strings(self.heldout_case_ids, "heldout_case_ids") if self.heldout_case_ids else (),
        )
        if len(self.heldout_case_ids) != self.heldout_case_count:
            raise EvaluationError("heldout_case_ids must match heldout_case_count")
        object.__setattr__(self, "generated_at", _utc(self.generated_at))

    @property
    def digest(self) -> str:
        return _digest(self._stable_dict())

    def _stable_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "suite_digest": self.suite_digest,
            "route_identity": self.route_identity.to_dict(),
            "observations": [item.to_dict() for item in self.observations],
            "summaries": {
                key.value: value.to_dict()
                for key, value in sorted(self.summaries.items(), key=lambda item: item[0].value)
            },
            "heldout_case_count": self.heldout_case_count,
            "heldout_case_ids": list(self.heldout_case_ids),
            "passport_digest": self.passport_digest,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            **self._stable_dict(),
            "generated_at": _format_datetime(self.generated_at),
            "report_digest": self.digest,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> EvaluationReport:
        payload = _strict_dict(
            value,
            required={
                "schema_version",
                "suite_digest",
                "route_identity",
                "observations",
                "summaries",
                "heldout_case_count",
                "heldout_case_ids",
                "passport_digest",
                "generated_at",
                "report_digest",
            },
            optional=set(),
            field_name="evaluation_report",
        )
        observations = payload["observations"]
        summaries = payload["summaries"]
        if not isinstance(observations, Sequence) or isinstance(observations, (str, bytes)):
            raise EvaluationError("evaluation_report.observations must be an array")
        if not isinstance(summaries, Mapping):
            raise EvaluationError("evaluation_report.summaries must be an object")
        report = cls(
            schema_version=payload["schema_version"],
            suite_digest=_require_digest(payload["suite_digest"], "suite_digest"),
            route_identity=RouteIdentity.from_dict(payload["route_identity"]),
            observations=tuple(EvaluationObservation.from_dict(item) for item in observations),
            summaries={
                EvaluationVariant(key): VariantSummary.from_dict(item)
                for key, item in summaries.items()
            },
            heldout_case_count=payload["heldout_case_count"],
            heldout_case_ids=tuple(payload["heldout_case_ids"]),
            passport_digest=(
                None
                if payload["passport_digest"] is None
                else _require_digest(payload["passport_digest"], "passport_digest")
            ),
            generated_at=_parse_datetime(payload["generated_at"], "generated_at"),
        )
        if payload["report_digest"] != report.digest:
            raise EvaluationError("report integrity verification failed")
        return report

    def verify(self) -> bool:
        return self.digest == _digest(self._stable_dict()) and all(
            item.verify() for item in self.observations
        )

    def is_fresh(self, *, at: datetime | None = None, max_age_seconds: float = 86_400) -> bool:
        """Return whether every observation is within the configured evidence age."""
        if max_age_seconds < 0:
            raise EvaluationError("max_age_seconds must be non-negative")
        current = _utc(at)
        return all(
            item.expires_at is not None
            and item.observed_at <= current < item.expires_at
            and (current - item.observed_at).total_seconds() <= max_age_seconds
            for item in self.observations
        )


@dataclass(frozen=True)
class PromotionPolicy:
    """Deterministic thresholds for candidate/canary/active promotion."""

    policy_id: str = "promotion-v1"
    minimum_coverage: float = 1.0
    minimum_verified_samples: int = 1
    minimum_quality_score: float = 0.7
    minimum_holdout_quality: float = 0.7
    minimum_context_lift: float = 0.0
    maximum_non_quality_failure_rate: float = 0.0
    maximum_evidence_age_seconds: float = 86_400
    require_heldout: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.policy_id, str) or not self.policy_id.strip():
            raise EvaluationError("policy_id must be non-empty")
        for name in (
            "minimum_coverage",
            "minimum_quality_score",
            "minimum_holdout_quality",
            "minimum_context_lift",
            "maximum_non_quality_failure_rate",
            "maximum_evidence_age_seconds",
        ):
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
            ):
                raise EvaluationError(f"{name} must be finite")
            if name == "maximum_evidence_age_seconds" and float(value) < 0:
                raise EvaluationError(f"{name} must be non-negative")
            if (
                name not in {"minimum_context_lift", "maximum_evidence_age_seconds"}
                and not 0 <= float(value) <= 1
            ):
                raise EvaluationError(f"{name} must be between 0 and 1")
        if (
            isinstance(self.minimum_verified_samples, bool)
            or not isinstance(self.minimum_verified_samples, int)
            or self.minimum_verified_samples < 1
        ):
            raise EvaluationError("minimum_verified_samples must be positive")
        if type(self.require_heldout) is not bool:
            raise EvaluationError("require_heldout must be boolean")

    @property
    def digest(self) -> str:
        return _digest(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "policy_id": self.policy_id,
            "minimum_coverage": self.minimum_coverage,
            "minimum_verified_samples": self.minimum_verified_samples,
            "minimum_quality_score": self.minimum_quality_score,
            "minimum_holdout_quality": self.minimum_holdout_quality,
            "minimum_context_lift": self.minimum_context_lift,
            "maximum_non_quality_failure_rate": self.maximum_non_quality_failure_rate,
            "maximum_evidence_age_seconds": self.maximum_evidence_age_seconds,
            "require_heldout": self.require_heldout,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> PromotionPolicy:
        payload = _strict_dict(
            value,
            required={
                "policy_id",
                "minimum_coverage",
                "minimum_verified_samples",
                "minimum_quality_score",
                "minimum_holdout_quality",
                "minimum_context_lift",
                "maximum_non_quality_failure_rate",
                "maximum_evidence_age_seconds",
                "require_heldout",
            },
            optional=set(),
            field_name="promotion_policy",
        )
        return cls(**payload)


@dataclass(frozen=True)
class PromotionDecision:
    """Immutable allow/deny decision and reasons for a lifecycle transition."""

    allowed: bool
    target_state: PromotionState
    route_identity: RouteIdentity
    report_digest: str
    suite_digest: str
    passport_digest: str
    policy_digest: str
    reasons: tuple[str, ...]
    checked_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self) -> None:
        if type(self.allowed) is not bool:
            raise EvaluationError("allowed must be boolean")
        if not isinstance(self.route_identity, RouteIdentity):
            raise EvaluationError("route_identity is required")
        for name in ("report_digest", "suite_digest", "passport_digest", "policy_digest"):
            if _SHA256.fullmatch(getattr(self, name)) is None:
                raise EvaluationError(f"{name} must be a sha256 digest")
        object.__setattr__(self, "target_state", PromotionState(self.target_state))
        object.__setattr__(
            self, "reasons", _strings(self.reasons, "reasons") if self.reasons else ()
        )
        object.__setattr__(self, "checked_at", _utc(self.checked_at))

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "target_state": self.target_state.value,
            "route_identity": self.route_identity.to_dict(),
            "report_digest": self.report_digest,
            "suite_digest": self.suite_digest,
            "passport_digest": self.passport_digest,
            "policy_digest": self.policy_digest,
            "reasons": list(self.reasons),
            "checked_at": _format_datetime(self.checked_at),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> PromotionDecision:
        payload = _strict_dict(
            value,
            required={
                "allowed",
                "target_state",
                "route_identity",
                "report_digest",
                "suite_digest",
                "passport_digest",
                "policy_digest",
                "reasons",
                "checked_at",
            },
            optional=set(),
            field_name="promotion_decision",
        )
        return cls(
            allowed=payload["allowed"],
            target_state=payload["target_state"],
            route_identity=RouteIdentity.from_dict(payload["route_identity"]),
            report_digest=_require_digest(payload["report_digest"], "report_digest"),
            suite_digest=_require_digest(payload["suite_digest"], "suite_digest"),
            passport_digest=_require_digest(payload["passport_digest"], "passport_digest"),
            policy_digest=_require_digest(payload["policy_digest"], "policy_digest"),
            reasons=tuple(payload["reasons"]),
            checked_at=_parse_datetime(payload["checked_at"], "checked_at"),
        )


@dataclass(frozen=True)
class CounterfactualResult:
    """A replay-only alternative scored without granting promotion authority."""

    source_receipt_id: str
    task_fingerprint: str
    observed_route: RouteIdentity
    counterfactual_route: RouteIdentity
    variant: EvaluationVariant
    quality_score: float | None
    verification: VerificationStatus
    failure_class: EvaluationFailureClass
    evidence_digest: str
    training_eligible: bool = False
    promotion_eligible: bool = False

    def __post_init__(self) -> None:
        if (
            not isinstance(self.source_receipt_id, str)
            or not isinstance(self.task_fingerprint, str)
            or not self.source_receipt_id.strip()
            or not self.task_fingerprint.strip()
        ):
            raise EvaluationError("counterfactual identifiers must be non-empty")
        if not isinstance(self.observed_route, RouteIdentity) or not isinstance(
            self.counterfactual_route, RouteIdentity
        ):
            raise EvaluationError("counterfactual routes are required")
        if self.observed_route == self.counterfactual_route:
            raise EvaluationError("counterfactual route must differ from observed route")
        try:
            object.__setattr__(self, "variant", EvaluationVariant(self.variant))
            object.__setattr__(self, "verification", VerificationStatus(self.verification))
            object.__setattr__(self, "failure_class", normalize_failure_class(self.failure_class))
        except ValueError as exc:
            raise EvaluationError("counterfactual enum value is invalid") from exc
        if self.training_eligible or self.promotion_eligible:
            raise EvaluationError("counterfactual evidence cannot authorize training or promotion")

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": "counterfactual",
            "source_receipt_id": self.source_receipt_id,
            "task_fingerprint": self.task_fingerprint,
            "observed_route": self.observed_route.to_dict(),
            "counterfactual_route": self.counterfactual_route.to_dict(),
            "variant": self.variant.value,
            "quality_score": self.quality_score,
            "verification": self.verification.value,
            "failure_class": self.failure_class.value,
            "evidence_digest": self.evidence_digest,
            "training_eligible": False,
            "promotion_eligible": False,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> CounterfactualResult:
        payload = _strict_dict(
            value,
            required={
                "kind",
                "source_receipt_id",
                "task_fingerprint",
                "observed_route",
                "counterfactual_route",
                "variant",
                "quality_score",
                "verification",
                "failure_class",
                "evidence_digest",
                "training_eligible",
                "promotion_eligible",
            },
            optional=set(),
            field_name="counterfactual_result",
        )
        if payload["kind"] != "counterfactual":
            raise EvaluationError("counterfactual_result.kind is invalid")
        return cls(
            source_receipt_id=payload["source_receipt_id"],
            task_fingerprint=payload["task_fingerprint"],
            observed_route=RouteIdentity.from_dict(payload["observed_route"]),
            counterfactual_route=RouteIdentity.from_dict(payload["counterfactual_route"]),
            variant=payload["variant"],
            quality_score=payload["quality_score"],
            verification=payload["verification"],
            failure_class=payload["failure_class"],
            evidence_digest=_require_digest(payload["evidence_digest"], "evidence_digest"),
            training_eligible=payload["training_eligible"],
            promotion_eligible=payload["promotion_eligible"],
        )


def _quality_interval(values: Sequence[float]) -> tuple[float, float]:
    """Return a normal 95% interval for the mean bounded quality score."""
    if not values:
        return (0.0, 0.0)
    z = 1.96
    mean = sum(values) / len(values)
    if len(values) == 1:
        return (mean, mean)
    variance = sum((value - mean) ** 2 for value in values) / (len(values) - 1)
    margin = z * math.sqrt(variance / len(values))
    return (max(0.0, mean - margin), min(1.0, mean + margin))


def build_evaluation_report(
    suite: EvaluationSuite,
    route_identity: RouteIdentity,
    observations: Sequence[EvaluationObservation],
    *,
    passport_digest: str | None = None,
    generated_at: datetime | None = None,
) -> EvaluationReport:
    """Build a paired report and reject missing, duplicate, or mismatched cases."""

    expected = {(case.case_id, case.variant, case.seed): case for case in suite.cases}
    received: dict[tuple[str, EvaluationVariant, int], EvaluationObservation] = {}
    for observation in observations:
        if observation.route_identity != route_identity:
            raise EvaluationError("observation route identity does not match suite route")
        key = (observation.case_id, observation.variant, observation.seed)
        case = expected.get(key)
        if case is None:
            raise EvaluationError(f"observation is not declared by suite: {key}")
        if observation.task_fingerprint != case.task_fingerprint:
            raise EvaluationError("observation task fingerprint does not match suite")
        if key in received:
            raise EvaluationError(f"duplicate observation: {key}")
        received[key] = observation
    missing = set(expected) - set(received)
    if missing:
        raise EvaluationError(f"evaluation is incomplete; missing {sorted(missing)!r}")

    summaries: dict[EvaluationVariant, VariantSummary] = {}
    for variant in EvaluationVariant:
        items = [item for item in received.values() if item.variant is variant]
        if not items:
            continue
        verified = [
            item
            for item in items
            if item.status is EvaluationStatus.SUCCESS
            and item.verification is VerificationStatus.PASSED
            and item.verification_receipt_id
            and item.failure_class is EvaluationFailureClass.NONE
        ]
        quality = [item for item in items if item.quality_eligible]
        quality_values = [item.quality_score for item in quality if item.quality_score is not None]
        quality_score = sum(quality_values) / len(quality_values) if quality_values else None
        low, high = _quality_interval([float(value) for value in quality_values])
        non_quality = sum(item.failure_class in _NON_QUALITY_FAILURES for item in items)
        quality_failures = sum(
            item.failure_class
            in {EvaluationFailureClass.QUALITY, EvaluationFailureClass.VERIFICATION}
            for item in items
        )
        latencies = [float(item.latency_ms) for item in items if item.latency_ms is not None]
        summaries[variant] = VariantSummary(
            variant=variant,
            sample_count=len(items),
            verified_count=len(verified),
            quality_count=len(quality),
            quality_score=quality_score,
            confidence_low=low,
            confidence_high=high,
            non_quality_failure_count=non_quality,
            quality_failure_count=quality_failures,
            average_latency_ms=sum(latencies) / len(latencies) if latencies else None,
        )
    return EvaluationReport(
        suite_digest=suite.digest,
        route_identity=route_identity,
        observations=tuple(observations),
        summaries=summaries,
        heldout_case_count=sum(case.heldout for case in suite.cases),
        heldout_case_ids=tuple(sorted(suite.heldout_case_ids)),
        passport_digest=passport_digest,
        generated_at=generated_at or datetime.now(timezone.utc),
    )


class EvaluationController:
    """Persist reports and enforce promotion, kill-switch, and rollback gates."""

    def __init__(
        self, receipt_store: ReceiptStore | None = None, *, scope: str = "evaluation"
    ) -> None:
        if not scope.strip():
            raise EvaluationError("scope must be non-empty")
        self.receipts = receipt_store or ReceiptStore(":memory:", strict_scope=True)
        self.scope = scope
        self._states: dict[str, PromotionState] = {}
        self._kill_switches: dict[str, str] = {}
        self._report_receipts: dict[str, str] = {}
        self._reports: dict[str, EvaluationReport] = {}
        self._decisions: dict[tuple[str, PromotionState, str], PromotionDecision] = {}
        self._known_good: dict[str, str] = {}
        self._load_lifecycle()

    def _load_lifecycle(self) -> None:
        """Reconstruct report and lifecycle indexes from durable receipts."""
        records = self.receipts.query_receipts(scope=self.scope, limit=100_000)
        for record in reversed(records):
            payload = record.payload
            if not isinstance(payload, dict):
                continue
            if record.receipt_type == "manifest" and payload.get("kind") == "evaluation_report":
                report = payload.get("report")
                if isinstance(report, dict) and isinstance(report.get("report_digest"), str):
                    try:
                        decoded = EvaluationReport.from_dict(report)
                    except EvaluationError:
                        continue
                    self._report_receipts[decoded.digest] = record.receipt_id
                    self._reports[decoded.digest] = decoded
                continue
            if record.receipt_type != "outcome":
                continue
            route_key = payload.get("route_key")
            if not isinstance(route_key, str):
                continue
            kind = payload.get("kind")
            if kind == "kill_switch":
                self._kill_switches[route_key] = str(payload.get("reason", "persisted kill switch"))
                self._states[route_key] = PromotionState.QUARANTINED
            elif kind == "rollback":
                if route_key not in self._kill_switches:
                    self._states[route_key] = PromotionState(
                        payload.get("restored_state", PromotionState.DEGRADED.value)
                    )
                    if isinstance(payload.get("known_good_report_digest"), str):
                        self._known_good[route_key] = payload["known_good_report_digest"]
            elif kind == "promotion" and isinstance(payload.get("state"), str):
                try:
                    self._states[route_key] = PromotionState(payload["state"])
                except ValueError:
                    continue
                decision_payload = payload.get("decision")
                if isinstance(decision_payload, dict):
                    try:
                        decision = PromotionDecision.from_dict(decision_payload)
                    except EvaluationError:
                        continue
                    self._decisions[(route_key, decision.target_state, decision.policy_digest)] = (
                        decision
                    )

    def record_report(self, report: EvaluationReport) -> str:
        """Store a redacted report as an immutable evaluation receipt."""
        receipt = self.receipts.put_receipt(
            "manifest",
            self.scope,
            {
                "kind": "evaluation_report",
                "route_key": report.route_identity.key,
                "report": report.to_dict(),
            },
            provenance={"source": "verdict_evaluation", "version": EVALUATION_SCHEMA_VERSION},
            idempotency_key=report.digest,
        )
        self._report_receipts[report.digest] = receipt.receipt_id
        self._reports[report.digest] = report
        return receipt.receipt_id

    def _verify_decision(self, decision: PromotionDecision) -> None:
        report = self._reports.get(decision.report_digest)
        if report is None:
            raise EvaluationError("promotion report is not durably recorded")
        if report.route_identity != decision.route_identity:
            raise EvaluationError("promotion decision route identity does not match report")
        if report.suite_digest != decision.suite_digest:
            raise EvaluationError("promotion decision suite digest does not match report")
        if report.passport_digest != decision.passport_digest:
            raise EvaluationError("promotion decision passport digest does not match report")
        stored = self._decisions.get(
            (decision.route_identity.key, decision.target_state, decision.policy_digest)
        )
        if stored is None:
            raise EvaluationError("promotion decision was not issued by this controller")
        if stored != decision:
            raise EvaluationError("promotion decision does not match stored decision")

    def evaluate_promotion(
        self,
        report: EvaluationReport,
        policy: PromotionPolicy,
        *,
        passport: CapabilityPassport | None,
        required_capabilities: Sequence[str] = (),
        target_state: PromotionState = PromotionState.CANDIDATE,
    ) -> PromotionDecision:
        reasons: list[str] = []
        route_key = report.route_identity.key
        report_receipt_id = self._report_receipts.get(report.digest)
        if report_receipt_id is None:
            reasons.append("evaluation report is not durably recorded")
        if not report.verify():
            reasons.append("report integrity verification failed")
        if not report.is_fresh(max_age_seconds=policy.maximum_evidence_age_seconds):
            reasons.append("evaluation evidence is stale")
        if route_key in self._kill_switches:
            reasons.append("route is kill-switched")
        if passport is None:
            reasons.append("missing capability passport")
        elif passport.route_identity != report.route_identity:
            reasons.append("capability passport route identity mismatch")
        elif not passport.satisfies(set(required_capabilities)):
            reasons.append("required capabilities are not freshly observed")
        elif report.passport_digest != passport.digest:
            reasons.append("capability passport digest does not match report")
        candidate = report.summaries.get(EvaluationVariant.CONTEXT_PACK)
        baseline = report.summaries.get(EvaluationVariant.NO_CONTEXT)
        if candidate is None:
            reasons.append("context_pack variant is missing")
        else:
            if candidate.coverage < policy.minimum_coverage:
                reasons.append("candidate verification coverage is below policy")
            if candidate.verified_count < policy.minimum_verified_samples:
                reasons.append("candidate has too few independently verified samples")
            if (
                candidate.quality_score is None
                or candidate.quality_score < policy.minimum_quality_score
            ):
                reasons.append("candidate quality score is below policy")
            if candidate.non_quality_failure_rate > policy.maximum_non_quality_failure_rate:
                reasons.append("candidate has non-quality operational failures")
        if policy.require_heldout and report.heldout_case_count < 1:
            reasons.append("heldout evaluation coverage is missing")
        heldout = [
            item
            for item in report.observations
            if item.case_id in report.heldout_case_ids
            if item.variant is EvaluationVariant.CONTEXT_PACK and item.quality_eligible
        ]
        if policy.require_heldout and (
            not heldout
            or sum(float(item.quality_score or 0) for item in heldout) / len(heldout)
            < policy.minimum_holdout_quality
        ):
            reasons.append("heldout quality score is below policy")
        if (
            baseline is not None
            and candidate is not None
            and candidate.quality_score is not None
            and baseline.quality_score is not None
            and candidate.quality_score - baseline.quality_score < policy.minimum_context_lift
        ):
            reasons.append("candidate does not meet required context lift")
        try:
            target = PromotionState(target_state)
        except ValueError as exc:
            raise EvaluationError("target_state is invalid") from exc
        if target not in {
            PromotionState.SHADOW,
            PromotionState.CANDIDATE,
            PromotionState.CANARY,
            PromotionState.ACTIVE,
        }:
            reasons.append("target state is not promotable")
        passport_digest = passport.digest if passport is not None else "sha256:" + "0" * 64
        decision = PromotionDecision(
            allowed=not reasons,
            target_state=target,
            route_identity=report.route_identity,
            report_digest=report.digest,
            suite_digest=report.suite_digest,
            passport_digest=passport_digest,
            policy_digest=policy.digest,
            reasons=tuple(reasons),
        )
        if decision.allowed:
            self._decisions[(route_key, target, policy.digest)] = decision
        return decision

    def counterfactual(
        self,
        *,
        source_receipt_id: str,
        task_fingerprint: str,
        observed_route: RouteIdentity,
        counterfactual_route: RouteIdentity,
        quality_score: float | None,
        verification: VerificationStatus,
        failure_class: EvaluationFailureClass = EvaluationFailureClass.UNKNOWN,
    ) -> CounterfactualResult:
        """Create and durably link a replay-only counterfactual result."""
        result = counterfactual_from_receipt(
            source_receipt_id,
            task_fingerprint,
            observed_route,
            counterfactual_route,
            receipt_store=self.receipts,
            scope=self.scope,
            quality_score=quality_score,
            verification=verification,
            failure_class=failure_class,
        )
        self.receipts.put_receipt(
            "outcome",
            self.scope,
            {
                "kind": "counterfactual",
                "source_receipt_id": source_receipt_id,
                "result": result.to_dict(),
            },
            parent_receipt_id=source_receipt_id,
            provenance={"source": "verdict_evaluation", "version": EVALUATION_SCHEMA_VERSION},
            idempotency_key=f"counterfactual:{result.evidence_digest}",
        )
        return result

    def promote(self, decision: PromotionDecision) -> PromotionDecision:
        """Apply an already-approved transition and persist its decision."""
        if not decision.allowed:
            raise EvaluationError("cannot apply a denied promotion decision")
        route_key = decision.route_identity.key
        self._verify_decision(decision)
        if route_key in self._kill_switches:
            raise EvaluationError("cannot promote a kill-switched route")
        if self._report_receipts.get(decision.report_digest) is None:
            raise EvaluationError("promotion report is not durably recorded")
        current = self._states.get(route_key, PromotionState.UNQUALIFIED)
        allowed_predecessors = {
            PromotionState.SHADOW: {PromotionState.UNQUALIFIED, PromotionState.DEGRADED},
            PromotionState.CANDIDATE: {
                PromotionState.UNQUALIFIED,
                PromotionState.SHADOW,
                PromotionState.DEGRADED,
            },
            PromotionState.CANARY: {PromotionState.CANDIDATE},
            PromotionState.ACTIVE: {PromotionState.CANARY},
        }
        if current not in allowed_predecessors[decision.target_state]:
            raise EvaluationError(
                f"illegal lifecycle transition {current.value}->{decision.target_state.value}"
            )
        receipt = self.receipts.put_receipt(
            "outcome",
            self.scope,
            {
                "kind": "promotion",
                "route_key": route_key,
                "report_receipt_id": self._report_receipts[decision.report_digest],
                "decision": decision.to_dict(),
                "state": decision.target_state.value,
            },
            provenance={"source": "verdict_evaluation", "version": EVALUATION_SCHEMA_VERSION},
            idempotency_key=f"promotion:{decision.report_digest}:{decision.target_state.value}",
        )
        del receipt
        self._states[route_key] = decision.target_state
        if decision.target_state in {PromotionState.CANARY, PromotionState.ACTIVE}:
            self._known_good[route_key] = decision.report_digest
        return decision

    def rollback(
        self,
        route_identity: RouteIdentity,
        *,
        reason: str,
        automatic: bool = False,
        restore_report_digest: str | None = None,
    ) -> PromotionState:
        if not reason.strip():
            raise EvaluationError("rollback reason is required")
        route_key = route_identity.key
        if route_key in self._kill_switches:
            raise EvaluationError("cannot rollback a kill-switched route")
        if restore_report_digest is not None:
            _require_digest(restore_report_digest, "restore_report_digest")
            restored = self._reports.get(restore_report_digest)
            if restored is None or restored.route_identity != route_identity:
                raise EvaluationError("rollback target is not a recorded report for this route")
        else:
            restore_report_digest = self._known_good.get(route_key)
        # Rollback is an explicit safety action. The known-good artifact is
        # retained for audit/redeployment, while the route remains degraded
        # until a new evidence-gated promotion is approved.
        restored_state = PromotionState.DEGRADED
        self.receipts.put_receipt(
            "outcome",
            self.scope,
            {
                "kind": "rollback",
                "route_key": route_key,
                "reason": reason,
                "automatic": automatic,
                "known_good_report_digest": restore_report_digest,
                "restored_state": restored_state.value,
            },
            provenance={"source": "verdict_evaluation", "version": EVALUATION_SCHEMA_VERSION},
            idempotency_key=f"rollback:{route_key}:{_digest({'reason': reason, 'automatic': automatic})}",
        )
        self._states[route_key] = restored_state
        return restored_state

    def kill_switch(self, route_identity: RouteIdentity, *, reason: str) -> PromotionState:
        if not reason.strip():
            raise EvaluationError("kill-switch reason is required")
        route_key = route_identity.key
        self.receipts.put_receipt(
            "outcome",
            self.scope,
            {"kind": "kill_switch", "route_key": route_key, "reason": reason},
            provenance={"source": "verdict_evaluation", "version": EVALUATION_SCHEMA_VERSION},
            idempotency_key=f"kill-switch:{route_key}:{_digest(reason)}",
        )
        self._kill_switches[route_key] = reason
        self._states[route_key] = PromotionState.QUARANTINED
        return PromotionState.QUARANTINED

    def state(self, route_identity: RouteIdentity) -> PromotionState:
        return self._states.get(route_identity.key, PromotionState.UNQUALIFIED)

    def known_good_report_digest(self, route_identity: RouteIdentity) -> str | None:
        """Return the last canary/active report retained for rollback."""
        return self._known_good.get(route_identity.key)


def counterfactual_from_receipt(
    source_receipt_id: str,
    task_fingerprint: str,
    observed_route: RouteIdentity,
    counterfactual_route: RouteIdentity,
    *,
    receipt_store: ReceiptStore | None = None,
    scope: str | None = None,
    quality_score: float | None,
    verification: VerificationStatus,
    failure_class: EvaluationFailureClass = EvaluationFailureClass.UNKNOWN,
) -> CounterfactualResult:
    """Create a replay-only result from already-redacted receipt metadata."""

    if receipt_store is None or not scope:
        raise EvaluationError("counterfactual evaluation requires a scoped receipt store")
    source = receipt_store.get_receipt(source_receipt_id, scope=scope)
    if source is None:
        raise EvaluationError("source receipt does not exist in the requested scope")
    if not receipt_store.verify_integrity(scope=scope)["valid"]:
        raise EvaluationError("source receipt chain is not intact")
    if source.receipt_type not in {"execution", "outcome", "decision"}:
        raise EvaluationError("source receipt type cannot be counterfactually evaluated")
    source_task_fingerprint = source.payload.get("task_fingerprint")
    if source_task_fingerprint != task_fingerprint:
        raise EvaluationError("source receipt task fingerprint does not match request")
    source_route_key = source.payload.get("route_key")
    source_route = source.payload.get("route_identity")
    if source_route_key is None and source_route is None:
        raise EvaluationError("source receipt has no route context")
    if source_route_key is not None and source_route_key != observed_route.key:
        raise EvaluationError("source receipt route does not match observed route")
    if source_route is not None:
        try:
            if RouteIdentity.from_dict(source_route) != observed_route:
                raise EvaluationError("source receipt route identity does not match observed route")
        except (TypeError, ValueError) as exc:
            raise EvaluationError("source receipt route identity is invalid") from exc

    payload = {
        "source_receipt_id": source_receipt_id,
        "task_fingerprint": task_fingerprint,
        "observed_route": observed_route.to_dict(),
        "counterfactual_route": counterfactual_route.to_dict(),
        "quality_score": quality_score,
        "verification": VerificationStatus(verification).value,
        "failure_class": normalize_failure_class(failure_class).value,
    }
    return CounterfactualResult(
        source_receipt_id=source_receipt_id,
        task_fingerprint=task_fingerprint,
        observed_route=observed_route,
        counterfactual_route=counterfactual_route,
        variant=EvaluationVariant.NO_CONTEXT,
        quality_score=quality_score,
        verification=VerificationStatus(verification),
        failure_class=normalize_failure_class(failure_class),
        evidence_digest=_digest(payload),
    )


__all__ = [
    "EVALUATION_SCHEMA_VERSION",
    "CounterfactualResult",
    "EvaluationCase",
    "EvaluationController",
    "EvaluationError",
    "EvaluationFailureClass",
    "EvaluationObservation",
    "EvaluationReport",
    "EvaluationStatus",
    "EvaluationSuite",
    "EvaluationVariant",
    "PromotionDecision",
    "PromotionPolicy",
    "PromotionState",
    "VariantSummary",
    "VerificationStatus",
    "build_evaluation_report",
    "counterfactual_from_receipt",
    "normalize_failure_class",
]
