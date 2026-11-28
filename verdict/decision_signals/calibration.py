"""OpenJev calibration metrics over labeled Verdict task outcomes (BOD-203).

Input is a list of :class:`CalibrationRecord`: one per real or replayed
Verdict task, carrying the OpenJev signal set that was (or would have been)
collected in SHADOW mode and the observed ground truth.

This module only measures. It never changes routing, admission or planner
behaviour, and it never grants OpenJev authority. The recommended promotion
state is advice for a human.

Labels are a node's verified outcome (``outcome``), the capacity class and tier
of the route that passed, the attempts used and the lifts the run shows. They
come from receipts (``calibration_labels.py``). There is no ground truth for
"frontier needed": the old label was the DAG topology, not an outcome, so the
false-negative, Brier and calibration-error metrics built on it are gone and
the recommendation stays SHADOW (OJ1, #1220).
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from verdict.decision_signals.calibration_labels import LIFTS

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

# Closed set of recognised orchestration roles (AC5, BOD-203).
ROLES: tuple[str, ...] = (
    "controller",
    "implementation_worker",
    "research_test_worker",
    "independent_reviewer",
)

# Records needed before the report may discuss promotion at all.
MIN_RECORDS_FOR_ADVISORY = 50

OUTCOMES = ("accepted", "not_accepted")


@dataclass(frozen=True)
class CalibrationRecord:
    """One task: OpenJev prediction plus observed Verdict outcome."""

    task_id: str
    task_class: str
    frontier_worthy: float | None  # OpenJev probability; None = no usable signal
    confidence: float | None
    security_sensitive: float | None
    security_relevant: bool
    planner_was_frontier: bool  # what the existing Verdict planner actually used
    verified: bool
    first_pass: bool
    retries: int = 0
    escalations: int = 0
    total_cost_usd: float | None = None
    time_to_green_s: float | None = None
    signal_latency_ms: int | None = None
    # AC5 -- orchestration role (optional; None = legacy record without role)
    role: str | None = None
    # AC6 -- per-category cost breakdown (optional; None = unknown)
    context_cost_usd: float | None = None
    tool_cost_usd: float | None = None
    retry_cost_usd: float | None = None
    verification_cost_usd: float | None = None
    escalation_cost_usd: float | None = None
    # AC6 -- real per-attempt token usage (BOD-203)
    total_input_tokens: int | None = None
    total_output_tokens: int | None = None
    attempts_with_usage: int = 0
    attempts_without_usage: int = 0
    # OJ1 -- labels from verified outcomes. None = not recorded (a record written
    # before these existed, or a fact the run's events do not show).
    outcome: str | None = None  # "accepted" | "not_accepted"; None when excluded or unknown
    excluded_reason: str | None = None  # environment/infra failure class: no task label
    capacity_class: str | None = None  # of the route that passed
    capability_tier: int | None = None  # of the route that passed (1 = strongest)
    attempts: int | None = None  # model attempts whose work was judged
    lifts: dict[str, bool | None] | None = None  # per lift: present, absent, None = unknown

    def __post_init__(self) -> None:
        if self.role is not None and self.role not in ROLES:
            raise ValueError(f"unknown role {self.role!r}; expected one of {ROLES}")
        if self.outcome is not None and self.outcome not in OUTCOMES:
            raise ValueError(f"unknown outcome {self.outcome!r}; expected one of {OUTCOMES}")
        if self.excluded_reason is not None and self.outcome is not None:
            raise ValueError("a record excluded from task labels cannot carry an outcome")

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> CalibrationRecord:
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in raw.items() if k in known})


@dataclass
class CalibrationReport:
    policy_threshold: float
    min_confidence: float
    records: int
    usable_signals: int
    per_class: dict[str, int]
    security_fn_rate: float | None
    planner_calls_avoided: int = 0
    verified_rate: float | None = None
    first_pass_rate: float | None = None
    mean_retries: float | None = None
    mean_escalations: float | None = None
    total_cost_usd: float | None = None
    cost_known_count: int = 0
    cost_unknown_count: int = 0
    median_time_to_green_s: float | None = None
    mean_signal_latency_ms: float | None = None
    recommended_state: str = "SHADOW"
    recommendation_reasons: list[str] = field(default_factory=list)
    # AC6 -- per-category cost totals (None = no records with known cost)
    context_cost_usd: float | None = None
    tool_cost_usd: float | None = None
    retry_cost_usd: float | None = None
    verification_cost_usd: float | None = None
    escalation_cost_usd: float | None = None
    cost_per_verified_completion: float | None = None
    # AC7 -- per-role breakdown (role -> sub-report dict)
    per_role: dict[str, dict[str, Any]] = field(default_factory=dict)
    per_role_recommendations: dict[str, tuple[str, list[str]]] = field(default_factory=dict)
    # OJ1 -- verified-outcome labels. Excluded runs are not task failures.
    labelled_records: int = 0  # records with a task outcome
    excluded_records: int = 0
    excluded_reasons: dict[str, int] = field(default_factory=dict)
    accepted_rate: float | None = None
    mean_attempts: float | None = None
    per_capacity_class: dict[str, dict[str, int]] = field(default_factory=dict)
    lift_counts: dict[str, dict[str, int]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"schema": "verdict.openjev-calibration/v2", **asdict(self)}


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


def _evaluate_slice(
    records: Sequence[CalibrationRecord],
    *,
    threshold: float,
    min_confidence: float,
    security_threshold: float,
) -> dict[str, Any]:
    """Core evaluation logic shared by global and per-slice paths.

    Returns a dict of metrics (not a CalibrationReport) so callers can
    embed results in the top-level report or in per-role/per-class dicts.
    """
    per_class: dict[str, int] = {name: 0 for name in TASK_CLASSES}
    for record in records:
        per_class[record.task_class] = per_class.get(record.task_class, 0) + 1

    usable = [r for r in records if _usable(r, min_confidence)]

    # Signal collection is still counted, but excluded runs supply no task labels.
    sec = [
        r
        for r in usable
        if r.excluded_reason is None and r.security_relevant and r.security_sensitive is not None
    ]
    sec_fn = [r for r in sec if (r.security_sensitive or 0.0) < security_threshold]

    avoided = sum(
        1 for r in usable if r.planner_was_frontier and (r.frontier_worthy or 0.0) < threshold
    )

    # An environment or infrastructure failure says nothing about the work: it is
    # counted as excluded, never as a task failure, and stays out of every rate.
    # A record with neither an outcome nor an exclusion (written before OJ1) still
    # feeds the verified / first-pass rates, but has no task label.
    counted = [r for r in records if r.excluded_reason is None]
    excluded_reasons: dict[str, int] = {}
    for r in records:
        if r.excluded_reason is not None:
            excluded_reasons[r.excluded_reason] = excluded_reasons.get(r.excluded_reason, 0) + 1
    judged = [r for r in counted if r.outcome is not None]
    per_capacity: dict[str, dict[str, int]] = {}
    for r in judged:
        if r.capacity_class is not None:
            row = per_capacity.setdefault(r.capacity_class, {"records": 0, "accepted": 0})
            row["records"] += 1
            row["accepted"] += r.outcome == "accepted"
    lift_counts = {name: {"present": 0, "absent": 0, "unknown": 0} for name in LIFTS}
    for r in judged:
        for name in LIFTS:
            seen = (r.lifts or {}).get(name)
            lift_counts[name]["unknown" if seen is None else "present" if seen else "absent"] += 1

    verified_count = sum(r.verified for r in counted)

    # Cost sums: only include records with known cost (not None).
    cost_known = [r for r in records if r.total_cost_usd is not None]
    cost_unknown_count = len(records) - len(cost_known)
    cost_known_count = len(cost_known)

    _cost_vals = [r.total_cost_usd for r in cost_known if r.total_cost_usd is not None]
    total_cost: float | None = round(sum(_cost_vals), 6) if _cost_vals else None
    context_cost: float | None = (
        round(sum(r.context_cost_usd or 0.0 for r in cost_known), 6) if cost_known else None
    )
    tool_cost: float | None = (
        round(sum(r.tool_cost_usd or 0.0 for r in cost_known), 6) if cost_known else None
    )
    retry_cost: float | None = (
        round(sum(r.retry_cost_usd or 0.0 for r in cost_known), 6) if cost_known else None
    )
    verification_cost: float | None = (
        round(sum(r.verification_cost_usd or 0.0 for r in cost_known), 6) if cost_known else None
    )
    escalation_cost: float | None = (
        round(sum(r.escalation_cost_usd or 0.0 for r in cost_known), 6) if cost_known else None
    )

    # cost_per_verified_completion: only meaningful when cost is known
    verified_with_cost = sum(1 for r in cost_known if r.verified)

    return {
        "records": len(records),
        "usable_signals": len(usable),
        "per_class": per_class,
        "security_fn_rate": _rate(len(sec_fn), len(sec)),
        "planner_calls_avoided": avoided,
        "labelled_records": len(judged),
        "excluded_records": len(records) - len(counted),
        "excluded_reasons": excluded_reasons,
        "accepted_rate": _rate(sum(r.outcome == "accepted" for r in judged), len(judged)),
        "mean_attempts": _mean([float(r.attempts) for r in judged if r.attempts is not None]),
        "per_capacity_class": per_capacity,
        "lift_counts": lift_counts,
        "verified_rate": _rate(verified_count, len(counted)),
        "first_pass_rate": _rate(sum(r.first_pass for r in counted), len(counted)),
        "mean_retries": _mean([float(r.retries) for r in counted]),
        "mean_escalations": _mean([float(r.escalations) for r in counted]),
        "total_cost_usd": total_cost,
        "cost_known_count": cost_known_count,
        "cost_unknown_count": cost_unknown_count,
        "context_cost_usd": context_cost,
        "tool_cost_usd": tool_cost,
        "retry_cost_usd": retry_cost,
        "verification_cost_usd": verification_cost,
        "escalation_cost_usd": escalation_cost,
        "cost_per_verified_completion": (
            round(total_cost / verified_with_cost, 6)
            if total_cost is not None and verified_with_cost
            else None
        ),
        "median_time_to_green_s": _median(
            [r.time_to_green_s for r in records if r.time_to_green_s is not None]
        ),
        "mean_signal_latency_ms": _mean(
            [float(r.signal_latency_ms) for r in records if r.signal_latency_ms is not None]
        ),
    }


def evaluate(
    records: Sequence[CalibrationRecord],
    *,
    threshold: float = 0.5,
    min_confidence: float = 0.6,
    security_threshold: float = 0.5,
) -> CalibrationReport:
    """Summarise verified outcomes and OpenJev's usable signals at one threshold.

    ``frontier predicted`` means ``frontier_worthy >= threshold``. A record
    without a usable signal (missing, or below ``min_confidence``) falls back
    to the existing planner: it can never count as an avoided planner call,
    matching the fail-closed rule "never default to the cheap path because
    OpenJev failed".

    Per-role breakdowns are included when the data contains records with a
    ``role`` field (AC5/AC7, BOD-203). Every slice yields SHADOW (see ``recommend``).
    """
    eval_kwargs = {
        "threshold": threshold,
        "min_confidence": min_confidence,
        "security_threshold": security_threshold,
    }
    m = _evaluate_slice(records, **eval_kwargs)

    report = CalibrationReport(
        policy_threshold=threshold,
        min_confidence=min_confidence,
        records=m["records"],
        usable_signals=m["usable_signals"],
        per_class=m["per_class"],
        security_fn_rate=m["security_fn_rate"],
        labelled_records=m["labelled_records"],
        excluded_records=m["excluded_records"],
        excluded_reasons=m["excluded_reasons"],
        accepted_rate=m["accepted_rate"],
        mean_attempts=m["mean_attempts"],
        per_capacity_class=m["per_capacity_class"],
        lift_counts=m["lift_counts"],
        planner_calls_avoided=m["planner_calls_avoided"],
        verified_rate=m["verified_rate"],
        first_pass_rate=m["first_pass_rate"],
        mean_retries=m["mean_retries"],
        mean_escalations=m["mean_escalations"],
        total_cost_usd=m["total_cost_usd"],
        cost_known_count=m["cost_known_count"],
        cost_unknown_count=m["cost_unknown_count"],
        median_time_to_green_s=m["median_time_to_green_s"],
        mean_signal_latency_ms=m["mean_signal_latency_ms"],
        context_cost_usd=m["context_cost_usd"],
        tool_cost_usd=m["tool_cost_usd"],
        retry_cost_usd=m["retry_cost_usd"],
        verification_cost_usd=m["verification_cost_usd"],
        escalation_cost_usd=m["escalation_cost_usd"],
        cost_per_verified_completion=m["cost_per_verified_completion"],
    )
    report.recommended_state, report.recommendation_reasons = recommend(report)

    # AC7 -- per-role slices
    roles_seen: dict[str, list[CalibrationRecord]] = {}
    for r in records:
        if r.role is not None:
            roles_seen.setdefault(r.role, []).append(r)

    for role, role_records in sorted(roles_seen.items()):
        role_m = _evaluate_slice(role_records, **eval_kwargs)
        report.per_role[role] = role_m
        # Per-role recommendation: build a minimal CalibrationReport to run
        # through the same recommend() logic, inheriting global thresholds.
        role_report = CalibrationReport(
            policy_threshold=threshold,
            min_confidence=min_confidence,
            records=role_m["records"],
            usable_signals=role_m["usable_signals"],
            per_class=role_m["per_class"],
            security_fn_rate=role_m["security_fn_rate"],
            total_cost_usd=role_m["total_cost_usd"],
            cost_known_count=role_m["cost_known_count"],
            cost_unknown_count=role_m["cost_unknown_count"],
        )
        report.per_role_recommendations[role] = recommend(role_report)

    return report


def recommend(report: CalibrationReport) -> tuple[str, list[str]]:
    """Promotion advice. Always SHADOW: no verified-outcome label scores ``frontier_worthy``.

    The old ground truth was the DAG topology, which never said whether a frontier
    model was needed, so it is removed. Until per-lift labels are calibrated
    (OJ4) nothing here can support more than SHADOW.
    """
    reasons: list[str] = []
    if report.usable_signals < MIN_RECORDS_FOR_ADVISORY:
        reasons.append(
            f"only {report.usable_signals} usable signals "
            f"(< {MIN_RECORDS_FOR_ADVISORY} required for ADVISORY)"
        )
    reasons.append(
        "no verified-outcome label scores frontier_worthy; promotion waits for per-lift labels"
    )
    return "SHADOW", reasons


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
        f"- security false-negative rate: {fmt(report.security_fn_rate)}",
        f"- planner calls avoidable: {report.planner_calls_avoided}",
        f"- labelled: {report.labelled_records}; excluded (environment or infrastructure): "
        f"{report.excluded_records} {report.excluded_reasons or ''}".rstrip(),
        f"- accepted: {fmt(report.accepted_rate)}; mean attempts: "
        f"{fmt(report.mean_attempts, pct=False)}",
        f"- verified: {fmt(report.verified_rate)}; first pass: {fmt(report.first_pass_rate)}",
        f"- mean retries: {fmt(report.mean_retries, pct=False)}; "
        f"mean escalations: {fmt(report.mean_escalations, pct=False)}",
        f"- total cost: "
        f"{'$' + f'{report.total_cost_usd:.4f}' if report.total_cost_usd is not None else 'unknown'}; "
        f"median time-to-green: {report.median_time_to_green_s}",
        f"- cost known: {report.cost_known_count}; cost unknown: {report.cost_unknown_count}",
        f"- cost per verified completion: "
        f"{'$' + fmt(report.cost_per_verified_completion, pct=False) if report.cost_per_verified_completion is not None else 'n/a'}",
        f"- cost breakdown: "
        f"context={'$' + f'{report.context_cost_usd:.4f}' if report.context_cost_usd is not None else 'unknown'}, "
        f"tool={'$' + f'{report.tool_cost_usd:.4f}' if report.tool_cost_usd is not None else 'unknown'}, "
        f"retry={'$' + f'{report.retry_cost_usd:.4f}' if report.retry_cost_usd is not None else 'unknown'}, "
        f"verification={'$' + f'{report.verification_cost_usd:.4f}' if report.verification_cost_usd is not None else 'unknown'}, "
        f"escalation={'$' + f'{report.escalation_cost_usd:.4f}' if report.escalation_cost_usd is not None else 'unknown'}",
        f"- recommended state: **{report.recommended_state}**",
    ]
    lines += [f"  - {reason}" for reason in report.recommendation_reasons]
    lines += ["", "## Records per class", ""]
    lines += [f"- {name}: {count}" for name, count in report.per_class.items()]
    lines += ["", "## Passing route capacity class", ""]
    lines += [
        f"- {name}: {row['accepted']} accepted of {row['records']}"
        for name, row in sorted(report.per_capacity_class.items())
    ]
    lines += ["", "## Lifts (records with a task outcome)", ""]
    lines += [
        f"- {name}: present {c['present']}, absent {c['absent']}, unknown {c['unknown']}"
        for name, c in report.lift_counts.items()
    ]

    # AC7 -- per-role breakdown
    if report.per_role:
        lines += ["", "## Per-role breakdown", ""]
        for role in sorted(report.per_role):
            rm = report.per_role[role]
            rec_state, rec_reasons = report.per_role_recommendations.get(
                role, ("SHADOW", ["no recommendation computed"])
            )

            def _cost_fmt(v: float | None) -> str:
                return f"${v:.4f}" if v is not None else "unknown"

            rm_total = _cost_fmt(rm.get("total_cost_usd"))
            rm_cpv = (
                "$" + fmt(rm.get("cost_per_verified_completion"), pct=False)
                if rm.get("cost_per_verified_completion") is not None
                else "n/a"
            )
            lines += [
                f"### {role}",
                "",
                f"- records: {rm['records']} (usable: {rm['usable_signals']})",
                f"- security FN rate: {fmt(rm.get('security_fn_rate'))}",
                f"- verified: {fmt(rm.get('verified_rate'))}; "
                f"first pass: {fmt(rm.get('first_pass_rate'))}",
                f"- total cost: {rm_total}; "
                f"cost known: {rm.get('cost_known_count', 0)}; "
                f"cost unknown: {rm.get('cost_unknown_count', 0)}; "
                f"cost/verified: {rm_cpv}",
                f"- cost breakdown: "
                f"context={_cost_fmt(rm.get('context_cost_usd'))}, "
                f"tool={_cost_fmt(rm.get('tool_cost_usd'))}, "
                f"retry={_cost_fmt(rm.get('retry_cost_usd'))}, "
                f"verification={_cost_fmt(rm.get('verification_cost_usd'))}, "
                f"escalation={_cost_fmt(rm.get('escalation_cost_usd'))}",
                f"- recommended state: **{rec_state}**",
            ]
            lines += [f"  - {r}" for r in rec_reasons]
            lines.append("")

    return "\n".join(lines) + "\n"
