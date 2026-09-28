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

MAX_CANDIDATES = 25


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


def build_candidates(
    verdicts: Sequence[Any], selected_route: str | None
) -> tuple[list[dict[str, Any]], int]:
    """Build at most MAX_CANDIDATES candidate dicts; selected route always included.

    Returns ``(candidates_list, omitted_count)``.
    """
    total = len(verdicts)
    if total == 0:
        return [], 0

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
    omitted = total - len(result)
    return result, omitted


__all__ = ["MAX_CANDIDATES", "build_candidates", "build_rejections"]
