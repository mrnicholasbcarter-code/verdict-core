"""Proof tests for cost ledger reservations and reconciliation."""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from verdict.cost_ledger import (
    CacheEvidenceInput,
    CostLedger,
    CostLedgerError,
    CostTerm,
    PriceEvidenceInput,
    QuotaEvidenceInput,
    quota_pressure_term,
    subscription_opportunity_term,
    tokens_to_usd_term,
)
from verdict.effective_capability import AssistanceCost

NOW = datetime(2026, 9, 19, 12, 0, tzinfo=timezone.utc)


def test_integer_decimal_arithmetic_and_unknown_price() -> None:
    term = CostTerm(kind="execution", amount=Decimal("0.001250"), unit="usd", status="estimated")
    assert term.amount == Decimal("0.001250")
    unknown = tokens_to_usd_term(1000, kind="execution", price=None, now=NOW)
    assert unknown.status == "unknown"
    assert unknown.amount is None


def test_reservation_race_is_atomic() -> None:
    ledger = CostLedger(trajectory_id="traj-race", cash_budget_usd=Decimal("1.00"))
    results: list[str] = []
    barrier = threading.Barrier(8)

    def attempt() -> None:
        barrier.wait()
        try:
            ledger.reserve(Decimal("0.40"), pool="cash", now=NOW)
            results.append("ok")
        except CostLedgerError:
            results.append("fail")

    threads = [threading.Thread(target=attempt) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert results.count("ok") == 2
    assert results.count("fail") == 6
    assert ledger.remaining_cash_usd() == Decimal("0.20")


def test_quota_vs_cash_are_distinct_pools() -> None:
    ledger = CostLedger(
        trajectory_id="traj-pools",
        cash_budget_usd=Decimal("5.00"),
        quota_budgets={"openrouter": Decimal("100")},
        subscription_budgets={"codex-plus": Decimal("50")},
    )
    cash = ledger.reserve(Decimal("1.00"), pool="cash", now=NOW)
    quota = ledger.reserve(
        Decimal("10"), pool="quota", unit="quota_units", pool_id="openrouter", now=NOW
    )
    sub = ledger.reserve(
        Decimal("5"), pool="subscription", unit="subscription_units", pool_id="codex-plus", now=NOW
    )
    assert cash.pool == "cash"
    assert quota.pool == "quota"
    assert sub.pool == "subscription"
    assert ledger.remaining_cash_usd() == Decimal("4.00")
    # Quota spend must not reduce cash headroom.
    assert ledger.remaining_cash_usd() == Decimal("4.00")


def test_subscription_pressure_distinct_from_cash() -> None:
    pressure = quota_pressure_term(
        QuotaEvidenceInput(pool_id="chatgpt-plus", remaining_pct=20.0, evidence_id="q-1")
    )
    opp = subscription_opportunity_term(units=Decimal("3"), status="estimated", evidence_id="s-1")
    assert pressure.unit == "dimensionless"
    assert pressure.amount == Decimal("0.800000")
    assert opp.unit == "subscription_units"
    assert opp.kind == "subscription_opportunity"
    unknown = quota_pressure_term(None)
    assert unknown.status == "unknown"


def test_cached_tokens_never_invent_savings() -> None:
    ledger = CostLedger(trajectory_id="traj-cache", cash_budget_usd=Decimal("2.00"))
    reservation = ledger.reserve(Decimal("0.50"), pool="cash", now=NOW)
    updated = ledger.reconcile(
        reservation.reservation_id,
        actual_amount=Decimal("0.40"),
        cache_evidence=CacheEvidenceInput(cache_hit=True, cached_input_tokens=800),
    )
    statuses = {term.status for term in updated.terms}
    units = {(term.unit, term.status) for term in updated.terms}
    assert ("tokens", "observed") in units
    assert ("usd", "unknown") in units
    assert "unknown" in statuses
    # Observed savings only when explicitly supplied.
    with_savings = ledger.reserve(Decimal("0.20"), pool="cash", now=NOW)
    priced = ledger.reconcile(
        with_savings.reservation_id,
        actual_amount=Decimal("0.10"),
        cache_evidence=CacheEvidenceInput(
            cache_hit=True, cached_input_tokens=800, savings_usd="0.05", evidence_id="c-1"
        ),
    )
    assert any(
        term.unit == "usd" and term.status == "observed" and term.amount == Decimal("0.05")
        for term in priced.terms
    )


def test_stale_prices_are_assumed_conservative() -> None:
    stale: PriceEvidenceInput = {
        "input_usd_per_mtok": "1.00",
        "stale": True,
        "evidence_id": "price-stale",
    }
    term = tokens_to_usd_term(1_000_000, kind="execution", price=stale, now=NOW)
    assert term.status == "assumed"
    assert term.amount == Decimal("1.00")
    assert term.notes == "stale_price_conservative"

    expired: PriceEvidenceInput = {
        "input_usd_per_mtok": "2.00",
        "fresh_until": (NOW - timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
    }
    expired_term = tokens_to_usd_term(500_000, kind="execution", price=expired, now=NOW)
    assert expired_term.status == "assumed"


def test_partial_and_missing_billing_reconcile() -> None:
    ledger = CostLedger(trajectory_id="traj-billing", cash_budget_usd=Decimal("10.00"))
    reservation = ledger.reserve(Decimal("3.00"), pool="cash", now=NOW)
    missing = ledger.reconcile(reservation.reservation_id, missing=True)
    assert missing.status == "reserved"
    assert "missing" in (missing.reconcile_notes or "")
    assert ledger.remaining_cash_usd() == Decimal("7.00")

    delayed = ledger.reconcile(reservation.reservation_id, delayed=True)
    assert delayed.status == "reserved"

    partial = ledger.reconcile(
        reservation.reservation_id, actual_amount=Decimal("1.25"), partial=True
    )
    assert partial.status == "partial"
    assert partial.actual_amount == Decimal("1.25")
    assert ledger.remaining_cash_usd() == Decimal("8.75")


def test_protected_cheaper_inference_budget() -> None:
    ledger = CostLedger(
        trajectory_id="traj-ci",
        cheaper_inference_budget_usd=Decimal("1.00"),
        cheaper_inference_protected_floor_usd=Decimal("0.25"),
    )
    ledger.reserve(Decimal("0.75"), pool="cheaper_inference", now=NOW)
    with pytest.raises(CostLedgerError, match="CheaperInference"):
        ledger.reserve(Decimal("0.10"), pool="cheaper_inference", now=NOW)
    # Protected floor usable only when explicitly allowed (calibration/eval).
    protected = ledger.reserve(
        Decimal("0.20"), pool="cheaper_inference", allow_protected_cheaper_inference=True, now=NOW
    )
    assert protected.pool == "cheaper_inference"
    assert ledger.remaining_cheaper_inference_usd() == Decimal("0.05")


def test_assistance_cost_attribution_same_trajectory() -> None:
    ledger = CostLedger(trajectory_id="traj-assist", cash_budget_usd=Decimal("5.00"))
    assistance = AssistanceCost(
        context_tokens=100, tool_tokens=50, planning_tokens=25, verification_tokens=10
    )
    price: PriceEvidenceInput = {"input_usd_per_mtok": "1.00", "evidence_id": "p-1"}
    terms = ledger.attribute_assistance(assistance, price=price, now=NOW)
    kinds = {term.kind for term in terms if term.unit == "tokens"}
    assert kinds == {"planning", "hydration", "tools", "verification"}
    assert all(
        term.notes == "assistance_cost_attribution" for term in terms if term.unit == "tokens"
    )
    assert ledger.assistance_terms() == terms
    receipt = ledger.to_dict()
    assert receipt["trajectory_id"] == "traj-assist"
    assert len(receipt["assistance_terms"]) == len(terms)
