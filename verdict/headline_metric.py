"""Headline metric report: accepted work per paid model share / per paid dollar.

This module aggregates verified receipts from VERDICT_RUNS_DIR and computes
the receipt-based part of the M0 headline metric (BOD-342 D15, issue #1213):

  accepted_tasks / paid_model_share   (a proxy for the paid-quota denominator)
  accepted_tasks / dollars            (when every attempt has a recorded cost)

It reads local files only.  The paid-quota denominator itself (% of a
provider's subscription window consumed) is not computed here.

Any number that cannot be computed is ``None`` (JSON null) with a companion
``*_note`` reason, never a string sentinel and never a zero fill.

Fixture runs (run_id starting with "test-") are excluded from the live
aggregate; ``load_receipts_since`` skips them and ``format_headline_report``
says so explicitly when any fixture is encountered.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import date, datetime
from pathlib import Path
from typing import Any

from verdict.orchestration.contracts import NodeKind
from verdict.orchestration.receipt import extend_run_metrics

_FIXTURE_RUN_ID_PREFIX = "test-"


def _is_fixture_run(receipt: Mapping[str, Any]) -> bool:
    """Fixture runs have run_ids starting with the fixture prefix."""
    return str(receipt.get("run_id") or "").startswith(_FIXTURE_RUN_ID_PREFIX)


def load_receipts_since(
    runs_dir: Path, since: date | None = None
) -> tuple[list[tuple[Path, dict[str, Any]]], int, int]:
    """Load and verify receipts from *runs_dir*, optionally filtered by date.

    Returns ``(receipts, skipped_live, fixture_skipped)`` where:
    - ``receipts`` is a list of ``(run_dir, receipt_dict)`` for valid, non-fixture runs.
    - ``skipped_live`` is the number of live runs skipped due to verification failures.
    - ``fixture_skipped`` is the number of fixture runs excluded.

    ``repair=False`` is used throughout so this read-only report never modifies
    evidence on disk (e.g. truncating a torn events.jsonl tail).
    """
    from verdict.orchestration.receipt import verify_run_receipt

    receipts: list[tuple[Path, dict[str, Any]]] = []
    skipped_live = 0
    fixture_skipped = 0

    if not runs_dir.exists():
        return receipts, skipped_live, fixture_skipped

    for receipt_path in sorted(runs_dir.rglob("receipt.json")):
        run_dir = receipt_path.parent
        try:
            raw = json.loads(receipt_path.read_text())
        except (OSError, ValueError):
            skipped_live += 1
            continue

        # Reject non-object JSON (null, [], scalars) — these cannot be receipts.
        if not isinstance(raw, dict):
            skipped_live += 1
            continue

        # Exclude fixture runs.
        if _is_fixture_run(raw):
            fixture_skipped += 1
            continue

        # Date filter: check started_at.
        if since is not None:
            started_at = raw.get("started_at")
            if started_at:
                try:
                    dt = datetime.fromisoformat(str(started_at).replace("Z", "+00:00"))
                    if dt.date() < since:
                        continue
                except ValueError:
                    pass  # fall through — include if we can't parse

        # repair=False: never modify evidence on disk.
        problems = verify_run_receipt(run_dir, repair=False)
        if problems:
            skipped_live += 1
            continue

        receipts.append((run_dir, raw))

    return receipts, skipped_live, fixture_skipped


def _tasks_per_dollar(
    accepted: int, dollars: float | None, has_unknown_cost: bool
) -> tuple[float | None, str | None]:
    """Return ``(value, note)`` for accepted tasks per dollar.

    A float and no note when it is computable; otherwise ``None`` and the reason.
    A missing number is never a string sentinel and never a division by zero.
    (``compute_run_metrics`` still returns the string ``"undefined"`` for the
    existing per-run ``verdict metrics`` table; the headline report normalises
    it here so its JSON only ever carries a number or null.)
    """
    if has_unknown_cost or dollars is None:
        return None, "some model attempts have unknown cost"
    if dollars <= 0:
        return None, "no spend recorded in selected period"
    return accepted / dollars, None


def aggregate_extended_metrics(receipts: list[tuple[Path, dict[str, Any]]]) -> dict[str, Any]:
    """Aggregate extend_run_metrics across a list of verified receipts.

    Returns aggregated totals and per-run data.  Numbers that cannot be
    computed (unknown cost, missing token counts) are propagated faithfully:
    ``dollars`` is None when any run has unknown cost, etc.  Every unavailable
    ratio is ``None`` with a companion ``*_note`` reason, never a string.
    """
    if not receipts:
        return {
            "accepted_tasks": 0,
            "dollars": None,
            "has_unknown_cost": False,
            "tasks_per_dollar": None,
            "tasks_per_dollar_note": "no runs in selected period",
            "free_share": 0.0,
            "subscription_share": 0.0,
            "metered_share": 0.0,
            "unknown_capacity_share": 0.0,
            "total_model_attempts": 0,
            "free_attempts": 0,
            "subscription_attempts": 0,
            "metered_attempts": 0,
            "unknown_capacity_attempts": 0,
            "paid_model_share": 0.0,
            "attempts_by_role": {r.value: 0 for r in NodeKind},
            "accepted_nodes_by_role": {r.value: 0 for r in NodeKind},
            "frontier_tokens": {"input": 0, "output": 0, "total": 0, "per_accepted_task": None},
            "first_attempt_pass_rate": {"by_role": {}, "by_cost": {}},
            "runs_processed": 0,
            "per_run": [],
        }

    roles = [r.value for r in NodeKind]
    total_accepted = 0
    total_cost = 0.0
    has_unknown_cost = False
    total_attempts = 0
    free_attempts = 0
    sub_attempts = 0
    metered_attempts = 0
    unknown_attempts = 0
    attempts_by_role: dict[str, int] = {r: 0 for r in roles}
    accepted_by_role: dict[str, int] = {r: 0 for r in roles}
    # Frontier tokens
    frontier_in = 0
    frontier_out = 0
    frontier_known = True
    # First-attempt pass: accumulate raw success/eligible counts for weighted aggregate.
    # Averaging per-run fractions gives wrong results when runs differ in node count
    # (1 success in 1 run + 99 failures in another → 50% not 1%).
    fpr_role_success: dict[str, int] = {r: 0 for r in roles}
    fpr_role_eligible: dict[str, int] = {r: 0 for r in roles}
    fpr_cost_success: dict[str, int] = {}
    fpr_cost_eligible: dict[str, int] = {}
    per_run: list[dict[str, Any]] = []

    for _run_dir, receipt in receipts:
        m = extend_run_metrics(receipt)

        total_accepted += m["accepted_tasks"]
        if m["dollars"] is not None:
            total_cost += m["dollars"]
        else:
            has_unknown_cost = True
        total_attempts += m["total_model_attempts"]
        free_attempts += m["free_attempts"]
        sub_attempts += m["subscription_attempts"]
        metered_attempts += m["metered_attempts"]
        unknown_attempts += m["unknown_capacity_attempts"]

        for role in roles:
            attempts_by_role[role] += m["attempts_by_role"].get(role, 0)
            accepted_by_role[role] += m["accepted_nodes_by_role"].get(role, 0)

        ft = m.get("frontier_tokens") or {}
        if ft.get("input") is None or ft.get("output") is None:
            frontier_known = False
        else:
            frontier_in += int(ft["input"])
            frontier_out += int(ft["output"])

        # Accumulate raw success/eligible counts from extend_run_metrics.
        fpc = m.get("first_attempt_pass_counts") or {}
        for role in roles:
            rc = (fpc.get("by_role") or {}).get(role) or {}
            fpr_role_success[role] += int(rc.get("success", 0))
            fpr_role_eligible[role] += int(rc.get("eligible", 0))
        for cost, cc in (fpc.get("by_cost") or {}).items():
            fpr_cost_success[cost] = fpr_cost_success.get(cost, 0) + int(
                (cc or {}).get("success", 0)
            )
            fpr_cost_eligible[cost] = fpr_cost_eligible.get(cost, 0) + int(
                (cc or {}).get("eligible", 0)
            )

        run_tpd, run_tpd_note = _tasks_per_dollar(
            m["accepted_tasks"], m["dollars"], m["has_unknown_cost"]
        )
        per_run.append(
            {
                "run_id": m["run_id"],
                "outcome": m["outcome"],
                "accepted_tasks": m["accepted_tasks"],
                "dollars": m["dollars"],
                "has_unknown_cost": m["has_unknown_cost"],
                "tasks_per_dollar": run_tpd,
                "tasks_per_dollar_note": run_tpd_note,
                "free_share": m["free_share"],
                "subscription_share": m["subscription_share"],
                "metered_share": m["metered_share"],
                "total_model_attempts": m["total_model_attempts"],
                "attempts_by_role": m["attempts_by_role"],
                "frontier_tokens": m["frontier_tokens"],
                "first_attempt_pass_rate": m["first_attempt_pass_rate"],
            }
        )

    dollars: float | None = None if has_unknown_cost else total_cost

    tasks_per_dollar, tasks_per_dollar_note = _tasks_per_dollar(
        total_accepted, dollars, has_unknown_cost
    )

    if total_attempts > 0:
        free_share = free_attempts / total_attempts
        sub_share = sub_attempts / total_attempts
        metered_share = metered_attempts / total_attempts
        unknown_share = unknown_attempts / total_attempts
        paid_share = sub_share + metered_share
    else:
        free_share = sub_share = metered_share = unknown_share = paid_share = 0.0

    frontier_total: int | None = (frontier_in + frontier_out) if frontier_known else None
    fpt: float | None = (
        frontier_total / total_accepted
        if frontier_total is not None and total_accepted > 0
        else None
    )

    # Aggregate first-attempt pass rates using raw counts (node-weighted, not run-weighted).
    def _weighted_rate(success: int, eligible: int) -> float | None:
        return success / eligible if eligible > 0 else None

    agg_fpr_role: dict[str, float | None] = {
        r: _weighted_rate(fpr_role_success[r], fpr_role_eligible[r]) for r in roles
    }
    all_costs = set(fpr_cost_success) | set(fpr_cost_eligible)
    agg_fpr_cost: dict[str, float | None] = {
        c: _weighted_rate(fpr_cost_success.get(c, 0), fpr_cost_eligible.get(c, 0))
        for c in all_costs
    }

    return {
        "accepted_tasks": total_accepted,
        "dollars": dollars,
        "has_unknown_cost": has_unknown_cost,
        "tasks_per_dollar": tasks_per_dollar,
        "tasks_per_dollar_note": tasks_per_dollar_note,
        "free_share": free_share,
        "subscription_share": sub_share,
        "metered_share": metered_share,
        "unknown_capacity_share": unknown_share,
        "paid_model_share": paid_share,
        "total_model_attempts": total_attempts,
        "free_attempts": free_attempts,
        "subscription_attempts": sub_attempts,
        "metered_attempts": metered_attempts,
        "unknown_capacity_attempts": unknown_attempts,
        "attempts_by_role": attempts_by_role,
        "accepted_nodes_by_role": accepted_by_role,
        "frontier_tokens": {
            "input": frontier_in if frontier_known else None,
            "output": frontier_out if frontier_known else None,
            "total": frontier_total,
            "per_accepted_task": fpt,
        },
        "first_attempt_pass_rate": {"by_role": agg_fpr_role, "by_cost": agg_fpr_cost},
        "runs_processed": len(receipts),
        "per_run": per_run,
    }


def compute_headline_report(runs_dir: Path, since: date | None = None) -> dict[str, Any]:
    """Main entry point for the M0 headline metric report.

    Loads verified receipts from *runs_dir*, excludes fixture runs, and
    aggregates extended metrics.

    Returns a dict with:
    - All keys from ``aggregate_extended_metrics``
    - ``fixture_runs_excluded``: count of fixture runs excluded
    - ``live_runs_skipped``: count of live runs with verification problems
    - ``since``: the date filter applied (ISO string or null)
    - ``headline_accepted_per_paid_share``: float when computable, else null
    - ``headline_accepted_per_paid_share_note``: null when computable,
      else a string explaining why the value is unavailable
    """
    receipts, skipped_live, fixture_skipped = load_receipts_since(runs_dir, since=since)
    agg = aggregate_extended_metrics(receipts)

    # Headline metric from receipts (always computable when paid attempts exist).
    paid_share = agg.get("paid_model_share", 0.0)
    if isinstance(paid_share, float) and paid_share > 0:
        headline_value: float | None = agg["accepted_tasks"] / paid_share
        headline_note: str | None = None
    else:
        headline_value = None
        headline_note = "no paid model attempts in selected period"

    return {
        **agg,
        "fixture_runs_excluded": fixture_skipped,
        "live_runs_skipped": skipped_live,
        "since": since.isoformat() if since else None,
        "headline_accepted_per_paid_share": headline_value,
        "headline_accepted_per_paid_share_note": headline_note,
    }


def format_headline_report(data: dict[str, Any]) -> str:
    """Format the headline report as a human-readable string."""
    lines: list[str] = []
    since = data.get("since")
    if since:
        lines.append(f"M0 Headline Metric Report  (since {since})")
    else:
        lines.append("M0 Headline Metric Report  (all runs)")
    lines.append("=" * 60)

    runs = data.get("runs_processed", 0)
    skipped = data.get("live_runs_skipped", 0)
    fixtures = data.get("fixture_runs_excluded", 0)
    lines.append(f"Runs processed:   {runs}")
    if skipped:
        lines.append(f"Runs skipped:     {skipped} (verification failures)")
    if fixtures:
        lines.append(f"Fixture runs excluded: {fixtures} (not counted in live report)")
    lines.append("")

    # Core headline metrics
    accepted = data.get("accepted_tasks", 0)
    lines.append(f"Accepted tasks:   {accepted}")

    dollars = data.get("dollars")
    if dollars is not None:
        lines.append(f"Dollars spent:    ${dollars:.4f}")
    else:
        lines.append("Dollars spent:    null  (some model attempts have unknown cost)")
    tpd = data.get("tasks_per_dollar")
    if isinstance(tpd, float):
        lines.append(f"Accepted/dollar:  {tpd:.2f}")
    else:
        tpd_reason = data.get("tasks_per_dollar_note") or "unavailable"
        lines.append(f"Accepted/dollar:  null  ({tpd_reason})")

    lines.append("")

    # Paid model share headline (from receipts)
    h = data.get("headline_accepted_per_paid_share")
    h_note = data.get("headline_accepted_per_paid_share_note")
    paid_share = data.get("paid_model_share", 0.0)
    if isinstance(h, float):
        lines.append(
            f"Accepted / paid model share:  {h:.2f}"
            f"  (paid share={paid_share:.1%} of model attempts)"
        )
    else:
        reason = h_note or "unavailable"
        lines.append(f"Accepted / paid model share:  null  ({reason})")

    lines.append("")

    # Capacity class breakdown
    total_att = data.get("total_model_attempts", 0)
    if total_att > 0:
        lines.append("Model attempts by cost class:")
        lines.append(
            f"  free:         {data.get('free_attempts', 0):>5}  "
            f"({data.get('free_share', 0.0):.1%})"
        )
        lines.append(
            f"  subscription: {data.get('subscription_attempts', 0):>5}  "
            f"({data.get('subscription_share', 0.0):.1%})"
        )
        lines.append(
            f"  metered:      {data.get('metered_attempts', 0):>5}  "
            f"({data.get('metered_share', 0.0):.1%})"
        )
        if data.get("unknown_capacity_attempts", 0):
            lines.append(
                f"  unknown:      {data.get('unknown_capacity_attempts', 0):>5}  "
                f"({data.get('unknown_capacity_share', 0.0):.1%})"
            )
    lines.append("")

    # Per-role attempt breakdown
    abr = data.get("attempts_by_role")
    if abr and any(v > 0 for v in abr.values()):
        lines.append("Model attempts by role:")
        for role, count in sorted(abr.items()):
            if count > 0:
                lines.append(f"  {role:<12}: {count}")
    lines.append("")

    # Frontier tokens
    ft = data.get("frontier_tokens") or {}
    if ft.get("total") is not None:
        lines.append(f"Frontier tokens (sub+metered):  {ft['total']:,}")
        if ft.get("per_accepted_task") is not None:
            lines.append(f"  per accepted task:  {ft['per_accepted_task']:.0f}")
    else:
        lines.append("Frontier tokens: unknown (some attempts lack token data)")

    # First-attempt pass rate
    fpr = data.get("first_attempt_pass_rate") or {}
    by_role = fpr.get("by_role") or {}
    if any(v is not None for v in by_role.values()):
        lines.append("")
        lines.append("First-attempt pass rate by role:")
        for role, rate in sorted(by_role.items()):
            if rate is not None:
                lines.append(f"  {role:<12}: {rate:.1%}")

    by_cost = fpr.get("by_cost") or {}
    if any(v is not None for v in by_cost.values()):
        lines.append("")
        lines.append("First-attempt pass rate by cost class:")
        for cost, rate in sorted(by_cost.items()):
            if rate is not None:
                lines.append(f"  {cost:<12}: {rate:.1%}")

    lines.append("")
    lines.append(
        "Note: planner/reviewer individual call tokens are not recorded in "
        "receipts. Research-duplication share is not tracked in receipts."
    )

    return "\n".join(lines)
