"""candidate-specific pre-hydration ContextPlan proofs."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest
from test_live_candidate_shortlist import FAST, STRONG, _context, _metadata, _request, _service

from verdict.context_pack import ContextPlan, estimate_tokens
from verdict.cost_ledger import CostTerm
from verdict.effective_capability import AssistanceCost
from verdict.expected_cost import ExpectedStrategyCost
from verdict.free_tier_admit import build_cheap_path_context_pack
from verdict.intelligence import _cost_with_context_plan


def _plan(candidate: str, window: int, *, family: str = "coding") -> ContextPlan:
    return ContextPlan.estimate_for_candidate(
        task="fix the parser and prove the regression",
        candidate_id=candidate,
        context_window=window,
        task_family=family,
        required_capabilities=("tools",),
        criteria_count=2,
        created_at="2026-09-21T20:00:00Z",
    )


def test_final_pack_respects_selected_budget(tmp_path) -> None:
    plan = _plan(STRONG, 4096)
    pack = build_cheap_path_context_pack(
        "fix the parser and prove the regression",
        candidate_id=STRONG,
        context_plan=plan,
        workspace_root=tmp_path,
        workspace_roots=(),
    )
    assert pack.budget_receipt is not None
    assert pack.budget_receipt.used_tokens <= plan.input_token_budget
    assert pack.plan_digest == plan.digest


def test_large_window_does_not_inflate_small_task() -> None:
    small_window = _plan("route/small", 32_000)
    huge_window = _plan("route/huge", 1_000_000)
    assert small_window.token_budget == huge_window.token_budget
    assert huge_window.token_budget < huge_window.candidate_context_window


def test_pre_hydration_plan_influences_selection() -> None:
    service = _service([], top_k=2)
    metadata = _metadata()
    records = tuple(
        replace(row, caps=replace(row.caps, context=replace(row.caps.context, value=2_000)))
        if row.id == FAST
        else row
        for row in metadata.records
    )
    service.metadata_snapshot = replace(metadata, records=records)
    request = _request(STRONG, FAST)
    prepared = service._prepare_execution_path_request(
        "fix code", "low", _context(request), request
    )
    assert FAST in prepared.hard_excluded_ids
    assert [offer.candidate_id for offer in prepared.offers] == [STRONG]
    selected_offer = prepared.offers[0]
    plan = selected_offer.assistance_plan.context_plan_requirements
    assert plan["candidate_id"] == STRONG
    assert plan["estimated_fit"] is True
    assert (
        selected_offer.assistance_plan.assistance_cost.context_tokens
        == plan["estimated_input_tokens"]
    )
    hydration = next(
        term
        for term in selected_offer.expected_cost.terms
        if term.kind == "hydration" and term.unit == "tokens"
    )
    assert hydration.amount == plan["estimated_input_tokens"]
    assert selected_offer.assistance_plan.evidence_digest != f"sha256:{STRONG}"
    assert selected_offer.assistance_plan.plan_id.startswith("ecp:")


def test_compile_uses_selected_candidate_plan(tmp_path) -> None:
    selected = _plan("route/selected", 8192)
    other = _plan("route/other", 4096)
    pack = build_cheap_path_context_pack(
        "fix the parser and prove the regression",
        candidate_id="route/selected",
        context_plan=selected,
        workspace_root=tmp_path,
        workspace_roots=(),
    )
    assert pack.plan_digest == selected.digest
    assert pack.plan_digest != other.digest
    with pytest.raises(ValueError, match="candidate_id must match"):
        build_cheap_path_context_pack(
            "fix the parser",
            candidate_id="route/selected",
            context_plan=other,
            workspace_root=tmp_path,
            workspace_roots=(),
        )


def test_small_vs_cross_module_pack_need() -> None:
    small = ContextPlan.estimate_for_candidate(
        task="rename one local variable",
        candidate_id="route/a",
        context_window=128_000,
        task_family="general",
        created_at="2026-09-21T20:00:00Z",
    )
    cross_module = ContextPlan.estimate_for_candidate(
        task="change routing across modules and prove integration behavior",
        candidate_id="route/a",
        context_window=128_000,
        task_family="agentic",
        required_capabilities=("tools", "structured_output"),
        criteria_count=5,
        created_at="2026-09-21T20:00:00Z",
    )
    assert small.estimated_input_tokens is not None
    assert cross_module.estimated_input_tokens is not None
    assert small.estimated_input_tokens >= estimate_tokens("rename one local variable")
    assert cross_module.estimated_input_tokens > small.estimated_input_tokens
    assert cross_module.token_budget <= 16_384 + 1024 + 512


def test_context_reestimate_preserves_unknown_cash_terms() -> None:
    original = ExpectedStrategyCost.build(
        strategy_id="candidate",
        trajectory_id="trajectory",
        terms=(
            CostTerm(kind="hydration", amount=Decimal("100"), unit="tokens", status="estimated"),
            CostTerm(kind="hydration", amount=None, unit="usd", status="unknown"),
            CostTerm(kind="execution", amount=Decimal("10"), unit="tokens", status="estimated"),
            CostTerm(kind="execution", amount=None, unit="usd", status="unknown"),
        ),
    )
    plan = replace(_plan("candidate", 4096), estimated_input_tokens=200, output_token_reserve=20)

    revised = _cost_with_context_plan(
        original, new_assistance=AssistanceCost(context_tokens=200), plan=plan
    )

    assert revised.has_unknown_cash is True
    assert {(term.kind, term.unit) for term in revised.terms if term.status == "unknown"} == {
        ("hydration", "usd"),
        ("execution", "usd"),
    }
    token_amounts = {term.kind: term.amount for term in revised.terms if term.unit == "tokens"}
    assert token_amounts["hydration"] == Decimal("200")
    assert token_amounts["execution"] == Decimal("20")
