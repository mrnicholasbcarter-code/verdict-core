"""BOD-67: hydrate-before-dispatch + bind already-authorized execution path."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any

import pytest

from verdict.context_pack import ContextUnit
from verdict.contracts import AvailabilitySnapshot, RuntimeCandidate
from verdict.cost_ledger import PriceEvidenceInput
from verdict.dispatcher import SwarmDispatcher
from verdict.effective_capability import (
    AssistanceCost,
    AssistancePlan,
    DecompositionRequirement,
    ProvenanceClaim,
    TaskSlice,
    VerificationStrategy,
)
from verdict.execution_path import (
    STRATEGY_AUTHORITY,
    ExecutionPathOffer,
    ExecutionPathRequest,
    optimize_execution_path,
)
from verdict.expected_cost import ExpectedStrategyCost, build_strategy_from_assistance
from verdict.optimized_dispatch import (
    DispatchReceipt,
    HydratedWorkerPack,
    OptimizedDispatchError,
    WorkerReturnContract,
    execute_optimized_dispatch,
    hydrate_worker_context,
    validate_worker_return,
)
from verdict.runtime_certification import CertificationState
from verdict.session_economics import ConcreteRoute

NOW = datetime(2026, 9, 19, 12, 0, tzinfo=timezone.utc)
PRICE: PriceEvidenceInput = {"input_usd_per_mtok": "1.00", "evidence_id": "price-1"}
PROOF = ("pytest green", "acceptance gates pass")


def _digest(text: str) -> str:
    return f"sha256:{hashlib.sha256(text.encode()).hexdigest()}"


def _slice(*, objective: str = "implement feature") -> TaskSlice:
    return TaskSlice(
        slice_id="slice-67",
        objective=objective,
        acceptance_criteria=("tests pass", "docs updated"),
        proof_criteria=PROOF,
    )


def _route(
    route_id: str,
    *,
    model: str,
    provider: str = "provider-a",
    pool: str = "pool-free",
    tier: int = 1,
) -> ConcreteRoute:
    return ConcreteRoute(
        route_id=route_id,
        gateway="gateway-a",
        provider=provider,
        model=model,
        credential_pool=pool,
        capability_tier=tier,
        eligible=True,
        excluded=False,
    )


def _plan(*, candidate_id: str, slice_: TaskSlice | None = None) -> AssistancePlan:
    task = slice_ or _slice()
    cost = AssistanceCost(
        context_tokens=0, tool_tokens=0, planning_tokens=0, verification_tokens=50
    )
    return AssistancePlan(
        plan_id=f"plan-{candidate_id}",
        candidate_id=candidate_id,
        task_slice=task,
        required_intrinsic_capabilities=("tools",),
        required_context_slots=("evidence",),
        required_context_evidence=("proof",),
        required_tool_capabilities=("pytest",),
        selected_tool_surface=("pytest",),
        decomposition=DecompositionRequirement(
            required=False,
            child_slices=(),
            preserves_parent_acceptance=True,
            preserves_parent_proof=True,
            reason="",
            planner_role="",
        ),
        verification=VerificationStrategy(kind="pytest", proof_criteria=PROOF),
        assistance_cost=cost,
        result="sufficient",
        reasons=("intrinsic_ok",),
        intrinsic_sufficient=True,
        assisted_sufficient=True,
        assistance_delta=(),
        provenance=(
            ProvenanceClaim(
                kind="candidate",
                claim=candidate_id,
                source="fixture",
                digest="sha256:plan67",
                observed_at=NOW.isoformat(),
                freshness="fresh",
                fresh=True,
            ),
        ),
        evidence_digest="sha256:plan67",
    )


def _cost(
    strategy_id: str, *, assistance: AssistanceCost, is_free: bool, trajectory_id: str = "traj-67"
) -> ExpectedStrategyCost:
    return build_strategy_from_assistance(
        strategy_id=strategy_id,
        trajectory_id=trajectory_id,
        assistance=assistance,
        execution_tokens=8_000,
        price=PRICE,
        is_free=is_free,
        now=NOW,
    )


def _offer(
    *,
    strategy: str,
    route: ConcreteRoute,
    plan: AssistancePlan,
    is_cheap: bool = False,
    is_frontier: bool = False,
    is_paid: bool = False,
    trajectory_id: str = "traj-67",
) -> ExecutionPathOffer:
    return ExecutionPathOffer(
        strategy=strategy,  # type: ignore[arg-type]
        route=route,
        assistance_plan=plan,
        expected_cost=_cost(
            f"{strategy}:{route.route_id}",
            assistance=plan.assistance_cost,
            is_free=is_cheap and not is_paid,
            trajectory_id=trajectory_id,
        ),
        certification_state=CertificationState.READY,
        certification_freshness="fresh",
        is_cheap=is_cheap,
        is_frontier=is_frontier,
        is_paid=is_paid,
    )


def _ep_free_over_premium():
    free = _route("free-1", model="openrouter/free-coder", pool="pool-free", tier=1)
    premium = _route(
        "premium-1", model="anthropic/claude-opus", provider="anthropic", pool="pool-paid", tier=3
    )
    free_plan = _plan(candidate_id="free-1")
    premium_plan = _plan(candidate_id="premium-1")
    return optimize_execution_path(
        ExecutionPathRequest(
            task_slice=_slice(),
            trajectory_id="traj-67",
            offers=(
                _offer(strategy="direct_cheap", route=free, plan=free_plan, is_cheap=True),
                _offer(strategy="direct_paid", route=premium, plan=premium_plan, is_paid=True),
            ),
            now=NOW,
        )
    )


def _ep_frontier_review():
    frontier = _route(
        "frontier-1",
        model="evidence-driven/frontier-planner",
        provider="frontier-provider",
        pool="pool-frontier",
        tier=3,
    )
    task = _slice(objective="architecture review of module boundaries")
    return optimize_execution_path(
        ExecutionPathRequest(
            task_slice=task,
            trajectory_id="traj-67-review",
            offers=(
                _offer(
                    strategy="frontier_direct",
                    route=frontier,
                    plan=_plan(candidate_id="frontier-1", slice_=task),
                    is_frontier=True,
                    trajectory_id="traj-67-review",
                ),
            ),
            now=NOW,
        )
    )


def _candidate(
    runtime_id: str,
    *,
    model: str | None = None,
    availability: str = "ready",
    cost: float = 0.0,
    **kwargs: Any,
) -> RuntimeCandidate:
    return RuntimeCandidate(
        runtime_id=runtime_id,
        catalog_present=kwargs.pop("catalog_present", True),
        live_eligible=kwargs.pop("live_eligible", True),
        availability=availability,
        signals={"cost_usd": {"value": cost}, **kwargs.pop("signals", {})},
        capabilities=list(kwargs.pop("capabilities", ["tools", "pytest"])),
        provider=kwargs.pop("provider", "provider-a"),
        model=model or runtime_id,
    )


def _snapshot(*candidates: RuntimeCandidate) -> AvailabilitySnapshot:
    return AvailabilitySnapshot(
        observed_at=NOW.isoformat(), state="ready", ttl_seconds=60, candidates=list(candidates)
    )


def _unit(
    unit_id: str, content: str, *, slot_type: str = "evidence", key: str | None = None
) -> ContextUnit:
    return ContextUnit(
        unit_id=unit_id,
        slot_type=slot_type,  # type: ignore[arg-type]
        key=key or unit_id,
        content=content,
        source_uri=f"urn:fixture:{unit_id}",
        source_digest=_digest(content),
        revision="r1",
        observed_at="2026-09-19T00:00:00Z",
        retrieved_at="2026-09-19T00:01:00Z",
        trust="fixture",
        authority="observed",
        sensitivity="public",
        tenant_scope="default",
        project_scope="default",
    )


def test_free_healthy_bound_when_ep_selects_cheap() -> None:
    decision = _ep_free_over_premium()
    assert decision.selected_route is not None
    assert decision.selected_strategy == "direct_cheap"

    snap = _snapshot(
        _candidate("free-1", model="openrouter/free-coder", cost=0.0),
        _candidate("premium-1", model="anthropic/claude-opus", cost=2.0),
    )
    hydrated = hydrate_worker_context(
        role="implementer",
        objective="implement feature",
        acceptance_criteria=("tests pass",),
        proof_criteria=PROOF,
        units=(_unit("proof", f"Proof: {PROOF[0]}"),),
        token_budget=500,
    )
    result, receipt = execute_optimized_dispatch(
        decision=decision,
        snapshot=snap,
        task_class="implementation",
        hydrated=hydrated,
        dry_run=True,
    )

    assert result.selected is not None
    assert result.selected.runtime_id == "free-1"
    assert result.selected.model == decision.selected_route.model
    assert isinstance(receipt, DispatchReceipt)
    assert receipt.chosen_model == decision.selected_route.model
    assert receipt.chosen_provider == decision.selected_route.provider
    assert receipt.chosen_pool == decision.selected_route.credential_pool
    assert receipt.strategy_authority == STRATEGY_AUTHORITY
    assert receipt.strategy_authority == "execution_path.optimize_execution_path"
    assert receipt.decision_digest == decision.decision_digest
    assert receipt.context_pack_digest == hydrated.pack_digest
    assert receipt.task_class == "implementation"
    assert receipt.dry_run is True
    assert "premium-1" in receipt.candidates or "anthropic/claude-opus" in receipt.candidates


def test_free_unhealthy_skipped_with_named_reason() -> None:
    decision = _ep_free_over_premium()
    snap = _snapshot(
        _candidate("free-1", model="openrouter/free-coder", availability="unhealthy"),
        _candidate("premium-1", model="anthropic/claude-opus", cost=2.0),
    )
    hydrated = hydrate_worker_context(
        role="implementer",
        objective="implement feature",
        acceptance_criteria=("tests pass",),
        proof_criteria=PROOF,
        units=(_unit("proof", f"Proof: {PROOF[0]}"),),
        token_budget=500,
    )

    with pytest.raises((OptimizedDispatchError, Exception)) as exc_info:
        execute_optimized_dispatch(
            decision=decision,
            snapshot=snap,
            task_class="implementation",
            hydrated=hydrated,
            dry_run=True,
        )

    message = str(exc_info.value).lower()
    assert "unhealthy" in message or "no candidate" in message or "ineligible" in message


def test_premium_carve_out_binds_frontier_from_ep_evidence() -> None:
    decision = _ep_frontier_review()
    assert decision.selected_route is not None
    # Identity must come from EP evidence, not a hard-coded model string in dispatch.
    expected_model = decision.selected_route.model
    assert expected_model == "evidence-driven/frontier-planner"

    snap = _snapshot(
        _candidate("frontier-1", model=expected_model, provider="frontier-provider", cost=3.0),
        _candidate("cheap-skip", model="openrouter/free-coder", cost=0.0),
    )
    hydrated = hydrate_worker_context(
        role="reviewer",
        objective="architecture review",
        acceptance_criteria=("design sound",),
        proof_criteria=PROOF,
        units=(_unit("proof", f"Proof: {PROOF[0]}"),),
        token_budget=500,
    )
    result, receipt = execute_optimized_dispatch(
        decision=decision, snapshot=snap, task_class="architecture", hydrated=hydrated, dry_run=True
    )

    assert result.selected is not None
    assert result.selected.model == expected_model
    assert receipt.chosen_model == expected_model
    assert receipt.chosen_model == decision.selected_route.model
    assert receipt.task_class == "architecture"
    assert decision.selected_strategy in {"frontier_direct", "frontier_plan_then_cheap_execute"}


def test_missing_explicit_child_model_rejected() -> None:
    decision = _ep_free_over_premium()
    snap = _snapshot(_candidate("free-1", model="openrouter/free-coder"))
    hydrated = hydrate_worker_context(
        role="implementer",
        objective="child slice",
        acceptance_criteria=("tests pass",),
        proof_criteria=PROOF,
        units=(_unit("proof", f"Proof: {PROOF[0]}"),),
        token_budget=200,
    )

    with pytest.raises(OptimizedDispatchError, match="explicit"):
        execute_optimized_dispatch(
            decision=decision,
            snapshot=snap,
            task_class="implementation",
            hydrated=hydrated,
            is_child=True,
            explicit_model=None,
            dry_run=True,
        )

    with pytest.raises(OptimizedDispatchError, match="explicit"):
        execute_optimized_dispatch(
            decision=decision,
            snapshot=snap,
            task_class="implementation",
            hydrated=hydrated,
            is_child=True,
            explicit_model="",
            dry_run=True,
        )


def test_hydrate_retains_proof_under_tight_budget_and_records_omissions() -> None:
    bulky = "x" * 4000
    units = (
        _unit("bulky-docs", bulky, slot_type="instructions"),
        _unit("bulky-history", bulky, slot_type="history"),
        _unit("proof-core", f"REQUIRED PROOF: {PROOF[0]}; also {PROOF[1]}", slot_type="evidence"),
        _unit("optional-adr", "nice-to-have ADR detail " + ("y" * 500), slot_type="memory"),
    )
    pack = hydrate_worker_context(
        role="implementer",
        objective="ship slice",
        acceptance_criteria=("tests pass", "docs updated"),
        proof_criteria=PROOF,
        units=units,
        token_budget=80,
        session_history=("raw turn 1 " * 200, "raw turn 2 " * 200),
    )

    assert isinstance(pack, HydratedWorkerPack)
    assert pack.role == "implementer"
    assert pack.proof_criteria_retained
    for criterion in PROOF:
        assert any(criterion in retained for retained in pack.proof_criteria_retained) or (
            criterion in pack.pack.compiled_prompt
        )
    assert any(c in pack.pack.compiled_prompt for c in PROOF)
    assert pack.omissions, "tight budget must record omissions"
    # Must not dump raw session history wholesale into the pack.
    assert "raw turn 1 raw turn 1" not in pack.pack.compiled_prompt


def test_hydrate_cache_reuse_and_invalidation() -> None:
    cache: dict[str, Any] = {}
    digests = {
        "code": _digest("code-v1"),
        "spec": _digest("spec-v1"),
        "dependency": _digest("deps-v1"),
    }
    units = (_unit("proof", f"Proof: {PROOF[0]}"),)

    first = hydrate_worker_context(
        role="implementer",
        objective="cache me",
        acceptance_criteria=("ok",),
        proof_criteria=PROOF,
        units=units,
        token_budget=200,
        input_digests=digests,
        cache=cache,
    )
    second = hydrate_worker_context(
        role="implementer",
        objective="cache me",
        acceptance_criteria=("ok",),
        proof_criteria=PROOF,
        units=units,
        token_budget=200,
        input_digests=digests,
        cache=cache,
    )
    assert first.cache_hit is False
    assert second.cache_hit is True
    assert first.pack_digest == second.pack_digest

    changed = {**digests, "code": _digest("code-v2")}
    third = hydrate_worker_context(
        role="implementer",
        objective="cache me",
        acceptance_criteria=("ok",),
        proof_criteria=PROOF,
        units=units,
        token_budget=200,
        input_digests=changed,
        cache=cache,
    )
    assert third.cache_hit is False
    assert third.pack_digest != first.pack_digest or third.input_digest != first.input_digest


def test_secrets_rejected_from_packs_and_receipts() -> None:
    with pytest.raises(OptimizedDispatchError, match="secret"):
        hydrate_worker_context(
            role="implementer",
            objective="leak attempt",
            acceptance_criteria=("ok",),
            proof_criteria=PROOF,
            units=(_unit("leak", "api_key=sk-super-secret-do-not-store"),),
            token_budget=200,
        )


def test_worker_return_contract_rejects_incomplete() -> None:
    incomplete = WorkerReturnContract(
        changed_files=(),
        commit=None,
        tests=(),
        commands=(),
        results={},
        fulfilled_ac=(),
        unfulfilled_ac=(),
        evidence=(),
        uncertainty=(),
    )
    with pytest.raises(OptimizedDispatchError):
        validate_worker_return(incomplete)

    complete = WorkerReturnContract(
        changed_files=("verdict/optimized_dispatch.py",),
        commit="abc123",
        tests=("tests/test_optimized_dispatch.py",),
        commands=("pytest tests/test_optimized_dispatch.py",),
        results={"pytest": "passed"},
        fulfilled_ac=("tests pass",),
        unfulfilled_ac=(),
        evidence=("receipt digest ok",),
        uncertainty=("none material",),
    )
    validate_worker_return(complete)


def test_execute_builds_decision_only_via_optimize_execution_path() -> None:
    free = _route("free-1", model="openrouter/free-coder")
    plan = _plan(candidate_id="free-1")
    request = ExecutionPathRequest(
        task_slice=_slice(),
        trajectory_id="traj-67-build",
        offers=(
            _offer(
                strategy="direct_cheap",
                route=free,
                plan=plan,
                is_cheap=True,
                trajectory_id="traj-67-build",
            ),
        ),
        now=NOW,
    )
    snap = _snapshot(_candidate("free-1", model="openrouter/free-coder"))
    hydrated = hydrate_worker_context(
        role="implementer",
        objective="build via request",
        acceptance_criteria=("ok",),
        proof_criteria=PROOF,
        units=(_unit("proof", f"Proof: {PROOF[0]}"),),
        token_budget=200,
    )
    result, receipt = execute_optimized_dispatch(
        execution_path_request=request,
        snapshot=snap,
        task_class="implementation",
        hydrated=hydrated,
        dry_run=True,
        dispatcher=SwarmDispatcher(),
    )
    assert result.selected is not None
    assert receipt.strategy_authority == STRATEGY_AUTHORITY
    assert receipt.decision_digest


def test_execute_requires_decision_or_request() -> None:
    snap = _snapshot(_candidate("free-1", model="openrouter/free-coder"))
    with pytest.raises(OptimizedDispatchError):
        execute_optimized_dispatch(snapshot=snap, task_class="implementation", dry_run=True)
