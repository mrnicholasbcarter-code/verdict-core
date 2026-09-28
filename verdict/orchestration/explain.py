"""Data-only selection explanation for BOD-277 explorer.

Given a tuple of :class:`RouteVerdict` values from
:meth:`verdict.orchestration.eligibility.EligibilityLadder.evaluate` or
:meth:`~verdict.orchestration.eligibility.EligibilityLadder.select`, produce:

* a funnel with one count per :class:`EligibilityStage` (DISCOVERED..SELECTED);
* per-stage rejection reasons with counts;
* the selected route (if any), plus a ``selected_because`` list built ONLY by
  comparing the selected route's ``rank_components`` with the runner-up's.

The output is derived from ``RouteVerdict`` fields only. It never invents a
score, never guesses a value that was ``None``, and never claims a component
matters when the exposed values are equal. Prose is limited to the exact
components the sort key used.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from verdict.orchestration.contracts import EligibilityStage, RouteVerdict

# The seven ordered components of EligibilityLadder._rank_key. Kept in this
# module (rather than imported) so consumers can not accidentally depend on a
# private ladder attribute; a mismatch is caught by the unit test that sorts
# candidates on these exact keys and compares to the real selector.
_RANK_COMPONENT_ORDER: tuple[str, ...] = (
    "capacity_order",
    "slack",
    "price",
    "provider_pref",
    "load",
    "fit",
    "route_id",
)

# All stages a route may reach or fail at. Ordered so callers can iterate the
# funnel top-down. SELECTED is included so a "no selection" run reads 0.
_STAGES: tuple[EligibilityStage, ...] = tuple(EligibilityStage)


def _reached_index(reached: EligibilityStage | None) -> int:
    """Position of ``reached`` in _STAGES, or -1 when the route never reached DISCOVERED."""
    if reached is None:
        return -1
    return _STAGES.index(reached)


def _funnel(verdicts: Sequence[RouteVerdict]) -> dict[str, int]:
    """Cumulative funnel: routes that reached at least each stage."""
    counts: dict[str, int] = {stage.value: 0 for stage in _STAGES}
    for v in verdicts:
        top = _reached_index(v.reached)
        if top < 0:
            continue
        for stage in _STAGES[: top + 1]:
            counts[stage.value] += 1
    return counts


def _rejections(verdicts: Sequence[RouteVerdict]) -> dict[str, dict[str, int]]:
    """Rejection reason counts grouped by the stage the route failed at."""
    out: dict[str, dict[str, int]] = {}
    for v in verdicts:
        if v.failed_stage is None:
            continue
        stage = v.failed_stage.value
        reason = v.reason or "unknown"
        bucket = out.setdefault(stage, {})
        bucket[reason] = bucket.get(reason, 0) + 1
    return out


def _selected(verdicts: Sequence[RouteVerdict]) -> RouteVerdict | None:
    for v in verdicts:
        if v.reached is EligibilityStage.SELECTED:
            return v
    return None


def _runner_up(verdicts: Sequence[RouteVerdict], *, exclude: str) -> RouteVerdict | None:
    """The candidate ranked immediately after ``exclude`` (rank=1 in the ranking)."""
    ranked = [
        v
        for v in verdicts
        if v.route_id != exclude and v.rank is not None and v.rank_components is not None
    ]
    if not ranked:
        return None
    ranked.sort(key=lambda v: (v.rank if v.rank is not None else 0, v.route_id))
    return ranked[0]


def _format_capacity_order(value: Any) -> str:
    """Return a short human name for a capacity_order integer.

    Mirrors ``EligibilityLadder._CAPACITY_ORDER`` (subscription=0, free=1,
    metered=2, unknown=3). Falls back to the raw integer if it does not match.
    """
    mapping = {0: "subscription", 1: "free", 2: "metered", 3: "unknown"}
    if isinstance(value, int) and value in mapping:
        return mapping[value]
    return str(value)


def _describe(component: str, chosen: Any, other: Any) -> str:
    """One line comparing one rank component. Never invents a delta."""
    if component == "capacity_order":
        return (
            f"cheaper capacity class than runner-up: "
            f"{_format_capacity_order(chosen)} vs {_format_capacity_order(other)}"
        )
    if component == "slack":
        return f"less capability slack than runner-up (closer to task floor): {chosen} vs {other}"
    if component == "price":
        return f"lower price than runner-up: {chosen} vs {other}"
    if component == "provider_pref":
        return f"preferred provider before runner-up: {chosen} vs {other}"
    if component == "load":
        return f"lower current load than runner-up: {chosen} vs {other}"
    if component == "fit":
        # Higher fit wins; the sort key negates it, but we describe the raw fact.
        return f"better task fit than runner-up: {chosen} vs {other}"
    if component == "route_id":
        return f"tiebroken by route_id: {chosen} vs {other}"
    return f"{component}: {chosen} vs {other}"


def _selected_because(selected: RouteVerdict, runner_up: RouteVerdict | None) -> list[str]:
    """A list of strings explaining why ``selected`` outranked ``runner_up``.

    Rules:
    * Only compare when both verdicts have rank_components.
    * Walk the components in the exact rank order.
    * The first component whose values DIFFER is the deciding one; report it.
    * Report all preceding components (which were equal) as "equal on X".
    * If either side has an unknown (``None``) value at a component, report
      "one side unknown" rather than guessing a direction; keep walking.
    * If every component is equal or unknown, say so honestly.
    """
    if selected.rank_components is None or runner_up is None:
        return []
    other_components = runner_up.rank_components
    if other_components is None:
        return []
    reasons: list[str] = []
    decided = False
    for key in _RANK_COMPONENT_ORDER:
        left = selected.rank_components.get(key)
        right = other_components.get(key)
        if left is None or right is None:
            reasons.append(f"{key} unknown on one side: {left} vs {right}")
            continue
        if left == right:
            reasons.append(f"equal on {key}: {left}")
            continue
        reasons.append(_describe(key, left, right))
        decided = True
        break
    if not decided:
        # No differing component found. Say so; do not fabricate a reason.
        reasons.append("equal on every exposed rank component (order is stable)")
    return reasons


def explain_selection(verdicts: Sequence[RouteVerdict]) -> dict[str, Any]:
    """Data-only explanation for a set of :class:`RouteVerdict` values.

    Returned mapping (all keys always present):

    * ``funnel``: ``{stage: count}`` for every :class:`EligibilityStage`, where
      count is the number of routes that reached at least that stage.
    * ``rejections``: ``{failed_stage: {reason: count}}``.
    * ``selected``: the ``to_dict()`` of the SELECTED verdict, or ``None``.
    * ``runner_up``: the ``to_dict()`` of the next-ranked candidate, or ``None``.
    * ``selected_because``: a list of short strings, each grounded in a
      component the real ranking uses; empty when there is no selection or the
      selector did not compute rank_components (e.g. failed before ranking).
    """
    selected = _selected(verdicts)
    runner_up: RouteVerdict | None = None
    reasons: list[str] = []
    if selected is not None:
        runner_up = _runner_up(verdicts, exclude=selected.route_id)
        reasons = _selected_because(selected, runner_up)
    return {
        "funnel": _funnel(verdicts),
        "rejections": _rejections(verdicts),
        "selected": selected.to_dict() if selected is not None else None,
        "runner_up": runner_up.to_dict() if runner_up is not None else None,
        "selected_because": reasons,
    }


__all__ = ["explain_selection"]
