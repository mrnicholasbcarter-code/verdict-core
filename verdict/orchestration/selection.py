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


def select_route_for_task(
    requirements: TaskRequirements,
    *,
    gateway: str,
    capacity: tuple[str, ...] = (),
    explicit_model: str | None = None,
    now: datetime | None = None,
) -> SelectionResult:
    """Select with the existing admission, ranking and bounded confirmation.

    Capacity is an opt-in admission filter, never a ranking preference. This
    call can send a model confirmation through the ladder for unproven routes.
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
        now=now,
    )
    route, verdicts = selector.select(requirements, now=now)
    explanation = explain_selection(verdicts)
    reasons = tuple(explanation["selected_because"])
    if not reasons:
        reasons = (route.reason,) if route else tuple(sorted({v.reason for v in verdicts}))
    if not reasons:
        reasons = ("no_candidates",)
    provenance = dict(route.rank_components or {}) if route else {}
    return SelectionResult(route, reasons, provenance, explanation)
