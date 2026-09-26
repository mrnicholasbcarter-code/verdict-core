"""Proof tests for expected complete-strategy cost."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from verdict.cost_ledger import CostTerm, PriceEvidenceInput, QuotaEvidenceInput
from verdict.effective_capability import AssistanceCost
from verdict.expected_cost import (
    ExpectedStrategyCost,
    build_strategy_from_assistance,
    compare_strategies,
    select_strategy,
)

NOW = datetime(2026, 9, 19, 12, 0, tzinfo=timezone.utc)
PRICE: PriceEvidenceInput = {"input_usd_per_mtok": "1.00", "evidence_id": "price-1"}


def _assistance(**overrides: int) -> AssistanceCost:
    base = {
        "context_tokens": 100,
        "tool_tokens": 20,
        "planning_tokens": 30,
        "verification_tokens": 10,
    }
    base.update(overrides)
    return AssistanceCost(**base)


def test_strategy_receipt_shows_observed_estimated_unknown_assumed() -> None:
    terms = (
        CostTerm(kind="execution", amount=Decimal("0.01"), unit="usd", status="observed"),
        CostTerm(kind="retry", amount=Decimal("0.02"), unit="usd", status="estimated"),
        CostTerm(kind="cache_read", amount=None, unit="usd", status="unknown"),
        CostTerm(
            kind="execution",
            amount=Decimal("0.03"),
            unit="usd",
            status="assumed",
            notes="stale_price_conservative",
        ),
    )
    strategy = ExpectedStrategyCost.build(
        strategy_id="s-receipt", trajectory_id="traj-1", terms=terms
    )
    receipt = strategy.to_dict()
    assert receipt["status_counts"] == {"observed": 1, "estimated": 1, "unknown": 1, "assumed": 1}
    assert strategy.cash_usd is None  # unknown cash term blocks known total
    assert {term["status"] for term in receipt["terms"]} == {
        "observed",
        "estimated",
        "unknown",
        "assumed",
    }


def test_free_first_preference_vs_expected_cost_optimality() -> None:
    free = build_strategy_from_assistance(
        strategy_id="free-retry-heavy",
        trajectory_id="traj-ff",
        assistance=_assistance(),
        execution_tokens=100_000,
        retry_tokens=900_000,
        escalation_tokens=200_000,
        price=PRICE,
        is_free=True,
        now=NOW,
    )
    paid = build_strategy_from_assistance(
        strategy_id="paid-stable",
        trajectory_id="traj-ff",
        assistance=_assistance(),
        execution_tokens=200_000,
        retry_tokens=0,
        escalation_tokens=0,
        price=PRICE,
        is_free=False,
        now=NOW,
    )
    assert free.cash_usd is not None and paid.cash_usd is not None
    assert free.cash_usd > paid.cash_usd

    preferred = compare_strategies([free, paid], mode="cheapest_qualified", free_first=True)
    assert preferred.selected_strategy_id == "free-retry-heavy"
    assert preferred.optimality_claimed is False

    optimal = compare_strategies([free, paid], mode="expected_cost", free_first=True)
    assert optimal.selected_strategy_id == "paid-stable"
    assert optimal.optimality_claimed is True
    assert "expected_cost_beats_free_first" in optimal.reason


def test_cheapest_qualified_vs_expected_cost_modes() -> None:
    cheap_call = ExpectedStrategyCost.build(
        strategy_id="cheap-call",
        trajectory_id="traj-mode",
        terms=(
            CostTerm(kind="execution", amount=Decimal("0.01"), unit="usd", status="estimated"),
            CostTerm(kind="retry", amount=Decimal("0.20"), unit="usd", status="estimated"),
        ),
        policy_mode="cheapest_qualified",
    )
    complete = ExpectedStrategyCost.build(
        strategy_id="complete-cheaper",
        trajectory_id="traj-mode",
        terms=(
            CostTerm(kind="execution", amount=Decimal("0.05"), unit="usd", status="estimated"),
            CostTerm(kind="retry", amount=Decimal("0.01"), unit="usd", status="estimated"),
        ),
        policy_mode="expected_cost",
    )
    # Both modes sum complete terms here; ranking uses full cash_usd.
    cq = select_strategy([cheap_call, complete], mode="cheapest_qualified")
    ec = select_strategy([cheap_call, complete], mode="expected_cost")
    assert cq.selected_strategy_id == "complete-cheaper"
    assert ec.selected_strategy_id == "complete-cheaper"
    assert cq.mode == "cheapest_qualified"
    assert ec.mode == "expected_cost"


def test_subscription_and_quota_dimensions_on_strategy() -> None:
    strategy = build_strategy_from_assistance(
        strategy_id="sub-quota",
        trajectory_id="traj-sq",
        assistance=_assistance(),
        execution_tokens=10_000,
        price=PRICE,
        quota=QuotaEvidenceInput(pool_id="grok-build", remaining_pct=40.0),
        subscription_units=Decimal("2.5"),
        now=NOW,
    )
    assert strategy.subscription_opportunity == Decimal("2.5")
    assert strategy.quota_pressure == Decimal("0.600000")
    receipt = strategy.to_dict()
    assert receipt["subscription_opportunity"] == "2.5"
    assert receipt["quota_pressure"] == "0.600000"


def test_assistance_and_overlays_share_trajectory() -> None:
    strategy = build_strategy_from_assistance(
        strategy_id="full-path",
        trajectory_id="traj-shared",
        assistance=_assistance(
            context_tokens=50, tool_tokens=25, planning_tokens=15, verification_tokens=5
        ),
        execution_tokens=1000,
        retry_tokens=100,
        escalation_tokens=50,
        route_switch_tokens=20,
        price=PRICE,
        now=NOW,
    )
    kinds = {term.kind for term in strategy.terms if term.unit == "tokens"}
    assert kinds >= {
        "planning",
        "hydration",
        "tools",
        "verification",
        "execution",
        "retry",
        "escalation",
        "route_switch",
    }
    assert strategy.trajectory_id == "traj-shared"
    assert strategy.cash_usd is not None


def test_equal_cash_uses_known_token_burden_as_tie_breaker() -> None:
    light = ExpectedStrategyCost.build(
        strategy_id="light",
        trajectory_id="traj-resource",
        terms=(
            CostTerm(kind="execution", amount=Decimal("0"), unit="usd", status="estimated"),
            CostTerm(kind="hydration", amount=Decimal("100"), unit="tokens", status="estimated"),
        ),
    )
    heavy = ExpectedStrategyCost.build(
        strategy_id="heavy",
        trajectory_id="traj-resource",
        terms=(
            CostTerm(kind="execution", amount=Decimal("0"), unit="usd", status="estimated"),
            CostTerm(kind="hydration", amount=Decimal("200"), unit="tokens", status="estimated"),
        ),
    )

    selected = compare_strategies((heavy, light))

    assert selected.selected_strategy_id == "light"
    assert light.resource_token_burden == Decimal("100")


def test_known_cash_precedes_token_burden() -> None:
    cheap_heavy = ExpectedStrategyCost.build(
        strategy_id="cheap-heavy",
        trajectory_id="traj-resource",
        terms=(
            CostTerm(kind="execution", amount=Decimal("0.01"), unit="usd", status="estimated"),
            CostTerm(kind="execution", amount=Decimal("1000"), unit="tokens", status="estimated"),
        ),
    )
    costly_light = ExpectedStrategyCost.build(
        strategy_id="costly-light",
        trajectory_id="traj-resource",
        terms=(
            CostTerm(kind="execution", amount=Decimal("0.02"), unit="usd", status="estimated"),
            CostTerm(kind="execution", amount=Decimal("1"), unit="tokens", status="estimated"),
        ),
    )

    assert compare_strategies((costly_light, cheap_heavy)).selected_strategy_id == "cheap-heavy"


def test_subscription_and_quota_precede_token_tie_breaker() -> None:
    economical_heavy = ExpectedStrategyCost.build(
        strategy_id="economical-heavy",
        trajectory_id="traj-dimensions",
        terms=(
            CostTerm(kind="execution", amount=Decimal("0"), unit="usd", status="estimated"),
            CostTerm(
                kind="subscription",
                amount=Decimal("1"),
                unit="subscription_units",
                status="estimated",
            ),
            CostTerm(
                kind="quota_pressure", amount=Decimal("0.1"), unit="quota_units", status="estimated"
            ),
            CostTerm(kind="hydration", amount=Decimal("1000"), unit="tokens", status="estimated"),
        ),
    )
    costly_light = ExpectedStrategyCost.build(
        strategy_id="costly-light",
        trajectory_id="traj-dimensions",
        terms=(
            CostTerm(kind="execution", amount=Decimal("0"), unit="usd", status="estimated"),
            CostTerm(
                kind="subscription",
                amount=Decimal("2"),
                unit="subscription_units",
                status="estimated",
            ),
            CostTerm(
                kind="quota_pressure", amount=Decimal("0.2"), unit="quota_units", status="estimated"
            ),
            CostTerm(kind="hydration", amount=Decimal("1"), unit="tokens", status="estimated"),
        ),
    )

    selected = compare_strategies((costly_light, economical_heavy))

    assert selected.selected_strategy_id == "economical-heavy"
