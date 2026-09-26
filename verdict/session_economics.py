"""Session economics — STAY / SWITCH / BLOCKED (session STAY/SWITCH decisions).

Pure decision core over already-qualified concrete routes.  Expected remaining
session cost (not next-call price alone) drives economics; hard eligibility,
capability-tier deficits, and quota/cooldown override cost-driven stickiness.

Consumes BOD-54 ``CostTerm`` / ``ExpectedStrategyCost`` without forking
arithmetic.  Optionally records BOD-92 runtime evidence (quota/cooldown/cache)
as inputs — never as routing authority.

Does **not** own strategy integration (BOD-104), cost_ledger ownership,
runtime_certification ownership, or bounded recovery (BOD-55).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any, Literal

from verdict.cost_ledger import (
    COST_LEDGER_SCHEMA_VERSION,
    CacheEvidenceInput,
    CostTerm,
    CostTermStatus,
    QuotaEvidenceInput,
)
from verdict.expected_cost import EXPECTED_COST_SCHEMA_VERSION, ExpectedStrategyCost

SESSION_ECONOMICS_SCHEMA_VERSION = "1"

SessionDecision = Literal["STAY", "SWITCH", "BLOCKED"]


class SessionEconomicsError(ValueError):
    """Raised when session-economics inputs violate the decision contract."""


class DecisionMode(str, Enum):
    """Policy authority mode for calibration before promotion."""

    AUTHORITATIVE = "authoritative"
    SHADOW = "shadow"
    COUNTERFACTUAL = "counterfactual"


def _utc(value: datetime | None, field_name: str) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        raise SessionEconomicsError(f"{field_name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _format_datetime(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _as_decimal(value: Decimal | int | str | float | None) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value if value.is_finite() else None
    try:
        amount = Decimal(str(value))
    except Exception:
        return None
    return amount if amount.is_finite() else None


@dataclass(frozen=True)
class ConcreteRoute:
    """Concrete execution identity retained across STAY/SWITCH decisions."""

    route_id: str
    gateway: str
    provider: str
    model: str
    credential_pool: str | None
    capability_tier: int
    eligible: bool
    excluded: bool = False
    exclusion_reason: str | None = None

    def __post_init__(self) -> None:
        if not self.route_id or not str(self.route_id).strip():
            raise SessionEconomicsError("route_id must be non-empty")
        if self.capability_tier < 0:
            raise SessionEconomicsError("capability_tier must be non-negative")

    @property
    def hard_ineligible(self) -> bool:
        return self.excluded or not self.eligible

    def to_dict(self) -> dict[str, Any]:
        return {
            "route_id": self.route_id,
            "gateway": self.gateway,
            "provider": self.provider,
            "model": self.model,
            "credential_pool": self.credential_pool,
            "capability_tier": self.capability_tier,
            "eligible": self.eligible,
            "excluded": self.excluded,
            "exclusion_reason": self.exclusion_reason,
        }


@dataclass(frozen=True)
class PromptCacheState:
    """Observable prompt-cache warmth. Unknown savings never invent discounts."""

    warm: bool
    savings_usd: Decimal | None
    cached_tokens: int | None
    fresh_until: datetime | None
    evidence_id: str | None = None
    status: CostTermStatus = "unknown"

    def __post_init__(self) -> None:
        object.__setattr__(self, "fresh_until", _utc(self.fresh_until, "fresh_until"))
        if self.savings_usd is not None:
            amount = _as_decimal(self.savings_usd)
            if amount is None or amount < 0:
                raise SessionEconomicsError("savings_usd must be a non-negative finite decimal")
            object.__setattr__(self, "savings_usd", amount)
        if self.status not in {"observed", "estimated", "unknown", "assumed"}:
            raise SessionEconomicsError(f"invalid cache status: {self.status!r}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "warm": self.warm,
            "savings_usd": None if self.savings_usd is None else str(self.savings_usd),
            "cached_tokens": self.cached_tokens,
            "fresh_until": _format_datetime(self.fresh_until),
            "evidence_id": self.evidence_id,
            "status": self.status,
        }


@dataclass(frozen=True)
class SessionState:
    """Pinned session identity and hysteresis counters."""

    session_id: str
    current_route: ConcreteRoute
    last_served_route: ConcreteRoute | None = None
    cache: PromptCacheState | None = None
    consecutive_switch_signals: int = 0
    consecutive_stay_signals: int = 0
    recent_switches: int = 0
    max_switches: int = 8
    switch_signal_threshold: int = 2
    quota_exhausted: bool = False
    cooldown_active: bool = False
    health_unusable: bool = False
    serving_failures: int = 0
    # Optional runtime certification (passport) evidence shapes (read-only consume).
    quota_evidence: QuotaEvidenceInput | None = None
    cache_evidence: CacheEvidenceInput | None = None

    def __post_init__(self) -> None:
        if not self.session_id or not str(self.session_id).strip():
            raise SessionEconomicsError("session_id must be non-empty")
        if self.switch_signal_threshold < 1:
            raise SessionEconomicsError("switch_signal_threshold must be >= 1")
        if self.max_switches < 0:
            raise SessionEconomicsError("max_switches must be non-negative")


@dataclass(frozen=True)
class TaskState:
    """Task requirements that can override cost-driven STAY."""

    required_capability_tier: int
    remaining_horizon_turns: int | None = None
    action_class: str | None = None

    def __post_init__(self) -> None:
        if self.required_capability_tier < 0:
            raise SessionEconomicsError("required_capability_tier must be non-negative")


@dataclass(frozen=True)
class CostState:
    """Expected remaining costs for STAY vs SWITCH trajectories.

    ``stay_expected`` / ``switch_expected`` are BOD-54 strategy receipts.
    Cache savings apply only when the term is observed **and** still fresh.
    """

    stay_expected: ExpectedStrategyCost
    switch_expected: ExpectedStrategyCost
    stay_cache_savings: CostTerm | None = None
    switch_handoff_cost: CostTerm | None = None
    hysteresis_margin_usd: Decimal = field(default_factory=lambda: Decimal("0.01"))
    now: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self) -> None:
        object.__setattr__(self, "now", _utc(self.now, "now"))
        margin = _as_decimal(self.hysteresis_margin_usd)
        if margin is None or margin < 0:
            raise SessionEconomicsError("hysteresis_margin_usd must be non-negative")
        object.__setattr__(self, "hysteresis_margin_usd", margin)


@dataclass(frozen=True)
class SessionRouteDecision:
    """Inspectable STAY/SWITCH/BLOCKED receipt."""

    decision: SessionDecision
    selected_route_id: str | None
    selected_route: ConcreteRoute | None
    reason: str
    override_reasons: tuple[str, ...]
    terms: tuple[CostTerm, ...]
    assumptions: tuple[str, ...]
    freshness: Mapping[str, Any]
    stay_ev: Decimal | None
    switch_ev: Decimal | None
    mode: DecisionMode
    authoritative: bool
    would_decide: SessionDecision
    alternatives: tuple[dict[str, Any], ...]
    schema_version: str = SESSION_ECONOMICS_SCHEMA_VERSION
    ledger_schema_version: str = COST_LEDGER_SCHEMA_VERSION
    expected_cost_schema_version: str = EXPECTED_COST_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "ledger_schema_version": self.ledger_schema_version,
            "expected_cost_schema_version": self.expected_cost_schema_version,
            "decision": self.decision,
            "would_decide": self.would_decide,
            "selected_route_id": self.selected_route_id,
            "selected_route": None
            if self.selected_route is None
            else self.selected_route.to_dict(),
            "reason": self.reason,
            "override_reasons": list(self.override_reasons),
            "assumptions": list(self.assumptions),
            "freshness": dict(self.freshness),
            "stay_ev": None if self.stay_ev is None else str(self.stay_ev),
            "switch_ev": None if self.switch_ev is None else str(self.switch_ev),
            "mode": self.mode.value,
            "authoritative": self.authoritative,
            "terms": [term.to_dict() for term in self.terms],
            "alternatives": list(self.alternatives),
        }


def _term_fresh(term: CostTerm | None, now: datetime) -> bool:
    if term is None:
        return False
    if term.fresh_until is None:
        # No TTL → treat as fresh only when observed (conservative for estimated).
        return term.status == "observed"
    return term.fresh_until > now


def _usd_amount(term: CostTerm | None) -> Decimal | None:
    if term is None or term.unit != "usd":
        return None
    if term.status == "unknown" or term.amount is None:
        return None
    return term.amount


def _apply_cache_savings(
    *,
    stay_cash: Decimal | None,
    savings_term: CostTerm | None,
    cache: PromptCacheState | None,
    now: datetime,
    assumptions: list[str],
    terms: list[CostTerm],
) -> Decimal | None:
    """Subtract observed fresh cache savings; never invent when unknown/stale."""

    if stay_cash is None:
        return None

    if savings_term is not None:
        terms.append(savings_term)
        if savings_term.status == "unknown" or savings_term.amount is None:
            assumptions.append("cache_savings_unknown_ignored")
            return stay_cash
        if not _term_fresh(savings_term, now):
            assumptions.append("cache_savings_stale_ignored")
            return stay_cash
        if savings_term.unit != "usd":
            assumptions.append("cache_savings_non_usd_ignored")
            return stay_cash
        return max(Decimal("0"), stay_cash - savings_term.amount)

    if cache is not None:
        if cache.status == "unknown" or cache.savings_usd is None:
            assumptions.append("cache_savings_unknown_ignored")
            return stay_cash
        if cache.fresh_until is not None and cache.fresh_until <= now:
            assumptions.append("cache_savings_stale_ignored")
            return stay_cash
        return max(Decimal("0"), stay_cash - cache.savings_usd)

    return stay_cash


def _switch_total(
    *,
    switch_cash: Decimal | None,
    handoff: CostTerm | None,
    terms: list[CostTerm],
    assumptions: list[str],
) -> Decimal | None:
    if switch_cash is None:
        return None
    total = switch_cash
    if handoff is not None:
        terms.append(handoff)
        amount = _usd_amount(handoff)
        if amount is None:
            assumptions.append("handoff_cost_unknown_conservative")
            # Unknown handoff → do not invent zero; leave EV unknown for switch.
            return None
        total += amount
    return total


def _collect_freshness(*, cost: CostState, session: SessionState) -> dict[str, Any]:
    stay_terms = [
        {
            "kind": term.kind,
            "status": term.status,
            "fresh_until": _format_datetime(term.fresh_until),
            "observed_at": _format_datetime(term.observed_at),
        }
        for term in cost.stay_expected.terms
    ]
    switch_terms = [
        {
            "kind": term.kind,
            "status": term.status,
            "fresh_until": _format_datetime(term.fresh_until),
            "observed_at": _format_datetime(term.observed_at),
        }
        for term in cost.switch_expected.terms
    ]
    cache_fresh = None
    if session.cache is not None:
        cache_fresh = {
            "status": session.cache.status,
            "fresh_until": _format_datetime(session.cache.fresh_until),
            "evidence_id": session.cache.evidence_id,
        }
    savings_fresh = None
    if cost.stay_cache_savings is not None:
        savings_fresh = {
            "status": cost.stay_cache_savings.status,
            "fresh_until": _format_datetime(cost.stay_cache_savings.fresh_until),
            "evidence_id": cost.stay_cache_savings.evidence_id,
        }
    return {
        "as_of": _format_datetime(cost.now),
        "stay_terms": stay_terms,
        "switch_terms": switch_terms,
        "cache": cache_fresh,
        "stay_cache_savings": savings_fresh,
    }


def _hard_override_switch(
    session: SessionState, candidate: ConcreteRoute, task: TaskState
) -> list[str]:
    """Hard overrides that force SWITCH when candidate is eligible."""

    if candidate.hard_ineligible:
        return []
    reasons: list[str] = []
    current = session.current_route
    if current.hard_ineligible:
        reasons.append("current_hard_ineligible")
    if (
        current.capability_tier < task.required_capability_tier
        and candidate.capability_tier >= task.required_capability_tier
    ):
        reasons.append("capability_tier_deficit")
    if session.quota_exhausted:
        reasons.append("quota_exhausted")
    if session.cooldown_active:
        reasons.append("cooldown_active")
    if session.health_unusable:
        reasons.append("health_unusable")
    if session.serving_failures >= 3:
        reasons.append("repeated_serving_failures")
    # Quota evidence remaining_pct == 0 is an explicit exhaustion signal.
    if session.quota_evidence is not None:
        remaining = session.quota_evidence.get("remaining_pct")
        if remaining is not None and float(remaining) <= 0.0 and "quota_exhausted" not in reasons:
            reasons.append("quota_exhausted")
    return reasons


def decide_session_route(
    session_state: SessionState,
    fresh_qualified_route: ConcreteRoute,
    cost_state: CostState,
    task_state: TaskState,
    *,
    mode: DecisionMode | Literal["authoritative", "shadow", "counterfactual"] = "authoritative",
    counterfactual_force_stay: bool = False,
    counterfactual_force_switch: bool = False,
) -> SessionRouteDecision:
    """Decide STAY | SWITCH | BLOCKED for the current session route.

    Hard eligibility is evaluated before economics.  Excluded candidates can
    never be selected.  Unknown cache savings never invent a discount.
    """

    if isinstance(mode, str):
        mode = DecisionMode(mode)

    assumptions: list[str] = []
    terms: list[CostTerm] = list(cost_state.stay_expected.terms) + list(
        cost_state.switch_expected.terms
    )
    overrides: list[str] = []
    current = session_state.current_route
    candidate = fresh_qualified_route

    stay_ev = _apply_cache_savings(
        stay_cash=cost_state.stay_expected.cash_usd,
        savings_term=cost_state.stay_cache_savings,
        cache=session_state.cache,
        now=cost_state.now,
        assumptions=assumptions,
        terms=terms,
    )
    switch_ev = _switch_total(
        switch_cash=cost_state.switch_expected.cash_usd,
        handoff=cost_state.switch_handoff_cost,
        terms=terms,
        assumptions=assumptions,
    )

    freshness = _collect_freshness(cost=cost_state, session=session_state)
    alternatives = (
        {
            "role": "current",
            "route": current.to_dict(),
            "expected_ev": None if stay_ev is None else str(stay_ev),
        },
        {
            "role": "candidate",
            "route": candidate.to_dict(),
            "expected_ev": None if switch_ev is None else str(switch_ev),
        },
    )

    def _finish(
        decision: SessionDecision,
        *,
        reason: str,
        selected: ConcreteRoute | None,
        would: SessionDecision | None = None,
    ) -> SessionRouteDecision:
        natural = would if would is not None else decision
        final = decision
        auth = mode is DecisionMode.AUTHORITATIVE
        selected_route = selected

        if mode is DecisionMode.SHADOW:
            final = natural
            auth = False
            assumptions.append("shadow_mode_non_authoritative")
        elif mode is DecisionMode.COUNTERFACTUAL:
            auth = False
            if counterfactual_force_stay:
                assumptions.append("counterfactual_force_stay")
                final = "STAY"
                selected_route = current
            elif counterfactual_force_switch and not candidate.hard_ineligible:
                assumptions.append("counterfactual_force_switch")
                final = "SWITCH"
                selected_route = candidate
            else:
                final = natural
            assumptions.append("counterfactual_mode_non_authoritative")

        if final == "STAY":
            if selected_route is None:
                selected_route = current
            selected_id: str | None = selected_route.route_id
        elif final == "SWITCH":
            if selected_route is None:
                selected_route = candidate
            selected_id = selected_route.route_id
        else:
            selected_route = None
            selected_id = None

        return SessionRouteDecision(
            decision=final,
            selected_route_id=selected_id,
            selected_route=selected_route,
            reason=reason,
            override_reasons=tuple(overrides),
            terms=tuple(terms),
            assumptions=tuple(dict.fromkeys(assumptions)),
            freshness=freshness,
            stay_ev=stay_ev,
            switch_ev=switch_ev,
            mode=mode,
            authoritative=auth,
            would_decide=natural,
            alternatives=alternatives,
        )

    # --- Hard eligibility gate (before economics) ---
    candidate_usable = not candidate.hard_ineligible and cost_state.switch_expected.qualified
    current_usable = not current.hard_ineligible

    if not candidate_usable:
        assumptions.append("candidate_hard_ineligible")
        if not current_usable:
            overrides.append("no_eligible_route")
            return _finish("BLOCKED", reason="blocked:no_eligible_route", selected=None)
        return _finish("STAY", reason="stay:candidate_hard_ineligible", selected=current)

    # Candidate is usable. Check hard overrides that force SWITCH.
    overrides.extend(_hard_override_switch(session_state, candidate, task_state))
    if overrides:
        return _finish("SWITCH", reason=f"switch:override:{overrides[0]}", selected=candidate)

    if not current_usable:
        return _finish("SWITCH", reason="switch:current_hard_ineligible", selected=candidate)

    # Transition budget: prevent indefinite thrashing.
    if session_state.recent_switches >= session_state.max_switches:
        assumptions.append("max_switches_reached")
        return _finish("STAY", reason="stay:max_switches_hysteresis", selected=current)

    # --- Expected-value comparison with hysteresis ---
    margin = cost_state.hysteresis_margin_usd
    assert margin is not None

    cost_prefers_switch = False
    if stay_ev is not None and switch_ev is not None:
        # SWITCH must beat STAY by the hysteresis margin.
        if switch_ev + margin < stay_ev:
            cost_prefers_switch = True
        elif switch_ev < stay_ev:
            assumptions.append("switch_cheaper_within_hysteresis_margin")
        else:
            assumptions.append("stay_economically_competitive")
    elif stay_ev is None and switch_ev is not None:
        assumptions.append("stay_ev_unknown_conservative")
    elif switch_ev is None and stay_ev is not None:
        assumptions.append("switch_ev_unknown_conservative")
    else:
        assumptions.append("both_ev_unknown_conservative")

    if cost_prefers_switch:
        signals = session_state.consecutive_switch_signals + 1
        if signals >= session_state.switch_signal_threshold:
            return _finish("SWITCH", reason="switch:expected_cost_beats_stay", selected=candidate)
        assumptions.append("hysteresis_signal_insufficient")
        return _finish("STAY", reason="stay:hysteresis_hold", selected=current)

    return _finish("STAY", reason="stay:expected_cost_or_hysteresis", selected=current)


def apply_runtime_evidence(
    session: SessionState,
    *,
    quota: QuotaEvidenceInput | None = None,
    cache: CacheEvidenceInput | None = None,
    quota_exhausted: bool | None = None,
    cooldown_active: bool | None = None,
    health_unusable: bool | None = None,
) -> SessionState:
    """Merge optional runtime certification (passport) evidence into session state (immutable replace)."""

    exhausted = session.quota_exhausted if quota_exhausted is None else quota_exhausted
    remaining_pct = None if quota is None else quota.get("remaining_pct")
    if remaining_pct is not None and float(remaining_pct) <= 0.0:
        exhausted = True
    return SessionState(
        session_id=session.session_id,
        current_route=session.current_route,
        last_served_route=session.last_served_route,
        cache=session.cache,
        consecutive_switch_signals=session.consecutive_switch_signals,
        consecutive_stay_signals=session.consecutive_stay_signals,
        recent_switches=session.recent_switches,
        max_switches=session.max_switches,
        switch_signal_threshold=session.switch_signal_threshold,
        quota_exhausted=exhausted,
        cooldown_active=session.cooldown_active if cooldown_active is None else cooldown_active,
        health_unusable=session.health_unusable if health_unusable is None else health_unusable,
        serving_failures=session.serving_failures,
        quota_evidence=quota if quota is not None else session.quota_evidence,
        cache_evidence=cache if cache is not None else session.cache_evidence,
    )


__all__ = [
    "SESSION_ECONOMICS_SCHEMA_VERSION",
    "ConcreteRoute",
    "CostState",
    "DecisionMode",
    "PromptCacheState",
    "SessionDecision",
    "SessionEconomicsError",
    "SessionRouteDecision",
    "SessionState",
    "TaskState",
    "apply_runtime_evidence",
    "decide_session_route",
]
