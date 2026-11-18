"""Shared live route selection through the orchestration eligibility ladder."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from verdict.orchestration.contracts import RouteVerdict, TaskRequirements
from verdict.orchestration.eligibility_report import build_selector
from verdict.orchestration.explain import explain_selection


@dataclass(frozen=True)
class SelectionResult:
    route: RouteVerdict | None
    reasons: tuple[str, ...]
    provenance: dict[str, Any]
    explanation: dict[str, Any]
    # No route, and the candidates left were skipped only because another producer
    # holds verified-refresh.lock. Transient: the caller may wait and retry.
    lease_held: bool = False


#: Exit status of ``verdict route`` / ``verdict choose`` for ``lease_held`` (EX_TEMPFAIL).
LEASE_HELD_EXIT = 75
#: The reason the ladder writes on a candidate it skipped under the refresh lease.
LEASE_HELD_REASON = "lease_held"


def no_route_outcome(selection: SelectionResult, *, explicit_model: bool) -> tuple[str, int]:
    """Reason and exit status when selection chose nothing."""
    if selection.lease_held:
        return LEASE_HELD_REASON, LEASE_HELD_EXIT
    return ("explicit_model_ineligible" if explicit_model else "no_eligible_route"), 1


def select_route_for_task(
    requirements: TaskRequirements,
    *,
    gateway: str,
    gateway_kind: str | None = None,
    capacity: tuple[str, ...] = (),
    explicit_model: str | None = None,
    now: datetime | None = None,
    no_confirm: bool = False,
) -> SelectionResult:
    """Select with the existing admission, ranking and bounded confirmation.

    Capacity filters narrow admission; D6 tier-first ordering applies afterward. This
    call can send a model confirmation through the ladder for unproven routes.

    ``requirements.task_category`` feeds the optional OmniRoute advisory rankings
    as a task-aware tie-break among already-admitted routes.
    """
    now = now or datetime.now(timezone.utc)
    selector = build_selector(
        gateway,
        scope="",
        prefer="",
        required_capabilities=requirements.required_capabilities,
        min_context_tokens=requirements.min_context_tokens,
        capacity=capacity,
        route_ids=frozenset({explicit_model}) if explicit_model else None,
        visibility_executor="direct-gateway",
        no_confirm=no_confirm,
        now=now,
        task_category=requirements.task_category,
        gateway_kind=gateway_kind,
    )
    route, verdicts = selector.select(requirements, now=now)
    explanation = explain_selection(verdicts)
    reasons = tuple(explanation["selected_because"])
    if not reasons:
        reasons = (route.reason,) if route else tuple(sorted({v.reason for v in verdicts}))
    if not reasons:
        reasons = ("no_candidates",)
    provenance = dict(route.rank_components or {}) if route else {}
    # Under the lease the ladder sends no probe, so a candidate is either chosen
    # or marked lease_held; with no route chosen, lease_held names what is left.
    lease_held = route is None and any(v.reason == LEASE_HELD_REASON for v in verdicts)
    return SelectionResult(route, reasons, provenance, explanation, lease_held)
