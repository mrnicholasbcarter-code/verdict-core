"""Proof fixtures for session economics STAY/SWITCH (BOD-119)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from verdict.cost_ledger import CostTerm, CostTermStatus
from verdict.expected_cost import ExpectedStrategyCost
from verdict.session_economics import (
    ConcreteRoute,
    CostState,
    DecisionMode,
    PromptCacheState,
    SessionState,
    TaskState,
    decide_session_route,
)

NOW = datetime(2026, 9, 19, 12, 0, tzinfo=timezone.utc)


def _route(
    route_id: str,
    *,
    model: str = "model-a",
    tier: int = 2,
    eligible: bool = True,
    excluded: bool = False,
    exclusion_reason: str | None = None,
    provider: str = "provider-a",
    gateway: str = "gateway-a",
    pool: str = "pool-a",
) -> ConcreteRoute:
    return ConcreteRoute(
        route_id=route_id,
        gateway=gateway,
        provider=provider,
        model=model,
        credential_pool=pool,
        capability_tier=tier,
        eligible=eligible,
        excluded=excluded,
        exclusion_reason=exclusion_reason,
    )


def _strategy(
    strategy_id: str,
    cash_usd: Decimal | None,
    *,
    terms: tuple[CostTerm, ...] | None = None,
    qualified: bool = True,
) -> ExpectedStrategyCost:
    built_terms: tuple[CostTerm, ...]
    if terms is not None:
        built_terms = terms
    elif cash_usd is None:
        built_terms = (CostTerm(kind="execution", amount=None, unit="usd", status="unknown"),)
    else:
        built_terms = (
            CostTerm(
                kind="execution", amount=cash_usd, unit="usd", status="estimated", observed_at=NOW
            ),
        )
    return ExpectedStrategyCost.build(
        strategy_id=strategy_id,
        trajectory_id="traj-session",
        terms=built_terms,
        qualified=qualified,
    )


def _cache(
    *,
    savings_usd: Decimal | None,
    fresh_until: datetime | None,
    warm: bool = True,
    status: CostTermStatus = "observed",
) -> PromptCacheState:
    return PromptCacheState(
        warm=warm,
        savings_usd=savings_usd,
        cached_tokens=800 if warm else None,
        fresh_until=fresh_until,
        evidence_id="cache-1",
        status=status,
    )


def test_warm_expensive_beats_slightly_cheaper_cold_stay() -> None:
    """Proof 1: warm expensive route vs slightly cheaper cold → STAY."""

    warm = _route("warm-expensive", model="gpt-expensive", tier=3)
    cold = _route("cold-cheap", model="gpt-cheap", tier=3, provider="provider-b", pool="pool-b")
    # Stay cash 0.10 but observed cache savings 0.04 → effective 0.06
    # Switch cash 0.08 + handoff 0.03 → effective 0.11
    stay = _strategy("stay", Decimal("0.10"))
    switch = _strategy("switch", Decimal("0.08"))
    cache = _cache(savings_usd=Decimal("0.04"), fresh_until=NOW + timedelta(minutes=5))
    handoff = CostTerm(
        kind="route_switch",
        amount=Decimal("0.03"),
        unit="usd",
        status="estimated",
        observed_at=NOW,
        notes="handoff_recontextualization",
    )
    result = decide_session_route(
        SessionState(session_id="s1", current_route=warm, last_served_route=warm, cache=cache),
        cold,
        CostState(
            stay_expected=stay,
            switch_expected=switch,
            stay_cache_savings=CostTerm(
                kind="cache_read",
                amount=Decimal("0.04"),
                unit="usd",
                status="observed",
                evidence_id="cache-1",
                observed_at=NOW,
                fresh_until=NOW + timedelta(minutes=5),
                notes="observed_cache_savings",
            ),
            switch_handoff_cost=handoff,
            now=NOW,
        ),
        TaskState(required_capability_tier=2),
    )
    assert result.decision == "STAY"
    assert result.selected_route_id == "warm-expensive"
    assert result.stay_ev is not None and result.switch_ev is not None
    assert result.stay_ev < result.switch_ev


def test_warm_underpowered_switches_to_required_tier() -> None:
    """Proof 2: warm underpowered vs required higher tier → SWITCH."""

    warm = _route("warm-weak", model="tiny", tier=1)
    strong = _route("strong", model="flagship", tier=4, provider="provider-b")
    cache = _cache(savings_usd=Decimal("0.50"), fresh_until=NOW + timedelta(hours=1))
    result = decide_session_route(
        SessionState(
            session_id="s2",
            current_route=warm,
            last_served_route=warm,
            cache=cache,
            consecutive_switch_signals=0,
        ),
        strong,
        CostState(
            stay_expected=_strategy("stay", Decimal("0.05")),
            switch_expected=_strategy("switch", Decimal("0.40")),
            stay_cache_savings=CostTerm(
                kind="cache_read",
                amount=Decimal("0.50"),
                unit="usd",
                status="observed",
                observed_at=NOW,
                fresh_until=NOW + timedelta(hours=1),
            ),
            switch_handoff_cost=CostTerm(
                kind="route_switch", amount=Decimal("0.02"), unit="usd", status="estimated"
            ),
            now=NOW,
        ),
        TaskState(required_capability_tier=3),
    )
    assert result.decision == "SWITCH"
    assert result.selected_route_id == "strong"
    assert "capability_tier_deficit" in result.override_reasons


def test_expired_cache_ttl_uses_raw_economics_no_phantom() -> None:
    """Proof 3: cache TTL expired → raw economics (no phantom cache)."""

    warm = _route("expired-warm", model="expensive", tier=3)
    cold = _route("fresh-cold", model="cheap", tier=3, provider="provider-b")
    # Without cache savings, switch is cheaper: stay 0.12 vs switch 0.08+0.01
    result = decide_session_route(
        SessionState(
            session_id="s3",
            current_route=warm,
            last_served_route=warm,
            cache=_cache(
                savings_usd=Decimal("0.10"), fresh_until=NOW - timedelta(minutes=1), warm=True
            ),
            consecutive_switch_signals=2,
        ),
        cold,
        CostState(
            stay_expected=_strategy("stay", Decimal("0.12")),
            switch_expected=_strategy("switch", Decimal("0.08")),
            stay_cache_savings=CostTerm(
                kind="cache_read",
                amount=Decimal("0.10"),
                unit="usd",
                status="observed",
                observed_at=NOW - timedelta(hours=1),
                fresh_until=NOW - timedelta(minutes=1),
                notes="stale_should_not_apply",
            ),
            switch_handoff_cost=CostTerm(
                kind="route_switch", amount=Decimal("0.01"), unit="usd", status="estimated"
            ),
            hysteresis_margin_usd=Decimal("0.005"),
            now=NOW,
        ),
        TaskState(required_capability_tier=2),
    )
    assert result.decision == "SWITCH"
    assert "cache_savings_stale_ignored" in result.assumptions
    # Stay EV must equal raw stay cash — no phantom discount.
    assert result.stay_ev == Decimal("0.12")
    assert result.switch_ev == Decimal("0.09")


def test_quota_exhausted_switches_to_eligible_alternate() -> None:
    """Proof 4: quota exhausted → SWITCH to eligible alternate plane."""

    current = _route("quota-dead", provider="provider-a", gateway="gw-a", pool="pool-a")
    alternate = _route(
        "alt-plane", provider="provider-b", gateway="gw-b", pool="pool-b", model="model-b"
    )
    result = decide_session_route(
        SessionState(
            session_id="s4",
            current_route=current,
            last_served_route=current,
            cache=None,
            quota_exhausted=True,
        ),
        alternate,
        CostState(
            stay_expected=_strategy("stay", Decimal("0.01")),
            switch_expected=_strategy("switch", Decimal("0.50")),
            now=NOW,
        ),
        TaskState(required_capability_tier=1),
    )
    assert result.decision == "SWITCH"
    assert result.selected_route_id == "alt-plane"
    assert "quota_exhausted" in result.override_reasons


def test_tiny_alternating_cost_diffs_hysteresis_holds() -> None:
    """Proof 5: tiny alternating cost diffs → hysteresis holds (no thrash)."""

    current = _route("sticky", model="a")
    other = _route("alt", model="b", provider="provider-b")
    # Switch appears slightly cheaper but below hysteresis threshold / signal count.
    result = decide_session_route(
        SessionState(
            session_id="s5",
            current_route=current,
            last_served_route=current,
            cache=None,
            consecutive_switch_signals=0,
            switch_signal_threshold=2,
        ),
        other,
        CostState(
            stay_expected=_strategy("stay", Decimal("0.100")),
            switch_expected=_strategy("switch", Decimal("0.098")),
            switch_handoff_cost=CostTerm(
                kind="route_switch", amount=Decimal("0.000"), unit="usd", status="estimated"
            ),
            hysteresis_margin_usd=Decimal("0.01"),
            now=NOW,
        ),
        TaskState(required_capability_tier=1),
    )
    assert result.decision == "STAY"
    assert "hysteresis_hold" in result.reason or "hysteresis" in result.reason
    assert result.selected_route_id == "sticky"


def test_excluded_candidate_cannot_be_selected() -> None:
    """Proof 6: excluded/hard-ineligible candidate cannot be selected."""

    current = _route("ok", eligible=True)
    excluded = _route(
        "banned",
        eligible=False,
        excluded=True,
        exclusion_reason="hard_gate_fail",
        provider="provider-b",
    )
    result = decide_session_route(
        SessionState(session_id="s6", current_route=current, last_served_route=current),
        excluded,
        CostState(
            stay_expected=_strategy("stay", Decimal("1.00")),
            switch_expected=_strategy("switch", Decimal("0.01"), qualified=False),
            now=NOW,
        ),
        TaskState(required_capability_tier=1),
    )
    assert result.decision == "STAY"
    assert result.selected_route_id == "ok"
    assert "candidate_hard_ineligible" in result.assumptions or any(
        "ineligible" in a or "excluded" in a for a in result.assumptions
    )


def test_receipt_shows_terms_assumptions_freshness_overrides() -> None:
    """Proof 7: receipt shows terms/assumptions/freshness/override reasons."""

    current = _route("cur", tier=1)
    better = _route("next", tier=5, provider="provider-b")
    result = decide_session_route(
        SessionState(
            session_id="s7", current_route=current, last_served_route=current, quota_exhausted=True
        ),
        better,
        CostState(
            stay_expected=_strategy(
                "stay",
                Decimal("0.20"),
                terms=(
                    CostTerm(
                        kind="execution",
                        amount=Decimal("0.20"),
                        unit="usd",
                        status="estimated",
                        observed_at=NOW,
                        fresh_until=NOW + timedelta(minutes=10),
                    ),
                ),
            ),
            switch_expected=_strategy("switch", Decimal("0.25")),
            now=NOW,
        ),
        TaskState(required_capability_tier=4),
    )
    receipt = result.to_dict()
    assert receipt["decision"] in {"STAY", "SWITCH", "BLOCKED"}
    assert "terms" in receipt and isinstance(receipt["terms"], list)
    assert "assumptions" in receipt
    assert "freshness" in receipt
    assert "override_reasons" in receipt
    assert receipt["schema_version"]
    assert receipt["selected_route"]["route_id"] == result.selected_route_id


def test_shadow_and_counterfactual_modes() -> None:
    """Proof 8: shadow/counterfactual mode available."""

    current = _route("cur", tier=1)
    better = _route("next", tier=4, provider="provider-b")
    cost = CostState(
        stay_expected=_strategy("stay", Decimal("0.05")),
        switch_expected=_strategy("switch", Decimal("0.40")),
        now=NOW,
    )
    task = TaskState(required_capability_tier=3)
    session = SessionState(session_id="s8", current_route=current, last_served_route=current)

    authoritative = decide_session_route(session, better, cost, task, mode="authoritative")
    shadow = decide_session_route(session, better, cost, task, mode="shadow")
    counterfactual = decide_session_route(
        session, better, cost, task, mode="counterfactual", counterfactual_force_stay=True
    )

    assert authoritative.decision == "SWITCH"
    assert authoritative.mode == DecisionMode.AUTHORITATIVE
    assert authoritative.authoritative is True

    assert shadow.decision == "SWITCH"
    assert shadow.mode == DecisionMode.SHADOW
    assert shadow.authoritative is False
    assert shadow.would_decide == "SWITCH"

    assert counterfactual.mode == DecisionMode.COUNTERFACTUAL
    assert counterfactual.decision == "STAY"
    assert counterfactual.would_decide == "SWITCH"
    assert counterfactual.authoritative is False
    assert "counterfactual_force_stay" in counterfactual.assumptions


def test_unknown_cache_savings_never_invented() -> None:
    warm = _route("warm")
    cold = _route("cold", provider="provider-b")
    result = decide_session_route(
        SessionState(
            session_id="s9",
            current_route=warm,
            last_served_route=warm,
            cache=_cache(
                savings_usd=None, fresh_until=NOW + timedelta(minutes=5), status="unknown"
            ),
            consecutive_switch_signals=2,
        ),
        cold,
        CostState(
            stay_expected=_strategy("stay", Decimal("0.10")),
            switch_expected=_strategy("switch", Decimal("0.07")),
            stay_cache_savings=CostTerm(
                kind="cache_read",
                amount=None,
                unit="usd",
                status="unknown",
                notes="cache_hit_without_observed_savings",
            ),
            switch_handoff_cost=CostTerm(
                kind="route_switch", amount=Decimal("0.01"), unit="usd", status="estimated"
            ),
            hysteresis_margin_usd=Decimal("0.005"),
            now=NOW,
        ),
        TaskState(required_capability_tier=1),
    )
    assert "cache_savings_unknown_ignored" in result.assumptions
    assert result.stay_ev == Decimal("0.10")


def test_current_ineligible_and_no_alternate_is_blocked() -> None:
    dead = _route("dead", eligible=False, excluded=True, exclusion_reason="banned")
    also_dead = _route(
        "also-dead", eligible=False, excluded=True, exclusion_reason="banned", provider="provider-b"
    )
    result = decide_session_route(
        SessionState(session_id="s10", current_route=dead, last_served_route=dead),
        also_dead,
        CostState(
            stay_expected=_strategy("stay", Decimal("0.10"), qualified=False),
            switch_expected=_strategy("switch", Decimal("0.10"), qualified=False),
            now=NOW,
        ),
        TaskState(required_capability_tier=1),
    )
    assert result.decision == "BLOCKED"
    assert result.selected_route_id is None
