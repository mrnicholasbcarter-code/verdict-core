"""Proof tests for expected complete-strategy cost."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from verdict.cost_ledger import CostTerm, PriceEvidenceInput, QuotaEvidenceInput
from verdict.effective_capability import AssistanceCost
from verdict.expected_cost import ExpectedStrategyCost, build_strategy_from_assistance

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
