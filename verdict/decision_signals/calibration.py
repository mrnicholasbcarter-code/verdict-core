"""OpenJev calibration metrics over labeled Verdict task outcomes (BOD-203).

Input is a list of :class:`CalibrationRecord`: one per real or replayed
Verdict task, carrying the OpenJev signal set that was (or would have been)
collected in SHADOW mode and the observed ground truth.

This module only measures. It never changes routing, admission or planner
behaviour, and it never grants OpenJev authority. The recommended promotion
state is advice for a human; it is capped by false negatives first:
**a cheap decision that wrongly skips necessary frontier work is a
regression even if average cost improves.**
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

TASK_CLASSES = (
    "trivial_local_edit",
    "bounded_implementation",
    "multi_file_implementation",
    "architecture_refactor",
    "debugging",
    "security_sensitive",
    "ambiguous_request",
    "repo_wide_change",
    "decomposable_parallel",
    "frontier_required",
)

# Promotion gates. Initial values are deliberately strict and are NOT
# calibrated thresholds for production; they only decide what the report may
# recommend. Changing them is a policy change that must be revisioned.
MIN_RECORDS_FOR_ADVISORY = 50
MIN_RECORDS_PER_CLASS_FOR_SKIP = 20
MAX_FRONTIER_FN_RATE_ADVISORY = 0.05
MAX_FRONTIER_FN_RATE_SKIP = 0.0
MAX_SECURITY_FN_RATE = 0.0
MAX_CALIBRATION_ERROR_ADVISORY = 0.10


@dataclass(frozen=True)
class CalibrationRecord:
    """One task: OpenJev prediction plus observed Verdict outcome."""

    task_id: str
    task_class: str
    frontier_worthy: float | None  # OpenJev probability; None = no usable signal
    confidence: float | None
    security_sensitive: float | None
    frontier_needed: bool  # ground truth: did verified completion need frontier cognition?
    security_relevant: bool
    planner_was_frontier: bool  # what the existing Verdict planner actually used
    verified: bool
    first_pass: bool
    retries: int = 0
    escalations: int = 0
    total_cost_usd: float = 0.0
    time_to_green_s: float | None = None
    signal_latency_ms: int | None = None

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> CalibrationRecord:
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in raw.items() if k in known})


@dataclass
class ReliabilityBucket:
    lower: float
    upper: float
    count: int
    mean_predicted: float
    observed_rate: float


@dataclass
class CalibrationReport:
    policy_threshold: float
    min_confidence: float
    records: int
    usable_signals: int
    per_class: dict[str, int]
    frontier_fn_rate: float | None
    frontier_fp_rate: float | None
    security_fn_rate: float | None
    brier: float | None
    calibration_error: float | None
    reliability: list[ReliabilityBucket] = field(default_factory=list)
    planner_calls_avoided: int = 0
    verified_rate: float | None = None
    first_pass_rate: float | None = None
    mean_retries: float | None = None
    mean_escalations: float | None = None
    total_cost_usd: float = 0.0
    median_time_to_green_s: float | None = None
    mean_signal_latency_ms: float | None = None
    false_negatives: list[str] = field(default_factory=list)
    recommended_state: str = "SHADOW"
    recommendation_reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"schema": "verdict.openjev-calibration/v1", **asdict(self)}


def load_records(path: Path) -> list[CalibrationRecord]:
    """Read JSONL records; malformed lines are an error, never skipped silently."""
    records: list[CalibrationRecord] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            records.append(CalibrationRecord.from_dict(json.loads(line)))
        except (ValueError, TypeError) as exc:
            raise ValueError(f"{path}:{number}: invalid calibration record: {exc}") from exc
    return records


def _rate(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def _mean(values: Sequence[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _median(values: Sequence[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    return ordered[mid] if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) / 2


def _usable(record: CalibrationRecord, min_confidence: float) -> bool:
    return (
        record.frontier_worthy is not None
        and record.confidence is not None
        and record.confidence >= min_confidence
    )


def reliability_buckets(
    pairs: Iterable[tuple[float, bool]], *, bins: int = 10
) -> list[ReliabilityBucket]:
    grouped: list[list[tuple[float, bool]]] = [[] for _ in range(bins)]
    for predicted, observed in pairs:
        index = min(int(predicted * bins), bins - 1)
        grouped[index].append((predicted, observed))
    out: list[ReliabilityBucket] = []
    for index, items in enumerate(grouped):
        if not items:
            continue
        out.append(
            ReliabilityBucket(
                lower=index / bins,
                upper=(index + 1) / bins,
                count=len(items),
                mean_predicted=sum(p for p, _ in items) / len(items),
                observed_rate=sum(1 for _, o in items if o) / len(items),
            )
        )
    return out


def evaluate(
    records: Sequence[CalibrationRecord],
    *,
    threshold: float = 0.5,
    min_confidence: float = 0.6,
    security_threshold: float = 0.5,
) -> CalibrationReport:
    """Score OpenJev against ground truth at one decision threshold.

    ``frontier predicted`` means ``frontier_worthy >= threshold``. A record
    without a usable signal (missing, or below ``min_confidence``) falls back
    to the existing planner: it can never count as an avoided planner call,
    matching the fail-closed rule "never default to the cheap path because
    OpenJev failed".
    """
    per_class = {name: 0 for name in TASK_CLASSES}
    for record in records:
        per_class[record.task_class] = per_class.get(record.task_class, 0) + 1

    usable = [r for r in records if _usable(r, min_confidence)]
    needed = [r for r in usable if r.frontier_needed]
    not_needed = [r for r in usable if not r.frontier_needed]
    fn = [r for r in needed if (r.frontier_worthy or 0.0) < threshold]
    fp = [r for r in not_needed if (r.frontier_worthy or 0.0) >= threshold]

    sec = [r for r in usable if r.security_relevant and r.security_sensitive is not None]
    sec_fn = [r for r in sec if (r.security_sensitive or 0.0) < security_threshold]

    pairs = [(float(r.frontier_worthy or 0.0), r.frontier_needed) for r in usable]
    brier = _mean([(p - (1.0 if o else 0.0)) ** 2 for p, o in pairs])
    buckets = reliability_buckets(pairs)
    total = sum(b.count for b in buckets)
    ece = (
        sum(b.count / total * abs(b.mean_predicted - b.observed_rate) for b in buckets)
        if total
        else None
    )
    avoided = sum(
        1 for r in usable if r.planner_was_frontier and (r.frontier_worthy or 0.0) < threshold
    )

    report = CalibrationReport(
        policy_threshold=threshold,
        min_confidence=min_confidence,
        records=len(records),
        usable_signals=len(usable),
        per_class=per_class,
        frontier_fn_rate=_rate(len(fn), len(needed)),
        frontier_fp_rate=_rate(len(fp), len(not_needed)),
        security_fn_rate=_rate(len(sec_fn), len(sec)),
        brier=brier,
        calibration_error=ece,
        reliability=buckets,
        planner_calls_avoided=avoided,
        verified_rate=_rate(sum(r.verified for r in records), len(records)),
        first_pass_rate=_rate(sum(r.first_pass for r in records), len(records)),
        mean_retries=_mean([float(r.retries) for r in records]),
        mean_escalations=_mean([float(r.escalations) for r in records]),
        total_cost_usd=round(sum(r.total_cost_usd for r in records), 6),
        median_time_to_green_s=_median(
            [r.time_to_green_s for r in records if r.time_to_green_s is not None]
        ),
        mean_signal_latency_ms=_mean(
            [float(r.signal_latency_ms) for r in records if r.signal_latency_ms is not None]
        ),
        false_negatives=[r.task_id for r in fn],
    )
    report.recommended_state, report.recommendation_reasons = recommend(report)
    return report


def recommend(report: CalibrationReport) -> tuple[str, list[str]]:
    """Promotion advice, false negatives first. Never above what evidence supports."""
    reasons: list[str] = []
    if report.usable_signals < MIN_RECORDS_FOR_ADVISORY:
        reasons.append(
            f"only {report.usable_signals} usable signals "
            f"(< {MIN_RECORDS_FOR_ADVISORY} required for ADVISORY)"
        )
        return "SHADOW", reasons
    if report.security_fn_rate is None or report.security_fn_rate > MAX_SECURITY_FN_RATE:
        reasons.append(f"security false-negative rate {report.security_fn_rate} is not 0")
        return "SHADOW", reasons
    if report.frontier_fn_rate is None or report.frontier_fn_rate > MAX_FRONTIER_FN_RATE_ADVISORY:
        reasons.append(
            f"frontier false-negative rate {report.frontier_fn_rate} "
            f"> {MAX_FRONTIER_FN_RATE_ADVISORY}"
        )
        return "SHADOW", reasons
    if (
        report.calibration_error is None
        or report.calibration_error > MAX_CALIBRATION_ERROR_ADVISORY
    ):
        reasons.append(
            f"calibration error {report.calibration_error} > {MAX_CALIBRATION_ERROR_ADVISORY}"
        )
        return "SHADOW", reasons
    thin = [c for c, n in report.per_class.items() if n < MIN_RECORDS_PER_CLASS_FOR_SKIP]
    if report.frontier_fn_rate > MAX_FRONTIER_FN_RATE_SKIP or thin:
        if thin:
            reasons.append(f"classes below {MIN_RECORDS_PER_CLASS_FOR_SKIP} records: {thin}")
        if report.frontier_fn_rate > MAX_FRONTIER_FN_RATE_SKIP:
            reasons.append("frontier false negatives present; BOUNDED_SKIP requires zero")
        return "ADVISORY", reasons
    reasons.append("all gates met; BOUNDED_SKIP still needs operator approval per task class")
    return "BOUNDED_SKIP", reasons


def render_markdown(report: CalibrationReport) -> str:
    def fmt(value: float | None, pct: bool = True) -> str:
        if value is None:
            return "n/a"
        return f"{value:.1%}" if pct else f"{value:.4f}"

    lines = [
        "# OpenJev calibration report",
        "",
        f"- records: {report.records} (usable signals: {report.usable_signals})",
        f"- threshold: frontier_worthy >= {report.policy_threshold}; "
        f"min confidence {report.min_confidence}",
        f"- frontier false-negative rate: {fmt(report.frontier_fn_rate)}",
        f"- frontier false-positive rate: {fmt(report.frontier_fp_rate)}",
        f"- security false-negative rate: {fmt(report.security_fn_rate)}",
        f"- Brier: {fmt(report.brier, pct=False)}; "
        f"calibration error: {fmt(report.calibration_error, pct=False)}",
        f"- planner calls avoidable: {report.planner_calls_avoided}",
        f"- verified: {fmt(report.verified_rate)}; first pass: {fmt(report.first_pass_rate)}",
        f"- mean retries: {fmt(report.mean_retries, pct=False)}; "
        f"mean escalations: {fmt(report.mean_escalations, pct=False)}",
        f"- total cost: ${report.total_cost_usd:.4f}; "
        f"median time-to-green: {report.median_time_to_green_s}",
        f"- recommended state: **{report.recommended_state}**",
    ]
    lines += [f"  - {reason}" for reason in report.recommendation_reasons]
    lines += ["", "## Records per class", ""]
    lines += [f"- {name}: {count}" for name, count in report.per_class.items()]
    lines += [
        "",
        "## Reliability",
        "",
        "| bin | n | mean predicted | observed |",
        "|---|---|---|---|",
    ]
    lines += [
        f"| {b.lower:.1f}-{b.upper:.1f} | {b.count} | {b.mean_predicted:.3f} | {b.observed_rate:.3f} |"
        for b in report.reliability
    ]
    if report.false_negatives:
        lines += ["", "## Frontier false negatives", ""]
        lines += [f"- {task_id}" for task_id in report.false_negatives]
    return "\n".join(lines) + "\n"
