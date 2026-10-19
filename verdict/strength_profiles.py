"""Deterministic, evidence-backed task-strength profiles.

Strength is measured separately from route health.  Authentication, identity,
configuration, transport, and timeout failures therefore remain evidence about
the route and never become a zero-quality model score.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from verdict.candidate_pool import OutcomeObservation

STRENGTH_PROFILE_SCHEMA_VERSION = "1"
_IDENTIFIER = re.compile(r"^[a-z0-9][a-z0-9_.:-]{0,127}$")
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")


class StrengthProfileError(ValueError):
    """Raised when strength evidence violates its public contract."""


class StrengthFailureClass(str, Enum):
    """Failure classes used to keep route failures out of quality scoring."""

    QUALITY = "quality"
    AUTHENTICATION = "authentication"
    CONFIGURATION = "configuration"
    IDENTITY = "identity"
    TRANSPORT = "transport"
    TIMEOUT = "timeout"


@dataclass(frozen=True)
class StrengthObservation:
    """One bounded, provenance-linked result for a task-family sample."""

    route_key: str
    task_family: str
    suite_id: str
    suite_version: str
    rubric_id: str
    rubric_version: str
    sample_count: int
    observed_at: datetime
    score: float | None
    confidence: float
    failure_class: StrengthFailureClass = StrengthFailureClass.QUALITY
    evidence_digest: str = ""
    schema_version: str = STRENGTH_PROFILE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != STRENGTH_PROFILE_SCHEMA_VERSION:
            raise StrengthProfileError("schema_version must be '1'")
        for name in (
            "route_key",
            "task_family",
            "suite_id",
            "suite_version",
            "rubric_id",
            "rubric_version",
        ):
            _identifier(getattr(self, name), name)
        if not isinstance(self.sample_count, int) or isinstance(self.sample_count, bool):
            raise StrengthProfileError("sample_count must be a positive integer")
        if self.sample_count < 1:
            raise StrengthProfileError("sample_count must be a positive integer")
        observed_at = _utc(self.observed_at, "observed_at")
        object.__setattr__(self, "observed_at", observed_at)
        if isinstance(self.failure_class, str):
            try:
                object.__setattr__(self, "failure_class", StrengthFailureClass(self.failure_class))
            except ValueError as exc:
                raise StrengthProfileError("failure_class is invalid") from exc
        if not isinstance(self.failure_class, StrengthFailureClass):
            raise StrengthProfileError("failure_class is invalid")
        _bounded_probability(self.confidence, "confidence")
        if self.score is not None:
            _bounded_probability(self.score, "score")
        if self.failure_class is StrengthFailureClass.QUALITY and self.score is None:
            raise StrengthProfileError("quality observations require a score")
        if self.failure_class is not StrengthFailureClass.QUALITY and self.score is not None:
            raise StrengthProfileError("non-quality observations cannot have a score")
        if not isinstance(self.evidence_digest, str) or not _SHA256.fullmatch(self.evidence_digest):
            raise StrengthProfileError("evidence_digest must be a lowercase sha256 digest")

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> StrengthObservation:
        payload = _strict_mapping(
            value,
            required={
                "schema_version",
                "route_key",
                "task_family",
                "suite_id",
                "suite_version",
                "rubric_id",
                "rubric_version",
                "sample_count",
                "observed_at",
                "score",
                "confidence",
                "failure_class",
                "evidence_digest",
            },
            field_name="strength_observation",
        )
        return cls(
            schema_version=payload["schema_version"],
            route_key=payload["route_key"],
            task_family=payload["task_family"],
            suite_id=payload["suite_id"],
            suite_version=payload["suite_version"],
            rubric_id=payload["rubric_id"],
            rubric_version=payload["rubric_version"],
            sample_count=payload["sample_count"],
            observed_at=_parse_datetime(payload["observed_at"], "observed_at"),
            score=payload["score"],
            confidence=payload["confidence"],
            failure_class=payload["failure_class"],
            evidence_digest=payload["evidence_digest"],
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "route_key": self.route_key,
            "task_family": self.task_family,
            "suite_id": self.suite_id,
            "suite_version": self.suite_version,
            "rubric_id": self.rubric_id,
            "rubric_version": self.rubric_version,
            "sample_count": self.sample_count,
            "observed_at": _format_datetime(self.observed_at),
            "score": self.score,
            "confidence": self.confidence,
            "failure_class": self.failure_class.value,
            "evidence_digest": self.evidence_digest,
        }

    @property
    def digest(self) -> str:
        return _digest(self.to_dict())


@dataclass(frozen=True)
class StrengthAggregate:
    """Deterministic quality summary for one route and task family."""

    route_key: str
    task_family: str
    suite_id: str
    suite_version: str
    rubric_id: str
    rubric_version: str
    quality_sample_count: int
    total_sample_count: int
    score: float | None
    confidence: float | None
    observed_at: datetime
    ignored_failures: Mapping[str, int]
    schema_version: str = STRENGTH_PROFILE_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "route_key": self.route_key,
            "task_family": self.task_family,
            "suite_id": self.suite_id,
            "suite_version": self.suite_version,
            "rubric_id": self.rubric_id,
            "rubric_version": self.rubric_version,
            "quality_sample_count": self.quality_sample_count,
            "total_sample_count": self.total_sample_count,
            "score": self.score,
            "confidence": self.confidence,
            "observed_at": _format_datetime(self.observed_at),
            "ignored_failures": dict(sorted(self.ignored_failures.items())),
        }

    @property
    def digest(self) -> str:
        return _digest(self.to_dict())


def aggregate_strength(
    observations: Iterable[StrengthObservation],
) -> tuple[StrengthAggregate, ...]:
    """Aggregate quality scores while preserving non-quality failure counts."""

    groups: dict[tuple[str, str, str, str, str, str], list[StrengthObservation]] = defaultdict(list)
    for observation in observations:
        if not isinstance(observation, StrengthObservation):
            raise StrengthProfileError("observations must contain StrengthObservation values")
        key = (
            observation.route_key,
            observation.task_family,
            observation.suite_id,
            observation.suite_version,
            observation.rubric_id,
            observation.rubric_version,
        )
        groups[key].append(observation)

    result: list[StrengthAggregate] = []
    for key in sorted(groups):
        items = groups[key]
        quality = [item for item in items if item.failure_class is StrengthFailureClass.QUALITY]
        quality_samples = sum(item.sample_count for item in quality)
        total_samples = sum(item.sample_count for item in items)
        score = (
            sum(float(item.score) * item.sample_count for item in quality if item.score is not None)
            / quality_samples
            if quality_samples
            else None
        )
        confidence = (
            sum(item.confidence * item.sample_count for item in quality) / quality_samples
            if quality_samples
            else None
        )
        failures = Counter(
            item.failure_class.value
            for item in items
            if item.failure_class is not StrengthFailureClass.QUALITY
        )
        result.append(
            StrengthAggregate(
                route_key=key[0],
                task_family=key[1],
                suite_id=key[2],
                suite_version=key[3],
                rubric_id=key[4],
                rubric_version=key[5],
                quality_sample_count=quality_samples,
                total_sample_count=total_samples,
                score=score,
                confidence=confidence,
                observed_at=max(item.observed_at for item in items),
                ignored_failures=dict(sorted(failures.items())),
            )
        )
    return tuple(result)


def _identifier(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise StrengthProfileError(f"{field_name} must be a bounded identifier")
    return value


def _bounded_probability(value: Any, field_name: str) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or not 0 <= float(value) <= 1
    ):
        raise StrengthProfileError(f"{field_name} must be between 0 and 1")


def _strict_mapping(value: Any, *, required: set[str], field_name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise StrengthProfileError(f"{field_name} must be an object")
    keys = set(value)
    missing = required - keys
    unknown = keys - required
    if missing:
        raise StrengthProfileError(f"{field_name} missing field(s): {', '.join(sorted(missing))}")
    if unknown:
        raise StrengthProfileError(
            f"{field_name} has unknown field(s): {', '.join(sorted(unknown))}"
        )
    return dict(value)


def _parse_datetime(value: Any, field_name: str) -> datetime:
    if not isinstance(value, str):
        raise StrengthProfileError(f"{field_name} must be an ISO-8601 string")
    try:
        return _utc(datetime.fromisoformat(value.replace("Z", "+00:00")), field_name)
    except ValueError as exc:
        raise StrengthProfileError(f"{field_name} must be an ISO-8601 string") from exc


def _utc(value: Any, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise StrengthProfileError(f"{field_name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _format_datetime(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _digest(value: Mapping[str, Any]) -> str:
    canonical = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class TaskHistoryScore:
    """Smoothed, task-specific verified-history score for one route (BOD-438).

    ``score`` is the Bayesian-smoothed success rate over the 30-day decayed
    window; with no qualifying observations it is the 0.5 prior. ``observed_n``
    is the count of distinct verified, quality-scored observations inside the
    window; ``weighted_n`` is their decay-weighted sum. ``demoted`` is True
    only when ``score < 0.4`` and ``observed_n >= 3``; demotion never removes
    a route from the candidate pool, it only reorders the admitted set.
    """

    score: float
    observed_n: int
    weighted_n: float
    demoted: bool


_HISTORY_WINDOW_DAYS = 30
_HISTORY_HALF_LIFE_DAYS = 7
_DEMOTION_SCORE_THRESHOLD = 0.4
_DEMOTION_MIN_OBSERVED_N = 3
_HISTORY_PRIOR = 0.5


def score_task_history(
    observations: Sequence[OutcomeObservation], *, route_id: str, task_family: str, as_of: datetime
) -> TaskHistoryScore:
    """Smoothed verified-history score for ``route_id``/``task_family`` as of ``as_of``.

    Only observations with a non-``None`` ``quality_credit`` and a non-``None``
    ``observed_at`` enter the score — unverified/transient rows never count.
    Each qualifying observation's age must fall in the half-open window
    ``[0, 30d)`` before ``as_of``; it is weighted ``2 ** (-age_days / 7)``
    (a 7-day half-life). With no qualifying observations the score is the 0.5
    prior with ``observed_n = 0`` and ``demoted = False``.
    """
    as_of = _utc(as_of, "as_of")
    weighted_accepted = 0.0
    weighted_partial = 0.0
    weighted_eligible = 0.0
    observed_n = 0
    for obs in observations:
        if obs.route_id != route_id or obs.task_family != task_family:
            continue
        if obs.quality_credit is None or obs.observed_at is None:
            continue
        observed_at = _utc(obs.observed_at, "observed_at")
        age_days = (as_of - observed_at).total_seconds() / 86400.0
        if not (0 <= age_days < _HISTORY_WINDOW_DAYS):
            continue
        weight = 2.0 ** (-age_days / _HISTORY_HALF_LIFE_DAYS)
        observed_n += 1
        weighted_eligible += weight
        if obs.quality_credit == 1.0:
            weighted_accepted += weight
        elif obs.quality_credit == 0.5:
            weighted_partial += weight

    if observed_n == 0:
        return TaskHistoryScore(score=_HISTORY_PRIOR, observed_n=0, weighted_n=0.0, demoted=False)

    score = (weighted_accepted + 0.5 * weighted_partial + 1.0) / (weighted_eligible + 2.0)
    demoted = score < _DEMOTION_SCORE_THRESHOLD and observed_n >= _DEMOTION_MIN_OBSERVED_N
    return TaskHistoryScore(
        score=score, observed_n=observed_n, weighted_n=weighted_eligible, demoted=demoted
    )


__all__ = [
    "STRENGTH_PROFILE_SCHEMA_VERSION",
    "StrengthAggregate",
    "StrengthFailureClass",
    "StrengthObservation",
    "StrengthProfileError",
    "TaskHistoryScore",
    "aggregate_strength",
    "score_task_history",
]
