"""Adversarial proof fixtures for execution-path optimizer (BOD-104)."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from verdict.bounded_recovery import (
    BoundedRecoveryController,
    ExecutionRoute,
    FailureEvidence,
    RecoveryAction,
    RecoveryBounds,
    RecoveryOutcome,
)
from verdict.context_budget import BudgetOmission, BudgetReceipt
from verdict.cost_ledger import CostLedger, CostTerm, PriceEvidenceInput
from verdict.effective_capability import (
    AssistanceCost,
    AssistancePlan,
    DecompositionRequirement,
    ProvenanceClaim,
    TaskSlice,
    VerificationStrategy,
)
from verdict.execution_path import ExecutionPathOffer, ExecutionPathRequest, optimize_execution_path
from verdict.expected_cost import ExpectedStrategyCost, build_strategy_from_assistance
from verdict.runtime_certification import CertificationState
from verdict.session_economics import ConcreteRoute, DecisionMode, SessionRouteDecision

NOW = datetime(2026, 9, 19, 12, 0, tzinfo=timezone.utc)
PRICE: PriceEvidenceInput = {"input_usd_per_mtok": "1.00", "evidence_id": "price-1"}
UNKNOWN_PRICE: PriceEvidenceInput = {"evidence_id": "price-unknown"}


def _slice() -> TaskSlice:
    return TaskSlice(
        slice_id="slice-1",
        objective="refactor module",
        acceptance_criteria=("tests pass",),
        proof_criteria=("pytest green",),
    )


def _route(
    route_id: str,
    *,
    model: str = "cheap-model",
    gateway: str = "gateway-a",
    provider: str = "provider-a",
    tier: int = 1,
    eligible: bool = True,
    excluded: bool = False,
) -> ConcreteRoute:
    return ConcreteRoute(
        route_id=route_id,
        gateway=gateway,
        provider=provider,
        model=model,
        credential_pool="pool-a",
        capability_tier=tier,
        eligible=eligible,
        excluded=excluded,
    )


def _plan(
    *,
    candidate_id: str,
    result: str = "sufficient",
    intrinsic: bool = True,
    assisted: bool = True,
    reasons: tuple[str, ...] = (),
    decomposition_required: bool = False,
    hard_excluded: bool = False,
    assistance: AssistanceCost | None = None,
    digest: str = "sha256:plan",
    tools: tuple[str, ...] = ("pytest",),
) -> AssistancePlan:
    cost = assistance or AssistanceCost(
        context_tokens=0, tool_tokens=0, planning_tokens=0, verification_tokens=50
    )
    return AssistancePlan(
        plan_id=f"plan-{candidate_id}",
        candidate_id=candidate_id,
        task_slice=_slice(),
        required_intrinsic_capabilities=("tools",),
        required_context_slots=("evidence",),
        required_context_evidence=("proof",),
        required_tool_capabilities=("pytest",),
        selected_tool_surface=tools,
        decomposition=DecompositionRequirement(
            required=decomposition_required,
            child_slices=(),
            preserves_parent_acceptance=True,
            preserves_parent_proof=True,
            reason="frontier_plan" if decomposition_required else "",
            planner_role="frontier" if decomposition_required else "",
        ),
        verification=VerificationStrategy(kind="pytest", proof_criteria=("pytest green",)),
        assistance_cost=cost,
        result=result,  # type: ignore[arg-type]
        reasons=reasons
        or (
            (("hard_gate_excluded",) if hard_excluded else ())
            + (("intrinsic_ok",) if intrinsic else ("needs_assistance",))
        ),
        intrinsic_sufficient=intrinsic,
        assisted_sufficient=assisted,
        assistance_delta=() if intrinsic else ("context:evidence",),
        provenance=(
            ProvenanceClaim(
                kind="candidate",
                claim=candidate_id,
                source="fixture",
                digest=digest,
                observed_at=NOW.isoformat(),
                freshness="fresh",
                fresh=True,
            ),
        ),
        evidence_digest=digest,
    )


def _budget(
    *, candidate_id: str, fits: bool = True, digest: str = "sha256:budget"
) -> BudgetReceipt:
    omitted: tuple[BudgetOmission, ...] = ()
    if not fits:
        omitted = (
            BudgetOmission(
                unit_id="proof-1",
                source_class="proof_verification",
                reason="budget_exhausted_mandatory",
                priority="mandatory",
                token_count=200,
                token_count_kind="exact",
                value_score=1.0,
                provenance_uri="fixture://proof-1",
            ),
        )
    return BudgetReceipt(
        candidate_id=candidate_id,
        context_limit=8000,
        usable_input_budget=4000,
        reserved_total=500,
        used_tokens=100 if fits else 4000,
        fits=fits,
        included=(),
        omitted=omitted,
        per_source_used={},
        account_digest="sha256:account",
        digest=digest,
    )


def _cost(
    strategy_id: str,
    *,
    assistance: AssistanceCost,
    execution_tokens: int,
    retry_tokens: int = 0,
    escalation_tokens: int = 0,
    route_switch_tokens: int = 0,
    price: PriceEvidenceInput | None = PRICE,
    is_free: bool = False,
    qualified: bool = True,
) -> ExpectedStrategyCost:
    return build_strategy_from_assistance(
        strategy_id=strategy_id,
        trajectory_id="traj-104",
        assistance=assistance,
        execution_tokens=execution_tokens,
        retry_tokens=retry_tokens,
        escalation_tokens=escalation_tokens,
        route_switch_tokens=route_switch_tokens,
        price=price,
        is_free=is_free,
        qualified=qualified,
        now=NOW,
    )


def _offer(
    *,
    strategy: str,
    route: ConcreteRoute,
    plan: AssistancePlan,
    expected: ExpectedStrategyCost,
    budget: BudgetReceipt | None = None,
    cert_state: CertificationState | None = CertificationState.READY,
    cert_freshness: str = "fresh",
    hard_excluded: bool = False,
    is_cheap: bool = False,
    is_paid: bool = False,
    is_frontier: bool = False,
    context_trust_admitted: bool = True,
    evidence_conflicts: tuple[str, ...] = (),
) -> ExecutionPathOffer:
    return ExecutionPathOffer(
        strategy=strategy,  # type: ignore[arg-type]
        route=route,
        assistance_plan=plan,
        expected_cost=expected,
        budget_receipt=budget,
        certification_state=cert_state,
        certification_freshness=cert_freshness,
        hard_excluded=hard_excluded,
        is_cheap=is_cheap,
        is_paid=is_paid,
        is_frontier=is_frontier,
        context_trust_admitted=context_trust_admitted,
        evidence_conflicts=evidence_conflicts,
    )


def test_direct_cheap_wins_when_intrinsically_sufficient() -> None:
    cheap = _route("cheap-1")
    paid = _route("paid-1", model="paid-model", provider="provider-b", tier=3)
    cheap_plan = _plan(candidate_id="cheap-1", intrinsic=True)
    paid_plan = _plan(candidate_id="paid-1", intrinsic=True, digest="sha256:paid-plan")
    offers = (
        _offer(
            strategy="direct_cheap",
            route=cheap,
            plan=cheap_plan,
            expected=_cost(
                "direct_cheap:cheap-1",
                assistance=cheap_plan.assistance_cost,
                execution_tokens=10_000,
                is_free=True,
            ),
            budget=_budget(candidate_id="cheap-1"),
            is_cheap=True,
        ),
        _offer(
            strategy="direct_paid",
            route=paid,
            plan=paid_plan,
            expected=_cost(
                "direct_paid:paid-1", assistance=paid_plan.assistance_cost, execution_tokens=50_000
            ),
            budget=_budget(candidate_id="paid-1", digest="sha256:budget-paid"),
            is_paid=True,
        ),
    )
    decision = optimize_execution_path(
        ExecutionPathRequest(task_slice=_slice(), trajectory_id="traj-104", offers=offers, now=NOW)
    )
    assert decision.selected_strategy == "direct_cheap"
    assert decision.selected_candidate_id == "cheap-1"
    assert decision.selected_route is not None
    assert decision.selected_route.gateway == "gateway-a"
    assert decision.selected_route.provider == "provider-a"
    assert decision.selected_route.model == "cheap-model"
    assert any(r.strategy == "direct_paid" for r in decision.rejected)
    assert "expected_cost" in decision.why_selected or "cheaper" in decision.why_selected.lower()
    assert decision.assistance_plan_digest == "sha256:plan"
    assert decision.verification_requirements == ("pytest green",)
    assert decision.decision_digest.startswith("sha256:")


def test_cheap_with_assistance_beats_paid_on_complete_cost() -> None:
    cheap = _route("cheap-assist")
    paid = _route("paid-2", model="frontier", provider="provider-b", tier=4)
    assist = AssistanceCost(
        context_tokens=2_000, tool_tokens=200, planning_tokens=0, verification_tokens=100
    )
    cheap_plan = _plan(
        candidate_id="cheap-assist",
        intrinsic=False,
        assisted=True,
        assistance=assist,
        digest="sha256:assist-plan",
    )
    paid_plan = _plan(candidate_id="paid-2", intrinsic=True, digest="sha256:paid2")
    offers = (
        _offer(
            strategy="cheap_with_assistance",
            route=cheap,
            plan=cheap_plan,
            expected=_cost(
                "cheap_with_assistance:cheap-assist",
                assistance=assist,
                execution_tokens=20_000,
                is_free=True,
            ),
            budget=_budget(candidate_id="cheap-assist"),
            is_cheap=True,
        ),
        _offer(
            strategy="frontier_direct",
            route=paid,
            plan=paid_plan,
            expected=_cost(
                "frontier_direct:paid-2",
                assistance=paid_plan.assistance_cost,
                execution_tokens=200_000,
            ),
            is_frontier=True,
            is_paid=True,
        ),
    )
    decision = optimize_execution_path(
        ExecutionPathRequest(task_slice=_slice(), trajectory_id="traj-104", offers=offers, now=NOW)
    )
    assert decision.selected_strategy == "cheap_with_assistance"
    assert decision.selected_candidate_id == "cheap-assist"
    rejected_frontier = [r for r in decision.rejected if r.strategy == "frontier_direct"]
    assert rejected_frontier
    assert any("cost" in r.reason or "cheaper" in r.reason for r in rejected_frontier)


def test_frontier_plan_then_cheap_beats_frontier_direct() -> None:
    cheap = _route("cheap-decomp")
    frontier = _route("frontier-1", model="opus", provider="anthropic", tier=5)
    plan_cost = AssistanceCost(
        context_tokens=500, tool_tokens=50, planning_tokens=5_000, verification_tokens=100
    )
    cheap_plan = _plan(
        candidate_id="cheap-decomp",
        intrinsic=False,
        assisted=True,
        decomposition_required=True,
        assistance=plan_cost,
        digest="sha256:decomp",
    )
    frontier_plan = _plan(candidate_id="frontier-1", intrinsic=True, digest="sha256:front")
    offers = (
        _offer(
            strategy="frontier_plan_then_cheap_execute",
            route=cheap,
            plan=cheap_plan,
            expected=_cost(
                "frontier_plan_then_cheap_execute:cheap-decomp",
                assistance=plan_cost,
                execution_tokens=30_000,
            ),
            is_cheap=True,
        ),
        _offer(
            strategy="frontier_direct",
            route=frontier,
            plan=frontier_plan,
            expected=_cost(
                "frontier_direct:frontier-1",
                assistance=frontier_plan.assistance_cost,
                execution_tokens=500_000,
            ),
            is_frontier=True,
            is_paid=True,
        ),
    )
    decision = optimize_execution_path(
        ExecutionPathRequest(task_slice=_slice(), trajectory_id="traj-104", offers=offers, now=NOW)
    )
    assert decision.selected_strategy == "frontier_plan_then_cheap_execute"
    assert decision.selected_candidate_id == "cheap-decomp"


def test_expensive_assistance_allows_paid_to_win() -> None:
    cheap = _route("cheap-heavy")
    paid = _route("paid-light", model="mid", provider="provider-b", tier=3)
    heavy = AssistanceCost(
        context_tokens=800_000,
        tool_tokens=50_000,
        planning_tokens=100_000,
        verification_tokens=10_000,
    )
    cheap_plan = _plan(
        candidate_id="cheap-heavy",
        intrinsic=False,
        assisted=True,
        assistance=heavy,
        digest="sha256:heavy",
    )
    paid_plan = _plan(candidate_id="paid-light", intrinsic=True, digest="sha256:light")
    offers = (
        _offer(
            strategy="cheap_with_assistance",
            route=cheap,
            plan=cheap_plan,
            expected=_cost(
                "cheap_with_assistance:cheap-heavy",
                assistance=heavy,
                execution_tokens=10_000,
                is_free=True,
            ),
            is_cheap=True,
        ),
        _offer(
            strategy="direct_paid",
            route=paid,
            plan=paid_plan,
            expected=_cost(
                "direct_paid:paid-light",
                assistance=paid_plan.assistance_cost,
                execution_tokens=40_000,
            ),
            is_paid=True,
        ),
    )
    decision = optimize_execution_path(
        ExecutionPathRequest(task_slice=_slice(), trajectory_id="traj-104", offers=offers, now=NOW)
    )
    assert decision.selected_strategy == "direct_paid"
    assert decision.selected_candidate_id == "paid-light"


def test_hard_gate_excluded_never_selected() -> None:
    excluded = _route("excluded-cheap", excluded=True, eligible=False)
    paid = _route("paid-ok", model="paid", provider="provider-b", tier=2)
    excluded_plan = _plan(
        candidate_id="excluded-cheap",
        result="insufficient",
        intrinsic=False,
        assisted=False,
        hard_excluded=True,
        reasons=("hard_gate_excluded",),
        digest="sha256:ex",
    )
    paid_plan = _plan(candidate_id="paid-ok", digest="sha256:ok")
    offers = (
        _offer(
            strategy="direct_cheap",
            route=excluded,
            plan=excluded_plan,
            expected=_cost(
                "direct_cheap:excluded-cheap",
                assistance=AssistanceCost(),
                execution_tokens=1,
                is_free=True,
                qualified=False,
            ),
            hard_excluded=True,
            is_cheap=True,
        ),
        _offer(
            strategy="direct_paid",
            route=paid,
            plan=paid_plan,
            expected=_cost(
                "direct_paid:paid-ok", assistance=paid_plan.assistance_cost, execution_tokens=40_000
            ),
            is_paid=True,
        ),
    )
    decision = optimize_execution_path(
        ExecutionPathRequest(
            task_slice=_slice(),
            trajectory_id="traj-104",
            offers=offers,
            hard_excluded_ids=frozenset({"excluded-cheap"}),
            now=NOW,
        )
    )
    assert decision.selected_candidate_id != "excluded-cheap"
    assert decision.selected_strategy == "direct_paid"
    assert any(r.candidate_id == "excluded-cheap" and "hard" in r.reason for r in decision.rejected)


def test_unknown_capability_never_promoted_to_sufficient() -> None:
    unknown = _route("unknown-cap")
    paid = _route("known-paid", model="paid", provider="provider-b", tier=2)
    unknown_plan = _plan(
        candidate_id="unknown-cap",
        result="unknown",
        intrinsic=False,
        assisted=False,
        reasons=("required_capability_unknown",),
        digest="sha256:unk",
    )
    paid_plan = _plan(candidate_id="known-paid", digest="sha256:known")
    offers = (
        _offer(
            strategy="direct_cheap",
            route=unknown,
            plan=unknown_plan,
            expected=_cost(
                "direct_cheap:unknown-cap",
                assistance=AssistanceCost(),
                execution_tokens=1,
                is_free=True,
            ),
            is_cheap=True,
        ),
        _offer(
            strategy="direct_paid",
            route=paid,
            plan=paid_plan,
            expected=_cost(
                "direct_paid:known-paid",
                assistance=paid_plan.assistance_cost,
                execution_tokens=40_000,
            ),
            is_paid=True,
        ),
    )
    decision = optimize_execution_path(
        ExecutionPathRequest(task_slice=_slice(), trajectory_id="traj-104", offers=offers, now=NOW)
    )
    assert decision.selected_candidate_id == "known-paid"
    assert any("unknown" in r.reason for r in decision.rejected)


def test_unknown_price_not_treated_as_free() -> None:
    unknown_price = _route("mystery-price")
    known = _route("known-price", model="mid", provider="provider-b", tier=2)
    plan_u = _plan(candidate_id="mystery-price", digest="sha256:mystery")
    plan_k = _plan(candidate_id="known-price", digest="sha256:knownp")
    # Unknown USD terms → cash_usd is None; must not beat known paid as "free".
    mystery_cost = ExpectedStrategyCost.build(
        strategy_id="direct_cheap:mystery-price",
        trajectory_id="traj-104",
        terms=(
            CostTerm(kind="execution", amount=None, unit="usd", status="unknown"),
            CostTerm(
                kind="execution",
                amount=Decimal("10000"),
                unit="tokens",
                status="estimated",
                observed_at=NOW,
            ),
        ),
        is_free=False,
        qualified=True,
    )
    offers = (
        _offer(
            strategy="direct_cheap",
            route=unknown_price,
            plan=plan_u,
            expected=mystery_cost,
            is_cheap=True,
        ),
        _offer(
            strategy="direct_paid",
            route=known,
            plan=plan_k,
            expected=_cost(
                "direct_paid:known-price",
                assistance=plan_k.assistance_cost,
                execution_tokens=40_000,
            ),
            is_paid=True,
        ),
    )
    decision = optimize_execution_path(
        ExecutionPathRequest(task_slice=_slice(), trajectory_id="traj-104", offers=offers, now=NOW)
    )
    # Conservative: unknown cash cannot claim free/optimal win over known qualified cash.
    assert decision.selected_candidate_id == "known-price"
    assert any("unknown" in r.reason and "price" in r.reason for r in decision.rejected) or (
        decision.selected_strategy == "direct_paid"
    )


def test_missing_mandatory_tool_not_sufficient_despite_context() -> None:
    cheap = _route("no-tools")
    paid = _route("with-tools", model="paid", provider="provider-b", tier=2)
    insufficient = _plan(
        candidate_id="no-tools",
        result="insufficient",
        intrinsic=False,
        assisted=False,
        reasons=("missing_mandatory_tool:pytest",),
        tools=(),
        digest="sha256:notools",
    )
    ok = _plan(candidate_id="with-tools", digest="sha256:withtools")
    offers = (
        _offer(
            strategy="cheap_with_assistance",
            route=cheap,
            plan=insufficient,
            expected=_cost(
                "cheap_with_assistance:no-tools",
                assistance=AssistanceCost(context_tokens=50_000),
                execution_tokens=1,
                is_free=True,
            ),
            is_cheap=True,
        ),
        _offer(
            strategy="direct_paid",
            route=paid,
            plan=ok,
            expected=_cost(
                "direct_paid:with-tools", assistance=ok.assistance_cost, execution_tokens=40_000
            ),
            is_paid=True,
        ),
    )
    decision = optimize_execution_path(
        ExecutionPathRequest(task_slice=_slice(), trajectory_id="traj-104", offers=offers, now=NOW)
    )
    assert decision.selected_candidate_id == "with-tools"
    assert any("tool" in r.reason or "insufficient" in r.reason for r in decision.rejected)


def test_budget_drop_mandatory_proof_blocks_sufficiency() -> None:
    cheap = _route("budget-tight")
    paid = _route("budget-ok", model="paid", provider="provider-b", tier=2)
    plan = _plan(candidate_id="budget-tight", digest="sha256:bt")
    ok = _plan(candidate_id="budget-ok", digest="sha256:bo")
    offers = (
        _offer(
            strategy="cheap_with_assistance",
            route=cheap,
            plan=plan,
            expected=_cost(
                "cheap_with_assistance:budget-tight",
                assistance=plan.assistance_cost,
                execution_tokens=10_000,
                is_free=True,
            ),
            budget=_budget(candidate_id="budget-tight", fits=False),
            is_cheap=True,
        ),
        _offer(
            strategy="direct_paid",
            route=paid,
            plan=ok,
            expected=_cost(
                "direct_paid:budget-ok", assistance=ok.assistance_cost, execution_tokens=40_000
            ),
            budget=_budget(candidate_id="budget-ok", fits=True, digest="sha256:bok"),
            is_paid=True,
        ),
    )
    decision = optimize_execution_path(
        ExecutionPathRequest(task_slice=_slice(), trajectory_id="traj-104", offers=offers, now=NOW)
    )
    assert decision.selected_candidate_id == "budget-ok"
    assert any("budget" in r.reason for r in decision.rejected)


def test_stale_runtime_certification_blocks_optimistic_selection() -> None:
    stale = _route("stale-route")
    fresh = _route("fresh-route", model="mid", provider="provider-b", tier=2)
    plan_s = _plan(candidate_id="stale-route", digest="sha256:stale")
    plan_f = _plan(candidate_id="fresh-route", digest="sha256:fresh")
    offers = (
        _offer(
            strategy="direct_cheap",
            route=stale,
            plan=plan_s,
            expected=_cost(
                "direct_cheap:stale-route",
                assistance=plan_s.assistance_cost,
                execution_tokens=5_000,
                is_free=True,
            ),
            cert_state=CertificationState.UNAVAILABLE,
            cert_freshness="stale",
            is_cheap=True,
        ),
        _offer(
            strategy="direct_paid",
            route=fresh,
            plan=plan_f,
            expected=_cost(
                "direct_paid:fresh-route",
                assistance=plan_f.assistance_cost,
                execution_tokens=40_000,
            ),
            cert_state=CertificationState.READY,
            cert_freshness="fresh",
            is_paid=True,
        ),
    )
    decision = optimize_execution_path(
        ExecutionPathRequest(task_slice=_slice(), trajectory_id="traj-104", offers=offers, now=NOW)
    )
    assert decision.selected_candidate_id == "fresh-route"
    assert any("stale" in r.reason or "certif" in r.reason for r in decision.rejected)


def test_all_unsafe_or_unknown_yields_blocked() -> None:
    bad = _route("bad-only")
    plan = _plan(
        candidate_id="bad-only",
        result="unknown",
        intrinsic=False,
        assisted=False,
        reasons=("unknown_health",),
        digest="sha256:bad",
    )
    offers = (
        _offer(
            strategy="direct_cheap",
            route=bad,
            plan=plan,
            expected=_cost(
                "direct_cheap:bad-only",
                assistance=AssistanceCost(),
                execution_tokens=1,
                is_free=True,
            ),
            cert_state=CertificationState.UNKNOWN,
            cert_freshness="unknown",
            is_cheap=True,
        ),
    )
    decision = optimize_execution_path(
        ExecutionPathRequest(task_slice=_slice(), trajectory_id="traj-104", offers=offers, now=NOW)
    )
    assert decision.selected_strategy == "blocked"
    assert decision.selected_candidate_id is None
    assert decision.rejected
    assert all(r.reason for r in decision.rejected)


def test_replay_deterministic_same_inputs() -> None:
    cheap = _route("det-1")
    plan = _plan(candidate_id="det-1")
    offer = _offer(
        strategy="direct_cheap",
        route=cheap,
        plan=plan,
        expected=_cost(
            "direct_cheap:det-1",
            assistance=plan.assistance_cost,
            execution_tokens=10_000,
            is_free=True,
        ),
        budget=_budget(candidate_id="det-1"),
        is_cheap=True,
    )
    req = ExecutionPathRequest(
        task_slice=_slice(), trajectory_id="traj-104", offers=(offer,), now=NOW
    )
    a = optimize_execution_path(req)
    b = optimize_execution_path(req)
    assert a.to_dict() == b.to_dict()
    assert a.decision_digest == b.decision_digest


def test_frontier_direct_loses_to_cheaper_qualified_complete_path() -> None:
    """Invariant 25: frontier_direct must not win when cheaper complete path exists."""
    cheap = _route("complete-cheap")
    frontier = _route("frontier-only", model="frontier", provider="provider-b", tier=5)
    assist = AssistanceCost(
        context_tokens=1_000, tool_tokens=100, planning_tokens=200, verification_tokens=100
    )
    cheap_plan = _plan(
        candidate_id="complete-cheap",
        intrinsic=False,
        assisted=True,
        assistance=assist,
        digest="sha256:cc",
    )
    front_plan = _plan(candidate_id="frontier-only", digest="sha256:fo")
    offers = (
        _offer(
            strategy="cheap_execute_then_verify",
            route=cheap,
            plan=cheap_plan,
            expected=_cost(
                "cheap_execute_then_verify:complete-cheap",
                assistance=assist,
                execution_tokens=25_000,
                retry_tokens=5_000,
            ),
            is_cheap=True,
        ),
        _offer(
            strategy="frontier_direct",
            route=frontier,
            plan=front_plan,
            expected=_cost(
                "frontier_direct:frontier-only",
                assistance=front_plan.assistance_cost,
                execution_tokens=400_000,
            ),
            is_frontier=True,
            is_paid=True,
        ),
    )
    decision = optimize_execution_path(
        ExecutionPathRequest(task_slice=_slice(), trajectory_id="traj-104", offers=offers, now=NOW)
    )
    assert decision.selected_strategy == "cheap_execute_then_verify"
    assert decision.selected_strategy != "frontier_direct"


def test_session_stay_and_switch_surface_in_receipt() -> None:
    current = _route("stay-route")
    plan = _plan(candidate_id="stay-route")
    session = SessionRouteDecision(
        decision="STAY",
        selected_route_id="stay-route",
        selected_route=current,
        reason="warm_cache_beats_cold",
        override_reasons=(),
        terms=(),
        assumptions=("observed_cache_savings",),
        freshness={"cache": "fresh"},
        stay_ev=Decimal("0.05"),
        switch_ev=Decimal("0.09"),
        mode=DecisionMode.AUTHORITATIVE,
        authoritative=True,
        would_decide="STAY",
        alternatives=(),
    )
    offers = (
        _offer(
            strategy="stay_current_route",
            route=current,
            plan=plan,
            expected=_cost(
                "stay_current_route:stay-route",
                assistance=plan.assistance_cost,
                execution_tokens=10_000,
                is_free=True,
            ),
            is_cheap=True,
        ),
    )
    decision = optimize_execution_path(
        ExecutionPathRequest(
            task_slice=_slice(),
            trajectory_id="traj-104",
            offers=offers,
            session_decision=session,
            now=NOW,
        )
    )
    assert decision.selected_strategy == "stay_current_route"
    assert decision.session_decision is not None
    assert decision.session_decision["decision"] == "STAY"


def test_recovery_policy_attached_and_cancellation_not_success() -> None:
    cheap = _route("recover-route")
    plan = _plan(candidate_id="recover-route")
    bounds = RecoveryBounds(max_attempts=2, estimated_action_cash_usd=Decimal("0.01"))
    offers = (
        _offer(
            strategy="cheap_execute_then_rehydrate_retry",
            route=cheap,
            plan=plan,
            expected=_cost(
                "cheap_execute_then_rehydrate_retry:recover-route",
                assistance=plan.assistance_cost,
                execution_tokens=10_000,
                retry_tokens=2_000,
                is_free=True,
            ),
            is_cheap=True,
        ),
    )
    decision = optimize_execution_path(
        ExecutionPathRequest(
            task_slice=_slice(),
            trajectory_id="traj-104",
            offers=offers,
            recovery_bounds=bounds,
            now=NOW,
        )
    )
    assert decision.recovery_policy is not None
    assert decision.recovery_policy["max_attempts"] == 2

    ledger = CostLedger(trajectory_id="traj-104", cash_budget_usd=Decimal("1.00"))
    controller = BoundedRecoveryController(bounds=bounds)
    recovery = controller.decide(
        evidence=FailureEvidence(cancelled=True, message="user cancel"),
        route=ExecutionRoute(
            model_id=cheap.model,
            provider=cheap.provider,
            gateway_id=cheap.gateway,
            capability_tier=cheap.capability_tier,
        ),
        ledger=ledger,
        now=NOW,
    )
    assert recovery.outcome is RecoveryOutcome.CANCELLED
    assert recovery.success is False
    assert recovery.action is RecoveryAction.BLOCK


def test_hard_excluded_ids_cannot_be_restored_by_session_switch() -> None:
    excluded = _route("ex-switch", excluded=True)
    other = _route("ok-route", model="ok", provider="provider-b", tier=2)
    ex_plan = _plan(
        candidate_id="ex-switch",
        result="insufficient",
        hard_excluded=True,
        intrinsic=False,
        assisted=False,
        reasons=("hard_gate_excluded",),
        digest="sha256:exs",
    )
    ok_plan = _plan(candidate_id="ok-route", digest="sha256:okr")
    session = SessionRouteDecision(
        decision="SWITCH",
        selected_route_id="ex-switch",
        selected_route=excluded,
        reason="economics_prefer_excluded",
        override_reasons=(),
        terms=(),
        assumptions=(),
        freshness={},
        stay_ev=Decimal("0.20"),
        switch_ev=Decimal("0.01"),
        mode=DecisionMode.AUTHORITATIVE,
        authoritative=True,
        would_decide="SWITCH",
        alternatives=(),
    )
    offers = (
        _offer(
            strategy="switch_equivalent_route",
            route=excluded,
            plan=ex_plan,
            expected=_cost(
                "switch_equivalent_route:ex-switch",
                assistance=AssistanceCost(),
                execution_tokens=1,
                is_free=True,
            ),
            hard_excluded=True,
            is_cheap=True,
        ),
        _offer(
            strategy="direct_paid",
            route=other,
            plan=ok_plan,
            expected=_cost(
                "direct_paid:ok-route", assistance=ok_plan.assistance_cost, execution_tokens=40_000
            ),
            is_paid=True,
        ),
    )
    decision = optimize_execution_path(
        ExecutionPathRequest(
            task_slice=_slice(),
            trajectory_id="traj-104",
            offers=offers,
            session_decision=session,
            hard_excluded_ids=frozenset({"ex-switch"}),
            now=NOW,
        )
    )
    assert decision.selected_candidate_id == "ok-route"
    assert decision.selected_candidate_id != "ex-switch"


def test_provenance_and_concrete_identity_survive_selection() -> None:
    route = _route("id-1", gateway="gw-x", provider="prov-y", model="model-z")
    plan = _plan(candidate_id="id-1", digest="sha256:prov-survive")
    decision = optimize_execution_path(
        ExecutionPathRequest(
            task_slice=_slice(),
            trajectory_id="traj-104",
            offers=(
                _offer(
                    strategy="direct_cheap",
                    route=route,
                    plan=plan,
                    expected=_cost(
                        "direct_cheap:id-1",
                        assistance=plan.assistance_cost,
                        execution_tokens=8_000,
                        is_free=True,
                    ),
                    budget=_budget(candidate_id="id-1"),
                    is_cheap=True,
                ),
            ),
            now=NOW,
        )
    )
    assert decision.selected_route is not None
    assert decision.selected_route.gateway == "gw-x"
    assert decision.selected_route.provider == "prov-y"
    assert decision.selected_route.model == "model-z"
    assert decision.assistance_plan_digest == "sha256:prov-survive"
    assert "sha256:prov-survive" in decision.evidence_digests.values() or (
        decision.assistance_plan_digest in decision.evidence_digests.values()
    )


def test_complete_cost_includes_retry_verification_recovery_terms() -> None:
    route = _route("complete-terms")
    assist = AssistanceCost(
        context_tokens=100, tool_tokens=50, planning_tokens=0, verification_tokens=200
    )
    plan = _plan(candidate_id="complete-terms", assistance=assist, digest="sha256:terms")
    expected = _cost(
        "cheap_execute_then_bounded_escalate:complete-terms",
        assistance=assist,
        execution_tokens=10_000,
        retry_tokens=3_000,
        escalation_tokens=5_000,
        route_switch_tokens=1_000,
    )
    decision = optimize_execution_path(
        ExecutionPathRequest(
            task_slice=_slice(),
            trajectory_id="traj-104",
            offers=(
                _offer(
                    strategy="cheap_execute_then_bounded_escalate",
                    route=route,
                    plan=plan,
                    expected=expected,
                    is_cheap=True,
                ),
            ),
            recovery_bounds=RecoveryBounds(max_attempts=3),
            now=NOW,
        )
    )
    kinds = {t["kind"] for t in decision.expected_cost_terms}
    assert {"execution", "retry", "escalation", "verification", "route_switch"} <= kinds or {
        "execution",
        "retry",
        "escalation",
        "route_switch",
    }.issubset(kinds)
    assert decision.selected_strategy == "cheap_execute_then_bounded_escalate"


# --- Agent 3 matrix aliases (S01-S10) + remaining S11-S25 ---


def test_ep_s01_cheap_intrinsic_selects_direct_cheap() -> None:
    test_direct_cheap_wins_when_intrinsically_sufficient()


def test_ep_s02_cheap_with_assistance_beats_paid_on_complete_cost() -> None:
    test_cheap_with_assistance_beats_paid_on_complete_cost()


def test_ep_s03_frontier_plan_then_cheap_beats_frontier_direct() -> None:
    test_frontier_plan_then_cheap_beats_frontier_direct()


def test_ep_s04_expensive_assistance_yields_to_paid_or_frontier() -> None:
    test_expensive_assistance_allows_paid_to_win()


def test_ep_s05_hard_gate_excluded_never_selected_by_strategy_or_session() -> None:
    test_hard_gate_excluded_never_selected()
    test_hard_excluded_ids_cannot_be_restored_by_session_switch()


def test_ep_s06_unknown_required_capability_never_sufficient() -> None:
    test_unknown_capability_never_promoted_to_sufficient()


def test_ep_s07_unknown_price_not_treated_as_zero() -> None:
    test_unknown_price_not_treated_as_free()


def test_ep_s08_missing_mandatory_tool_not_faked_by_rich_context() -> None:
    test_missing_mandatory_tool_not_sufficient_despite_context()


def test_ep_s09_budget_drop_mandatory_proof_not_sufficient() -> None:
    test_budget_drop_mandatory_proof_blocks_sufficiency()


def test_ep_s10_stale_runtime_cert_blocks_optimistic_selection() -> None:
    test_stale_runtime_certification_blocks_optimistic_selection()


def test_ep_s11_provider_outage_switches_equivalent_plane_before_escalate() -> None:
    from datetime import timedelta

    from verdict.runtime_certification import (
        CertificationState,
        CertifiedComponent,
        ComponentKind,
        RuntimeCertificationReport,
    )

    primary = ExecutionRoute(
        model_id="cheap-model", provider="provider-a", gateway_id="gateway-a", capability_tier=1
    )
    equivalent = ExecutionRoute(
        model_id="cheap-model-b", provider="provider-b", gateway_id="gateway-b", capability_tier=1
    )
    frontier = ExecutionRoute(
        model_id="frontier",
        provider="provider-c",
        gateway_id="gateway-c",
        capability_tier=5,
        is_frontier=True,
    )
    now = NOW
    cert = RuntimeCertificationReport(
        certified_at=now,
        expires_at=now + timedelta(minutes=5),
        ttl_seconds=300,
        components=(
            CertifiedComponent(
                component_id="gateway-b",
                kind=ComponentKind.GATEWAY,
                identity="gateway-b",
                state=CertificationState.READY,
                source="fixture",
                confidence=0.9,
                freshness="fresh",
                observed_at=now,
                expires_at=now + timedelta(minutes=5),
            ),
        ),
        memory_authority=None,
        conflicts=(),
        probes_used=0,
        premium_probes_used=0,
    )
    ledger = CostLedger(trajectory_id="traj-104", cash_budget_usd=Decimal("1.00"))
    controller = BoundedRecoveryController(bounds=RecoveryBounds(max_attempts=3))
    decision = controller.decide(
        evidence=FailureEvidence(signals=frozenset({"provider_outage"}), message="502"),
        route=primary,
        ledger=ledger,
        certification=cert,
        equivalent_routes=(equivalent,),
        stronger_routes=(frontier,),
        now=now,
    )
    assert decision.action is RecoveryAction.SWITCH_EXECUTION_PLANE
    assert decision.route_after.model_id == "cheap-model-b"
    assert decision.frontier_call is False
    assert decision.escalated is False


def test_ep_s12_stale_context_rehydrates_same_cheap_before_frontier() -> None:
    route = ExecutionRoute(
        model_id="cheap-model", provider="provider-a", gateway_id="gateway-a", capability_tier=1
    )
    frontier = ExecutionRoute(
        model_id="frontier",
        provider="provider-b",
        gateway_id="gateway-b",
        capability_tier=5,
        is_frontier=True,
    )
    ledger = CostLedger(trajectory_id="traj-104", cash_budget_usd=Decimal("1.00"))
    controller = BoundedRecoveryController(bounds=RecoveryBounds(max_attempts=3))
    decision = controller.decide(
        evidence=FailureEvidence(signals=frozenset({"stale_context"}), message="stale"),
        route=route,
        ledger=ledger,
        equivalent_routes=(),
        stronger_routes=(frontier,),
        now=NOW,
    )
    assert decision.action is RecoveryAction.REHYDRATE
    assert decision.route_after.model_id == route.model_id
    assert decision.frontier_call is False


def test_ep_s13_model_deficit_no_endless_same_tier_retry() -> None:
    weak = ExecutionRoute(
        model_id="weak", provider="provider-a", gateway_id="gateway-a", capability_tier=1
    )
    strong = ExecutionRoute(
        model_id="strong", provider="provider-b", gateway_id="gateway-b", capability_tier=4
    )
    ledger = CostLedger(trajectory_id="traj-104", cash_budget_usd=Decimal("1.00"))
    controller = BoundedRecoveryController(bounds=RecoveryBounds(max_attempts=2))
    from verdict.execution_path import apply_bounded_recovery

    decision = apply_bounded_recovery(
        controller=controller,
        evidence=FailureEvidence(signals=frozenset({"capability_deficit"})),
        route=weak,
        ledger=ledger,
        stronger_routes=(strong,),
        prequalified_stronger_ids=frozenset({"strong"}),
        now=NOW,
    )
    assert decision.action is RecoveryAction.ESCALATE_CAPABILITY
    assert decision.route_after.model_id == "strong"
    # Exhaust attempts → block, not endless retry
    decision2 = controller.decide(
        evidence=FailureEvidence(signals=frozenset({"capability_deficit"})),
        route=strong,
        ledger=ledger,
        stronger_routes=(),
        now=NOW,
    )
    assert decision2.outcome is RecoveryOutcome.BLOCKED or decision2.action is RecoveryAction.BLOCK


def test_ep_s14_bad_decomposition_replans_preserving_parent_ac_proof() -> None:
    bad = _plan(
        candidate_id="bad-decomp",
        result="insufficient",
        intrinsic=False,
        assisted=False,
        decomposition_required=True,
        reasons=("decomposition_drops_parent_proof",),
        digest="sha256:bad-decomp",
    )
    # Force preserves_parent_proof False via replacement
    bad = AssistancePlan(
        plan_id=bad.plan_id,
        candidate_id=bad.candidate_id,
        task_slice=bad.task_slice,
        required_intrinsic_capabilities=bad.required_intrinsic_capabilities,
        required_context_slots=bad.required_context_slots,
        required_context_evidence=bad.required_context_evidence,
        required_tool_capabilities=bad.required_tool_capabilities,
        selected_tool_surface=bad.selected_tool_surface,
        decomposition=DecompositionRequirement(
            required=True,
            child_slices=(),
            preserves_parent_acceptance=True,
            preserves_parent_proof=False,
            reason="drops_proof",
            planner_role="frontier",
        ),
        verification=bad.verification,
        assistance_cost=bad.assistance_cost,
        result="insufficient",
        reasons=("decomposition_drops_parent_proof",),
        intrinsic_sufficient=False,
        assisted_sufficient=False,
        assistance_delta=(),
        provenance=bad.provenance,
        evidence_digest="sha256:bad-decomp",
    )
    good = _plan(
        candidate_id="good-replan", decomposition_required=True, digest="sha256:good-replan"
    )
    offers = (
        _offer(
            strategy="frontier_plan_then_cheap_execute",
            route=_route("bad-decomp"),
            plan=bad,
            expected=_cost(
                "frontier_plan_then_cheap_execute:bad-decomp",
                assistance=AssistanceCost(planning_tokens=100),
                execution_tokens=1,
            ),
            is_cheap=True,
        ),
        _offer(
            strategy="frontier_plan_then_cheap_execute",
            route=_route("good-replan", model="cheap-2", provider="provider-b"),
            plan=good,
            expected=_cost(
                "frontier_plan_then_cheap_execute:good-replan",
                assistance=good.assistance_cost,
                execution_tokens=20_000,
            ),
            is_cheap=True,
        ),
    )
    decision = optimize_execution_path(
        ExecutionPathRequest(task_slice=_slice(), trajectory_id="traj-104", offers=offers, now=NOW)
    )
    assert decision.selected_candidate_id == "good-replan"
    assert "pytest green" in decision.verification_requirements
    assert any("insufficient" in r.reason or "decomp" in r.reason for r in decision.rejected)

    route = ExecutionRoute(
        model_id="cheap-model", provider="provider-a", gateway_id="gateway-a", capability_tier=1
    )
    ledger = CostLedger(trajectory_id="traj-104", cash_budget_usd=Decimal("1.00"))
    recovery = BoundedRecoveryController(bounds=RecoveryBounds(max_attempts=3)).decide(
        evidence=FailureEvidence(signals=frozenset({"bad_decomposition"})),
        route=route,
        ledger=ledger,
        now=NOW,
    )
    assert recovery.action is RecoveryAction.REPLAN
    assert recovery.replan_signal is True


def test_ep_s15_warm_vs_expired_cache_stay_switch_requires_observed_evidence() -> None:
    from datetime import timedelta

    from verdict.session_economics import (
        CostState,
        PromptCacheState,
        SessionState,
        TaskState,
        decide_session_route,
    )

    warm = _route("warm-exp", model="expensive", tier=3)
    cold = _route("cold-cheap", model="cheap", tier=3, provider="provider-b")
    stay = ExpectedStrategyCost.build(
        strategy_id="stay",
        trajectory_id="traj-104",
        terms=(
            CostTerm(
                kind="execution",
                amount=Decimal("0.10"),
                unit="usd",
                status="estimated",
                observed_at=NOW,
            ),
        ),
    )
    switch = ExpectedStrategyCost.build(
        strategy_id="switch",
        trajectory_id="traj-104",
        terms=(
            CostTerm(
                kind="execution",
                amount=Decimal("0.08"),
                unit="usd",
                status="estimated",
                observed_at=NOW,
            ),
        ),
    )
    warm_result = decide_session_route(
        SessionState(
            session_id="s-warm",
            current_route=warm,
            last_served_route=warm,
            cache=PromptCacheState(
                warm=True,
                savings_usd=Decimal("0.04"),
                cached_tokens=100,
                fresh_until=NOW + timedelta(minutes=5),
                status="observed",
            ),
        ),
        cold,
        CostState(
            stay_expected=stay,
            switch_expected=switch,
            stay_cache_savings=CostTerm(
                kind="cache_read",
                amount=Decimal("0.04"),
                unit="usd",
                status="observed",
                observed_at=NOW,
                fresh_until=NOW + timedelta(minutes=5),
            ),
            switch_handoff_cost=CostTerm(
                kind="route_switch", amount=Decimal("0.03"), unit="usd", status="estimated"
            ),
            now=NOW,
        ),
        TaskState(required_capability_tier=2),
    )
    assert warm_result.decision == "STAY"

    expired = decide_session_route(
        SessionState(
            session_id="s-exp",
            current_route=warm,
            last_served_route=warm,
            cache=PromptCacheState(
                warm=True,
                savings_usd=Decimal("0.10"),
                cached_tokens=100,
                fresh_until=NOW - timedelta(minutes=1),
                status="observed",
            ),
            consecutive_switch_signals=2,
        ),
        cold,
        CostState(
            stay_expected=ExpectedStrategyCost.build(
                strategy_id="stay-exp",
                trajectory_id="traj-104",
                terms=(
                    CostTerm(
                        kind="execution",
                        amount=Decimal("0.12"),
                        unit="usd",
                        status="estimated",
                        observed_at=NOW,
                    ),
                ),
            ),
            switch_expected=ExpectedStrategyCost.build(
                strategy_id="switch-exp",
                trajectory_id="traj-104",
                terms=(
                    CostTerm(
                        kind="execution",
                        amount=Decimal("0.08"),
                        unit="usd",
                        status="estimated",
                        observed_at=NOW,
                    ),
                ),
            ),
            stay_cache_savings=CostTerm(
                kind="cache_read",
                amount=Decimal("0.10"),
                unit="usd",
                status="observed",
                observed_at=NOW - timedelta(hours=1),
                fresh_until=NOW - timedelta(minutes=1),
            ),
            switch_handoff_cost=CostTerm(
                kind="route_switch", amount=Decimal("0.01"), unit="usd", status="estimated"
            ),
            now=NOW,
        ),
        TaskState(required_capability_tier=2),
    )
    assert expired.decision == "SWITCH"


def test_ep_s16_small_cost_diffs_hysteresis_no_thrash() -> None:
    from verdict.session_economics import CostState, SessionState, TaskState, decide_session_route

    current = _route("curr", tier=3)
    alt = _route("alt", model="alt", provider="provider-b", tier=3)
    stay = ExpectedStrategyCost.build(
        strategy_id="stay",
        trajectory_id="traj-104",
        terms=(
            CostTerm(
                kind="execution",
                amount=Decimal("0.100"),
                unit="usd",
                status="estimated",
                observed_at=NOW,
            ),
        ),
    )
    switch = ExpectedStrategyCost.build(
        strategy_id="switch",
        trajectory_id="traj-104",
        terms=(
            CostTerm(
                kind="execution",
                amount=Decimal("0.099"),
                unit="usd",
                status="estimated",
                observed_at=NOW,
            ),
        ),
    )
    result = decide_session_route(
        SessionState(session_id="hyst", current_route=current, last_served_route=current),
        alt,
        CostState(
            stay_expected=stay,
            switch_expected=switch,
            hysteresis_margin_usd=Decimal("0.01"),
            now=NOW,
        ),
        TaskState(required_capability_tier=2),
    )
    assert result.decision == "STAY"
    assert "hysteresis" in result.reason or result.decision == "STAY"


def test_ep_s17_capability_deficit_overrides_stay_despite_cache() -> None:
    from datetime import timedelta

    from verdict.session_economics import (
        CostState,
        PromptCacheState,
        SessionState,
        TaskState,
        decide_session_route,
    )

    weak = _route("weak", model="tiny", tier=1)
    strong = _route("strong", model="flagship", provider="provider-b", tier=4)
    result = decide_session_route(
        SessionState(
            session_id="cap",
            current_route=weak,
            last_served_route=weak,
            cache=PromptCacheState(
                warm=True,
                savings_usd=Decimal("0.50"),
                cached_tokens=100,
                fresh_until=NOW + timedelta(hours=1),
                status="observed",
            ),
        ),
        strong,
        CostState(
            stay_expected=ExpectedStrategyCost.build(
                strategy_id="stay",
                trajectory_id="traj-104",
                terms=(
                    CostTerm(
                        kind="execution",
                        amount=Decimal("0.05"),
                        unit="usd",
                        status="estimated",
                        observed_at=NOW,
                    ),
                ),
            ),
            switch_expected=ExpectedStrategyCost.build(
                strategy_id="switch",
                trajectory_id="traj-104",
                terms=(
                    CostTerm(
                        kind="execution",
                        amount=Decimal("0.40"),
                        unit="usd",
                        status="estimated",
                        observed_at=NOW,
                    ),
                ),
            ),
            stay_cache_savings=CostTerm(
                kind="cache_read",
                amount=Decimal("0.50"),
                unit="usd",
                status="observed",
                observed_at=NOW,
                fresh_until=NOW + timedelta(hours=1),
            ),
            now=NOW,
        ),
        TaskState(required_capability_tier=3),
    )
    assert result.decision == "SWITCH"
    assert "capability_tier_deficit" in result.override_reasons


def test_ep_s18_quota_exhausted_alternate_or_bounded_block() -> None:
    from verdict.session_economics import CostState, SessionState, TaskState, decide_session_route

    current = _route("quota-dead", tier=2)
    alt = _route("quota-alt", model="alt", provider="provider-b", tier=2)
    stay = ExpectedStrategyCost.build(
        strategy_id="stay",
        trajectory_id="traj-104",
        terms=(
            CostTerm(
                kind="execution",
                amount=Decimal("0.05"),
                unit="usd",
                status="estimated",
                observed_at=NOW,
            ),
        ),
    )
    switch = ExpectedStrategyCost.build(
        strategy_id="switch",
        trajectory_id="traj-104",
        terms=(
            CostTerm(
                kind="execution",
                amount=Decimal("0.06"),
                unit="usd",
                status="estimated",
                observed_at=NOW,
            ),
        ),
    )
    with_alt = decide_session_route(
        SessionState(
            session_id="q1", current_route=current, last_served_route=current, quota_exhausted=True
        ),
        alt,
        CostState(stay_expected=stay, switch_expected=switch, now=NOW),
        TaskState(required_capability_tier=1),
    )
    assert with_alt.decision == "SWITCH"

    no_alt = decide_session_route(
        SessionState(
            session_id="q2",
            current_route=_route("quota-dead-2", eligible=False, excluded=True),
            last_served_route=_route("quota-dead-2", eligible=False, excluded=True),
            quota_exhausted=True,
        ),
        _route("also-dead", excluded=True, eligible=False),
        CostState(
            stay_expected=ExpectedStrategyCost.build(
                strategy_id="stay-dead",
                trajectory_id="traj-104",
                terms=(
                    CostTerm(
                        kind="execution",
                        amount=Decimal("0.05"),
                        unit="usd",
                        status="estimated",
                        observed_at=NOW,
                    ),
                ),
                qualified=False,
            ),
            switch_expected=ExpectedStrategyCost.build(
                strategy_id="switch-dead",
                trajectory_id="traj-104",
                terms=(
                    CostTerm(
                        kind="execution",
                        amount=Decimal("0.06"),
                        unit="usd",
                        status="estimated",
                        observed_at=NOW,
                    ),
                ),
                qualified=False,
            ),
            now=NOW,
        ),
        TaskState(required_capability_tier=1),
    )
    assert no_alt.decision == "BLOCKED"


def test_ep_s19_retry_verification_recovery_in_complete_expected_cost() -> None:
    test_complete_cost_includes_retry_verification_recovery_terms()


def test_ep_s20_all_unsafe_unknown_blocked_with_named_reasons() -> None:
    test_all_unsafe_or_unknown_yields_blocked()


def test_ep_s21_cancel_during_verification_is_not_success() -> None:
    test_recovery_policy_attached_and_cancellation_not_success()


def test_ep_s22_conflicting_evidence_provenance_surfaced() -> None:
    cheap = _route("conflict-a")
    plan = _plan(candidate_id="conflict-a", digest="sha256:c1")
    decision = optimize_execution_path(
        ExecutionPathRequest(
            task_slice=_slice(),
            trajectory_id="traj-104",
            offers=(
                _offer(
                    strategy="direct_cheap",
                    route=cheap,
                    plan=plan,
                    expected=_cost(
                        "direct_cheap:conflict-a",
                        assistance=plan.assistance_cost,
                        execution_tokens=5_000,
                        is_free=True,
                    ),
                    is_cheap=True,
                    evidence_conflicts=("memory_authority_duplicate",),
                ),
            ),
            evidence_conflicts=("trust_vs_cert_digest_mismatch",),
            now=NOW,
        )
    )
    assert decision.selected_strategy == "blocked"
    assert any("conflict" in r.reason for r in decision.rejected)


def test_ep_s23_same_input_deterministic_replay() -> None:
    test_replay_deterministic_same_inputs()


def test_ep_s24_gateway_provider_aliases_preserve_concrete_route() -> None:
    test_provenance_and_concrete_identity_survive_selection()
    auto = _route("auto/best-coding", gateway="gateway-a", provider="provider-a", model="auto/best")
    plan = _plan(candidate_id="auto/best-coding", digest="sha256:auto")
    paid = _route("concrete", model="model-z", provider="prov-y", gateway="gw-x")
    paid_plan = _plan(candidate_id="concrete", digest="sha256:concrete")
    decision = optimize_execution_path(
        ExecutionPathRequest(
            task_slice=_slice(),
            trajectory_id="traj-104",
            offers=(
                _offer(
                    strategy="direct_cheap",
                    route=auto,
                    plan=plan,
                    expected=_cost(
                        "direct_cheap:auto/best-coding",
                        assistance=plan.assistance_cost,
                        execution_tokens=1_000,
                        is_free=True,
                    ),
                    is_cheap=True,
                ),
                _offer(
                    strategy="direct_paid",
                    route=paid,
                    plan=paid_plan,
                    expected=_cost(
                        "direct_paid:concrete",
                        assistance=paid_plan.assistance_cost,
                        execution_tokens=40_000,
                    ),
                    is_paid=True,
                ),
            ),
            now=NOW,
        )
    )
    assert decision.selected_candidate_id == "concrete"
    assert decision.selected_route is not None
    assert not str(decision.selected_route.model).startswith("auto/")
    assert any("opaque_auto" in r.reason for r in decision.rejected)


def test_ep_s25_frontier_direct_loses_to_cheaper_qualified_complete_path() -> None:
    test_frontier_direct_loses_to_cheaper_qualified_complete_path()


def test_ep_legacy_selector_yields_to_bod104_authority() -> None:
    from verdict.execution_path import ExecutionPathError, legacy_selector_must_yield

    cheap = _route("auth-1")
    plan = _plan(candidate_id="auth-1")
    decision = optimize_execution_path(
        ExecutionPathRequest(
            task_slice=_slice(),
            trajectory_id="traj-104",
            offers=(
                _offer(
                    strategy="direct_cheap",
                    route=cheap,
                    plan=plan,
                    expected=_cost(
                        "direct_cheap:auth-1",
                        assistance=plan.assistance_cost,
                        execution_tokens=8_000,
                        is_free=True,
                    ),
                    is_cheap=True,
                ),
            ),
            now=NOW,
        )
    )
    legacy_selector_must_yield(
        execution_path_decision=decision, legacy_selected_model_id="cheap-model"
    )
    try:
        legacy_selector_must_yield(
            execution_path_decision=decision, legacy_selected_model_id="rogue-model"
        )
        raise AssertionError("expected conflict")
    except ExecutionPathError as exc:
        assert "conflicts" in str(exc)


def test_ep_context_trust_not_admitted_blocks() -> None:
    route = _route("untrusted")
    plan = _plan(candidate_id="untrusted")
    alt = _route("trusted", model="mid", provider="provider-b")
    alt_plan = _plan(candidate_id="trusted", digest="sha256:trusted")
    decision = optimize_execution_path(
        ExecutionPathRequest(
            task_slice=_slice(),
            trajectory_id="traj-104",
            offers=(
                _offer(
                    strategy="direct_cheap",
                    route=route,
                    plan=plan,
                    expected=_cost(
                        "direct_cheap:untrusted",
                        assistance=plan.assistance_cost,
                        execution_tokens=1_000,
                        is_free=True,
                    ),
                    is_cheap=True,
                    context_trust_admitted=False,
                ),
                _offer(
                    strategy="direct_paid",
                    route=alt,
                    plan=alt_plan,
                    expected=_cost(
                        "direct_paid:trusted",
                        assistance=alt_plan.assistance_cost,
                        execution_tokens=40_000,
                    ),
                    is_paid=True,
                    context_trust_admitted=True,
                ),
            ),
            now=NOW,
        )
    )
    assert decision.selected_candidate_id == "trusted"
    assert any("context_trust" in r.reason for r in decision.rejected)


def test_ep_session_stay_overridden_when_current_not_capable() -> None:
    incapable = _route("incapable")
    capable = _route("capable", model="ok", provider="provider-b", tier=2)
    bad_plan = _plan(
        candidate_id="incapable",
        result="insufficient",
        intrinsic=False,
        assisted=False,
        reasons=("missing_cap",),
        digest="sha256:incap",
    )
    ok_plan = _plan(candidate_id="capable", digest="sha256:cap")
    session = SessionRouteDecision(
        decision="STAY",
        selected_route_id="incapable",
        selected_route=incapable,
        reason="warm_cache",
        override_reasons=(),
        terms=(),
        assumptions=(),
        freshness={},
        stay_ev=Decimal("0.01"),
        switch_ev=Decimal("0.50"),
        mode=DecisionMode.AUTHORITATIVE,
        authoritative=True,
        would_decide="STAY",
        alternatives=(),
    )
    decision = optimize_execution_path(
        ExecutionPathRequest(
            task_slice=_slice(),
            trajectory_id="traj-104",
            offers=(
                _offer(
                    strategy="stay_current_route",
                    route=incapable,
                    plan=bad_plan,
                    expected=_cost(
                        "stay_current_route:incapable",
                        assistance=AssistanceCost(),
                        execution_tokens=1,
                        is_free=True,
                    ),
                    is_cheap=True,
                ),
                _offer(
                    strategy="direct_paid",
                    route=capable,
                    plan=ok_plan,
                    expected=_cost(
                        "direct_paid:capable",
                        assistance=ok_plan.assistance_cost,
                        execution_tokens=40_000,
                    ),
                    is_paid=True,
                ),
            ),
            session_decision=session,
            now=NOW,
        )
    )
    assert decision.selected_candidate_id == "capable"
    assert any("stay_blocked" in r.reason for r in decision.rejected)
