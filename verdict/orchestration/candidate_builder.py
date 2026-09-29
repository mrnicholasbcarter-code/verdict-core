"""Shared helpers for building eligibility candidate and rejection structures.

These are used by both :mod:`verdict.orchestration.runtime` (to record events)
and :mod:`verdict.orchestration.routing_view` (to project inventory-mode
evaluations without re-deriving from raw events).

Both callers import from here; runtime.py re-exports via its own names so
existing internal callers inside that module are unaffected.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

MAX_CANDIDATES = 100  # AC7: raised from 25; bounded to keep events small


def build_rejections(verdicts: Sequence[Any]) -> dict[str, dict[str, int]]:
    """Aggregate rejection counts by stage and reason over all considered routes."""
    rejections: dict[str, dict[str, int]] = {}
    for v in verdicts:
        stage = getattr(v, "failed_stage", None)
        if stage is None:
            continue
        stage_key = stage.value if hasattr(stage, "value") else str(stage)
        reason = getattr(v, "reason", "") or "unknown"
        bucket = rejections.setdefault(stage_key, {})
        bucket[reason] = bucket.get(reason, 0) + 1
    return rejections


def _candidate_state_key(d: dict[str, Any]) -> str:
    """Derive a simple state label from a verdict dict for omitted_summary."""
    failed = d.get("failed_stage")
    if failed is None:
        return "selected" if d.get("reached") == "SELECTED" else "admitted"
    reason = d.get("reason") or ""
    if "cooldown" in reason:
        return "cooldown"
    return "rejected"


def build_candidates(
    verdicts: Sequence[Any], selected_route: str | None
) -> tuple[list[dict[str, Any]], int, dict[str, Any] | None]:
    """Build at most MAX_CANDIDATES candidate dicts; selected route always included.

    Returns ``(candidates_list, omitted_count, omitted_summary)``.
    omitted_summary is None when nothing was omitted; otherwise maps
    state_name -> {"count": N, "first_reason": str}.
    """
    total = len(verdicts)
    if total == 0:
        return [], 0, None

    selected_verdict: dict[str, Any] | None = None
    others: list[dict[str, Any]] = []

    for v in verdicts:
        d = v.to_dict() if hasattr(v, "to_dict") else {"route_id": str(getattr(v, "route_id", ""))}
        rid = d.get("route_id", "")
        if selected_route and rid == selected_route and selected_verdict is None:
            selected_verdict = d
        else:
            others.append(d)

    budget = MAX_CANDIDATES - (1 if selected_verdict else 0)
    kept = others[:budget]
    result = [selected_verdict, *kept] if selected_verdict else kept[:MAX_CANDIDATES]
    omitted_dicts = others[budget:] if len(others) > budget else []

    omitted = total - len(result)

    # AC7: build per-state summary for omitted routes
    omitted_summary: dict[str, Any] | None = None
    if omitted_dicts:
        state_info: dict[str, dict[str, Any]] = {}
        for d in omitted_dicts:
            sk = _candidate_state_key(d)
            if sk not in state_info:
                state_info[sk] = {"count": 0, "first_reason": d.get("reason") or "unknown"}
            state_info[sk]["count"] += 1
        omitted_summary = state_info

    return result, omitted, omitted_summary


__all__ = ["MAX_CANDIDATES", "build_candidates", "build_rejections"]
