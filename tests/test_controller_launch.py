"""BOD-156 controller launch contract, decision, and identity tests."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from verdict.controller_launch import (
    ControllerLaunchDecision,
    ControllerLaunchError,
    ControllerMission,
    ObservedControllerIdentity,
    OperatorOverride,
    PersistedAuthoritativeDecision,
    PrimeLaunchTarget,
    build_prime_argv,
    compute_decision_digest,
    decide_controller_launch,
    fence_owned_root_plan,
    observe_owned_controller_identity,
    validate_controller_decision,
    verify_observed_controller_identity,
)
from verdict.controller_selection import ControllerSelectionHooks, select_controller_launch
from verdict.cost_ledger import PriceEvidenceInput
from verdict.effective_capability import (
    AssistanceCost,
    AssistancePlan,
    DecompositionRequirement,
    ProvenanceClaim,
    TaskSlice,
    VerificationStrategy,
)
from verdict.execution_path import ExecutionPathOffer, ExecutionPathRequest, optimize_execution_path
from verdict.expected_cost import build_strategy_from_assistance
from verdict.runtime_certification import CertificationState
from verdict.session_economics import (
    ConcreteRoute,
    CostState,
    PromptCacheState,
    SessionState,
    TaskState,
    decide_session_route,
)

NOW = datetime(2026, 9, 22, 12, 0, 0, tzinfo=timezone.utc)
EXPIRY = (NOW + timedelta(hours=1)).isoformat()


def _mission() -> ControllerMission:
    return ControllerMission(
        mission_id="mission-1",
        story_id="story-1",
        attempt_id="attempt-1",
        objective="bootstrap controller",
        durable_context_refs=("checkpoint://a",),
    )


def _target(*, reasoning: str | None = "high") -> PrimeLaunchTarget:
    return PrimeLaunchTarget(
        upstream_provider="omniroute",
        upstream_model="gc/grok-4.5",
        prime_provider="omniroute",
        prime_model="gc/grok-4.5",
        binding_digest="binding-digest-1",
        reasoning_effort=reasoning,
        binding_evidence_ref="evidence://binding-1",
    )


def _persisted(
    *, reasoning: str | None = "high", receipt: str = "receipt://pre-1"
) -> PersistedAuthoritativeDecision:
    return PersistedAuthoritativeDecision(
        execution_path_decision_digest="bod104-digest-1",
        selected_upstream_route="omniroute/gc/grok-4.5",
        prime_target=_target(reasoning=reasoning),
        context_plan_digest="ctx-plan-1",
        context_pack_digest="ctx-pack-1",
        context_receipt_digest="ctx-receipt-1",
        selected_prompt_digest="prompt-1",
        routing_receipt_ref=receipt,
        pool_ref="pool://live-1",
        evidence_refs=("evidence://live", "evidence://passport"),
        constituent_freshness={"inventory": NOW.isoformat()},
        minimum_expiry=EXPIRY,
        session_decision="NEW",
        why_selected="complete-cost winner",
        task_profile_digest="tp-1",
        task_slice_digest="ts-1",
        trajectory_digest="tr-1",
    )


def test_operator_override_requires_both_and_cli_provenance() -> None:
    ov = OperatorOverride(
        provider="omniroute",
        model="gc/grok-4.5",
        source="cli",
        reason="operator pin",
        timestamp=NOW.isoformat(),
    )
    assert ov.provider == "omniroute"
    with pytest.raises(ControllerLaunchError) as exc:
        OperatorOverride(
            provider="auto/best-coding",
            model="gc/grok-4.5",
            source="cli",
            reason="bad",
            timestamp=NOW.isoformat(),
        )
    assert exc.value.reason_code == "forbidden_identity"
    with pytest.raises(ControllerLaunchError) as exc2:
        OperatorOverride(
            provider="omniroute",
            model="gc/grok-4.5",
            source="repo",
            reason="forged",
            timestamp=NOW.isoformat(),
        )
    assert exc2.value.reason_code == "untrusted_override_source"


def test_prime_target_rejects_auto() -> None:
    with pytest.raises(ControllerLaunchError) as exc:
        PrimeLaunchTarget(
            upstream_provider="omniroute",
            upstream_model="auto/best",
            prime_provider="omniroute",
            prime_model="gc/grok-4.5",
            binding_digest="x",
        )
    assert exc.value.reason_code == "forbidden_identity"


def test_automatic_mode_requires_persisted_bod104_and_receipt() -> None:
    mission = _mission()
    with pytest.raises(ControllerLaunchError) as exc:
        decide_controller_launch(mission, persisted=None, override=None, now=NOW)
    assert exc.value.reason_code == "missing_authoritative_decision"

    decision = decide_controller_launch(mission, persisted=_persisted(), now=NOW)
    assert decision.mode == "automatic"
    assert decision.routing_receipt_ref == "receipt://pre-1"
    assert decision.execution_path_decision_digest == "bod104-digest-1"
    assert decision.canonical_digest == compute_decision_digest(decision.to_dict())


def test_decide_controller_launch_without_persisted_still_refuses_to_invent() -> None:
    """Assembly helper still requires persisted authority; live selection is separate."""
    mission = _mission()
    with pytest.raises(ControllerLaunchError) as exc:
        decide_controller_launch(mission, now=NOW)
    assert exc.value.reason_code == "missing_authoritative_decision"


def test_validate_rejects_stale_and_digest_mismatch() -> None:
    mission = _mission()
    decision = decide_controller_launch(mission, persisted=_persisted(), now=NOW)
    validate_controller_decision(decision, mission=mission, attempt_id="attempt-1", now=NOW)

    with pytest.raises(ControllerLaunchError) as stale:
        validate_controller_decision(
            decision, mission=mission, attempt_id="attempt-1", now=NOW + timedelta(hours=2)
        )
    assert stale.value.reason_code == "stale_decision"

    bad = ControllerLaunchDecision(
        **{
            **decision.to_dict(),
            "prime_target": decision.prime_target,
            "evidence_refs": decision.evidence_refs,
            "canonical_digest": "deadbeef",
            "constituent_freshness": dict(decision.constituent_freshness),
            "override_provenance": decision.override_provenance,
        }
    )
    with pytest.raises(ControllerLaunchError) as dig:
        validate_controller_decision(bad, mission=mission, attempt_id="attempt-1", now=NOW)
    assert dig.value.reason_code == "digest_mismatch"


def test_override_mode_records_provenance_and_requires_eligible_target() -> None:
    mission = _mission()
    ov = OperatorOverride(
        provider="omniroute",
        model="gc/grok-4.5",
        source="cli",
        reason="pin for proof",
        timestamp=NOW.isoformat(),
        reasoning_effort="high",
    )
    with pytest.raises(ControllerLaunchError) as exc:
        decide_controller_launch(mission, override=ov, persisted=None, now=NOW)
    assert exc.value.reason_code == "override_requires_eligible_target"

    decision = decide_controller_launch(mission, override=ov, persisted=_persisted(), now=NOW)
    assert decision.mode == "override"
    assert decision.override_provenance is not None
    assert decision.override_provenance["source"] == "cli"
    assert decision.override_provenance["provider"] == "omniroute"
    assert decision.override_provenance["model"] == "gc/grok-4.5"


def test_override_mismatch_against_persisted_target_fails() -> None:
    mission = _mission()
    ov = OperatorOverride(
        provider="omniroute",
        model="other/model",
        source="cli",
        reason="wrong pin",
        timestamp=NOW.isoformat(),
    )
    with pytest.raises(ControllerLaunchError) as exc:
        decide_controller_launch(mission, override=ov, persisted=_persisted(), now=NOW)
    assert exc.value.reason_code == "override_target_mismatch"


def test_build_prime_argv_exact_and_omits_unsupported_thinking() -> None:
    with_thinking = _target(reasoning="high")
    argv = build_prime_argv(
        prime="prime-agent",
        target=with_thinking,
        session_dir="/tmp/attempt/session",
        prompt="do the work",
    )
    assert argv == [
        "prime-agent",
        "--provider",
        "omniroute",
        "--model",
        "gc/grok-4.5",
        "--session-dir",
        "/tmp/attempt/session",
        "--thinking",
        "high",
        "do the work",
    ]

    no_thinking = _target(reasoning=None)
    argv2 = build_prime_argv(
        prime="prime-agent",
        target=no_thinking,
        session_dir="/tmp/attempt/session",
        prompt="do the work",
    )
    assert "--thinking" not in argv2
    assert argv2[argv2.index("--provider") + 1] == "omniroute"
    assert argv2[argv2.index("--model") + 1] == "gc/grok-4.5"


def test_observe_ignores_unrelated_drafts_and_requires_single_root(tmp_path: Path) -> None:
    session_dir = tmp_path / "attempt" / "session"
    session_dir.mkdir(parents=True)
    owned_file = session_dir / "root.json"
    owned_file.write_text("{}")
    other = tmp_path / "other" / "draft.json"
    other.parent.mkdir(parents=True)
    other.write_text("{}")

    roster = {
        "sessions": [
            {
                "id": "draft-1",
                "status": "draft",
                "sessionFile": str(other),
                "runtimeKind": "top-level",
                "rlmDepth": 0,
                "model": {"provider": "x", "id": "y", "thinkingLevel": "low"},
            },
            {
                "id": "root-1",
                "status": "running",
                "sessionFile": str(owned_file),
                "runtimeKind": "top-level",
                "rlmDepth": 0,
                "model": {"provider": "omniroute", "id": "gc/grok-4.5", "thinkingLevel": "high"},
            },
        ]
    }
    observed = observe_owned_controller_identity(roster, session_dir=session_dir, now=NOW)
    assert observed.session_id == "root-1"
    assert observed.provider == "omniroute"
    assert observed.model == "gc/grok-4.5"
    assert observed.thinking_level == "high"


def test_observe_missing_and_ambiguous_fail_closed(tmp_path: Path) -> None:
    session_dir = tmp_path / "attempt" / "session"
    session_dir.mkdir(parents=True)
    with pytest.raises(ControllerLaunchError) as missing:
        observe_owned_controller_identity({"sessions": []}, session_dir=session_dir, now=NOW)
    assert missing.value.reason_code == "missing_owned_root"

    f1 = session_dir / "a.json"
    f2 = session_dir / "b.json"
    f1.write_text("{}")
    f2.write_text("{}")
    roster = {
        "sessions": [
            {
                "id": "r1",
                "status": "running",
                "sessionFile": str(f1),
                "runtimeKind": "top-level",
                "rlmDepth": 0,
                "model": {"provider": "omniroute", "id": "gc/grok-4.5"},
            },
            {
                "id": "r2",
                "status": "running",
                "sessionFile": str(f2),
                "runtimeKind": "top-level",
                "rlmDepth": 0,
                "model": {"provider": "omniroute", "id": "gc/grok-4.5"},
            },
        ]
    }
    with pytest.raises(ControllerLaunchError) as amb:
        observe_owned_controller_identity(roster, session_dir=session_dir, now=NOW)
    assert amb.value.reason_code == "ambiguous_owned_root"


def test_observe_rejects_malformed_owned_live_identity(tmp_path: Path) -> None:
    session_dir = tmp_path / "attempt" / "session"
    session_dir.mkdir(parents=True)
    owned = session_dir / "root.json"
    owned.write_text("{}")
    roster = {
        "sessions": [
            {
                "id": "root-bad",
                "status": "running",
                "sessionFile": str(owned),
                "runtimeKind": "top-level",
                # missing rlmDepth / model
            }
        ]
    }
    with pytest.raises(ControllerLaunchError) as exc:
        observe_owned_controller_identity(roster, session_dir=session_dir, now=NOW)
    assert exc.value.reason_code == "malformed_owned_identity"


def test_verify_observed_identity_exact_match_and_mismatch(tmp_path: Path) -> None:
    mission = _mission()
    decision = decide_controller_launch(mission, persisted=_persisted(), now=NOW)
    session_dir = tmp_path / "attempt" / "session"
    session_dir.mkdir(parents=True)
    session_file = session_dir / "root.json"
    session_file.write_text("{}")

    good = ObservedControllerIdentity(
        session_id="root-1",
        session_file=str(session_file),
        runtime_kind="top-level",
        rlm_depth=0,
        provider="omniroute",
        model="gc/grok-4.5",
        thinking_level="high",
        observed_at=NOW.isoformat(),
    )
    assert verify_observed_controller_identity(decision, good, session_dir=session_dir) is good

    bad_model = ObservedControllerIdentity(
        session_id="root-1",
        session_file=str(session_file),
        runtime_kind="top-level",
        rlm_depth=0,
        provider="omniroute",
        model="wrong/model",
        thinking_level="high",
        observed_at=NOW.isoformat(),
    )
    with pytest.raises(ControllerLaunchError) as exc:
        verify_observed_controller_identity(decision, bad_model, session_dir=session_dir)
    assert exc.value.reason_code == "identity_mismatch"

    # Fence plan never stops unrelated sessions
    plan = fence_owned_root_plan(
        session_dir=session_dir, observed=bad_model, reason_code="identity_mismatch"
    )
    assert plan["stop_unrelated"] is False
    assert plan["confirm_absence"] is True


def test_verify_thinking_omission_when_unsupported(tmp_path: Path) -> None:
    mission = _mission()
    decision = decide_controller_launch(mission, persisted=_persisted(reasoning=None), now=NOW)
    session_dir = tmp_path / "s"
    session_dir.mkdir()
    session_file = session_dir / "root.json"
    session_file.write_text("{}")

    ok = ObservedControllerIdentity(
        session_id="root-1",
        session_file=str(session_file),
        runtime_kind="top-level",
        rlm_depth=0,
        provider="omniroute",
        model="gc/grok-4.5",
        thinking_level=None,
        observed_at=NOW.isoformat(),
    )
    verify_observed_controller_identity(decision, ok, session_dir=session_dir)

    assumed = ObservedControllerIdentity(
        session_id="root-1",
        session_file=str(session_file),
        runtime_kind="top-level",
        rlm_depth=0,
        provider="omniroute",
        model="gc/grok-4.5",
        thinking_level="medium",
        observed_at=NOW.isoformat(),
    )
    with pytest.raises(ControllerLaunchError) as exc:
        verify_observed_controller_identity(decision, assumed, session_dir=session_dir)
    assert exc.value.reason_code == "identity_mismatch"


def test_session_boundary_fence(tmp_path: Path) -> None:
    mission = _mission()
    decision = decide_controller_launch(mission, persisted=_persisted(), now=NOW)
    session_dir = tmp_path / "owned"
    session_dir.mkdir()
    outsider = tmp_path / "other" / "root.json"
    outsider.parent.mkdir()
    outsider.write_text("{}")
    observed = ObservedControllerIdentity(
        session_id="root-x",
        session_file=str(outsider),
        runtime_kind="top-level",
        rlm_depth=0,
        provider="omniroute",
        model="gc/grok-4.5",
        thinking_level="high",
        observed_at=NOW.isoformat(),
    )
    with pytest.raises(ControllerLaunchError) as exc:
        verify_observed_controller_identity(decision, observed, session_dir=session_dir)
    assert exc.value.reason_code == "session_boundary_violation"


def test_persisted_rejects_auto_route() -> None:
    with pytest.raises(ControllerLaunchError) as exc:
        PersistedAuthoritativeDecision(
            execution_path_decision_digest="bod104",
            selected_upstream_route="auto/best-coding",
            prime_target=_target(),
            context_plan_digest="a",
            context_pack_digest="b",
            context_receipt_digest="c",
            selected_prompt_digest="d",
            routing_receipt_ref="receipt://x",
            pool_ref="pool",
            evidence_refs=("e1",),
            constituent_freshness={},
            minimum_expiry=EXPIRY,
            session_decision="NEW",
            why_selected="nope",
            task_profile_digest="t",
            task_slice_digest="s",
            trajectory_digest="r",
        )
    assert exc.value.reason_code == "forbidden_identity"


# ---------------------------------------------------------------------------
# Live selector: select_controller_launch (BOD-156)
# ---------------------------------------------------------------------------

PRICE: PriceEvidenceInput = {"input_usd_per_mtok": "1.00", "evidence_id": "price-ctrl"}


def _ctrl_route(route_id: str, *, provider: str, model: str, tier: int = 2) -> ConcreteRoute:
    return ConcreteRoute(
        route_id=route_id,
        gateway="omniroute",
        provider=provider,
        model=model,
        credential_pool="pool-a",
        capability_tier=tier,
        eligible=True,
        excluded=False,
    )


def _ctrl_plan(
    candidate_id: str, *, ctx_digest: str, mission: ControllerMission | None = None
) -> AssistancePlan:
    m = mission or _mission()
    return AssistancePlan(
        plan_id=f"plan-{candidate_id}",
        candidate_id=candidate_id,
        task_slice=TaskSlice(
            slice_id=m.attempt_id,
            objective=m.objective,
            acceptance_criteria=(m.objective,),
            proof_criteria=(m.proof_burden or "controller-launch-proof",),
        ),
        required_intrinsic_capabilities=("tools",),
        required_context_slots=("evidence",),
        required_context_evidence=("proof",),
        required_tool_capabilities=("pytest",),
        selected_tool_surface=("pytest",),
        decomposition=DecompositionRequirement(required=False),
        verification=VerificationStrategy(
            kind="pytest", proof_criteria=(m.proof_burden or "controller-launch-proof",)
        ),
        assistance_cost=AssistanceCost(
            context_tokens=100, tool_tokens=10, planning_tokens=0, verification_tokens=50
        ),
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
                digest=f"sha256:{candidate_id}",
                observed_at=NOW.isoformat(),
                freshness="fresh",
                fresh=True,
            ),
        ),
        evidence_digest=f"sha256:{candidate_id}",
        context_plan_requirements={
            "plan_id": f"prehydrate:{candidate_id}",
            "digest": ctx_digest,
            "candidate_id": candidate_id,
            "estimated_fit": True,
        },
    )


def _ctrl_offer(
    route: ConcreteRoute, *, ctx_digest: str, execution_tokens: int = 8_000, is_free: bool = True
) -> ExecutionPathOffer:
    plan = _ctrl_plan(route.route_id, ctx_digest=ctx_digest)
    return ExecutionPathOffer(
        strategy="direct_cheap",
        route=route,
        assistance_plan=plan,
        expected_cost=build_strategy_from_assistance(
            strategy_id=f"direct_cheap:{route.route_id}",
            trajectory_id="traj-ctrl",
            assistance=plan.assistance_cost,
            execution_tokens=execution_tokens,
            price=PRICE,
            is_free=is_free,
            now=NOW,
        ),
        certification_state=CertificationState.READY,
        certification_freshness="fresh",
        is_cheap=True,
    )


def _cost_state(current: ConcreteRoute, fresh: ConcreteRoute, when: datetime) -> CostState:
    stay = build_strategy_from_assistance(
        strategy_id=f"stay:{current.route_id}",
        trajectory_id="traj-ctrl",
        assistance=AssistanceCost(verification_tokens=10),
        execution_tokens=1_000,
        price=PRICE,
        is_free=True,
        now=when,
    )
    switch = build_strategy_from_assistance(
        strategy_id=f"switch:{fresh.route_id}",
        trajectory_id="traj-ctrl",
        assistance=AssistanceCost(verification_tokens=10),
        execution_tokens=1_000,
        price=PRICE,
        is_free=True,
        now=when,
    )
    return CostState(stay_expected=stay, switch_expected=switch, now=when)


def _task_state(_mission: ControllerMission) -> TaskState:
    return TaskState(required_capability_tier=1)


class _FakeRecord:
    def __init__(self, receipt_id: str) -> None:
        self.receipt_id = receipt_id


def _hooks(
    *,
    seed: list[ExecutionPathOffer],
    prepare=None,
    optimize=None,
    exclude_ids: set[str] | None = None,
    context_digest_override: dict[str, str] | None = None,
):
    prepared_calls: list[ExecutionPathRequest] = []
    persist_calls: list[object] = []

    def seed_offers(mission: ControllerMission, when: datetime) -> list[ExecutionPathOffer]:
        assert mission.attempt_id
        assert when.tzinfo is not None
        return list(seed)

    def default_prepare(
        task: str, criticality: str, context: dict, request: ExecutionPathRequest
    ) -> ExecutionPathRequest:
        prepared_calls.append(request)
        offers = tuple(
            offer
            for offer in request.offers
            if exclude_ids is None or offer.route.route_id not in exclude_ids
        )
        # Simulate ContextPlan binding already present on seed offers.
        return replace(
            request,
            offers=offers,
            assumptions=tuple([*request.assumptions, "live_eligibility_applied"]),
        )

    def build_receipt(**kwargs):
        receipt = MagicMock()
        receipt.receipt_id = "rr-ctrl-1"
        receipt.kwargs = kwargs
        return receipt

    def persist_receipt(store, receipt):
        persist_calls.append(receipt)
        return _FakeRecord(getattr(receipt, "receipt_id", "rr-ctrl-1"))

    def compile_digests(decision, prepared):
        if context_digest_override is not None:
            payload = dict(context_digest_override)
        else:
            selected = decision.selected_candidate_id or "none"
            plan = f"ctx-plan:{selected}"
            for offer in prepared.offers:
                if offer.route.route_id == selected:
                    reqs = offer.assistance_plan.context_plan_requirements or {}
                    plan = str(reqs.get("digest") or plan)
                    break
            payload = {
                "context_plan_digest": plan,
                "context_pack_digest": f"ctx-pack:{plan}",
                "context_receipt_digest": f"ctx-receipt:{plan}",
                "selected_prompt_digest": f"prompt:{plan}",
            }
        # M4: digests alone are not authority; include stub objects for receipt.
        plan_digest = str(payload["context_plan_digest"])
        pack_digest = str(payload["context_pack_digest"])
        receipt_digest = str(payload["context_receipt_digest"])

        def _plan_to_dict(digest: str = plan_digest) -> dict:
            return {"digest": digest}

        def _empty_dict() -> dict:
            return {}

        payload.setdefault(
            "context_plan",
            SimpleNamespace(digest=plan_digest, plan_id=plan_digest, to_dict=_plan_to_dict),
        )
        payload.setdefault("context_pack", SimpleNamespace(digest=pack_digest, to_dict=_empty_dict))
        payload.setdefault(
            "context_receipt",
            SimpleNamespace(
                digest=receipt_digest,
                plan_digest=plan_digest,
                pack_digest=pack_digest,
                receipt_id=f"receipt:{plan_digest}",
                unresolved_uncertainties=(),
                to_dict=_empty_dict,
            ),
        )
        return payload

    def bind_prime_target(route: ConcreteRoute) -> PrimeLaunchTarget:
        provider = route.provider
        model = route.model
        return PrimeLaunchTarget(
            upstream_provider=provider,
            upstream_model=model,
            prime_provider=provider,
            prime_model=model,
            binding_digest=f"prime-bind:{route.route_id}:{provider}:{model}",
            reasoning_effort=None,
            binding_evidence_ref=f"test-map://{route.route_id}",
        )

    hooks = ControllerSelectionHooks(
        prepare_execution_request=prepare or default_prepare,
        seed_offers=seed_offers,
        optimize=optimize or optimize_execution_path,
        decide_session=decide_session_route,
        build_receipt=build_receipt,
        persist_receipt=persist_receipt,
        bind_prime_target=bind_prime_target,
        receipt_store=MagicMock(),
        cost_state_factory=_cost_state,
        task_state_factory=_task_state,
        compile_context_digests=compile_digests,
        decision_ttl=timedelta(hours=1),
    )
    return hooks, prepared_calls, persist_calls


def test_select_calls_live_eligibility_before_ranking() -> None:
    strong = _ctrl_offer(
        _ctrl_route("omniroute/gc/grok-4.5", provider="omniroute", model="gc/grok-4.5"),
        ctx_digest="ctx-plan-strong",
        execution_tokens=5_000,
    )
    weak = _ctrl_offer(
        _ctrl_route("omniroute/other/cheap", provider="omniroute", model="other/cheap"),
        ctx_digest="ctx-plan-weak",
        execution_tokens=50_000,
        is_free=False,
    )
    order: list[str] = []
    prepared_calls: list[ExecutionPathRequest] = []

    def prepare(task, criticality, context, request):
        order.append("prepare")
        prepared_calls.append(request)
        return replace(request, offers=request.offers)

    def optimize(request):
        order.append("optimize")
        return optimize_execution_path(request)

    hooks, _, persist_calls = _hooks(seed=[strong, weak], prepare=prepare, optimize=optimize)
    decision = select_controller_launch(_mission(), hooks=hooks, now=NOW)
    assert order == ["prepare", "optimize"]
    assert prepared_calls, "live eligibility prepare must be invoked"
    assert persist_calls, "routing receipt must be persisted before return"
    assert decision.mode == "automatic"
    assert decision.prime_target.prime_provider == "omniroute"
    assert decision.prime_target.prime_model == "gc/grok-4.5"
    assert decision.routing_receipt_ref == "receipt://rr-ctrl-1"
    assert decision.execution_path_decision_digest
    assert decision.context_plan_digest == "ctx-plan-strong"


def test_context_plan_digest_changes_selection_or_exclusion() -> None:
    keep = _ctrl_offer(
        _ctrl_route("omniroute/keep/model", provider="omniroute", model="keep/model"),
        ctx_digest="ctx-plan-keep",
    )
    drop = _ctrl_offer(
        _ctrl_route("omniroute/drop/model", provider="omniroute", model="drop/model"),
        ctx_digest="ctx-plan-drop",
        execution_tokens=1_000,
    )
    hooks, _, _ = _hooks(seed=[keep, drop], exclude_ids={"omniroute/drop/model"})
    decision = select_controller_launch(_mission(), hooks=hooks, now=NOW)
    assert decision.prime_target.prime_model == "keep/model"
    assert decision.context_plan_digest == "ctx-plan-keep"


def test_receipt_persisted_with_ep_and_context_digests() -> None:
    offer = _ctrl_offer(
        _ctrl_route("omniroute/gc/grok-4.5", provider="omniroute", model="gc/grok-4.5"),
        ctx_digest="ctx-plan-1",
    )
    hooks, _, persist_calls = _hooks(seed=[offer])
    decision = select_controller_launch(_mission(), hooks=hooks, now=NOW)
    assert persist_calls
    receipt = persist_calls[0]
    ext = receipt.kwargs["extensions"]["controller_launch"]
    assert ext["execution_path_decision_digest"] == decision.execution_path_decision_digest
    assert ext["context_plan_digest"] == decision.context_plan_digest
    assert ext["context_pack_digest"] == decision.context_pack_digest
    assert ext["context_receipt_digest"] == decision.context_receipt_digest


def test_session_stay_keeps_healthy_route() -> None:
    current = _ctrl_route("omniroute/current/model", provider="omniroute", model="current/model")
    fresh = _ctrl_route("omniroute/fresh/model", provider="omniroute", model="fresh/model")
    # Make fresh cheaper so BOD-104 would prefer it absent STAY.
    current_offer = _ctrl_offer(current, ctx_digest="ctx-current", execution_tokens=20_000)
    fresh_offer = _ctrl_offer(fresh, ctx_digest="ctx-fresh", execution_tokens=1_000)
    session = SessionState(
        session_id="sess-1",
        current_route=current,
        cache=PromptCacheState(
            warm=True,
            savings_usd=None,
            cached_tokens=100,
            fresh_until=NOW + timedelta(hours=1),
            status="observed",
        ),
        health_unusable=False,
        quota_exhausted=False,
    )
    hooks, _, _ = _hooks(seed=[current_offer, fresh_offer])
    decision = select_controller_launch(_mission(), hooks=hooks, session_state=session, now=NOW)
    assert decision.session_decision == "STAY"
    assert decision.prime_target.prime_model == "current/model"


def test_session_switch_replaces_ineligible_route() -> None:
    current = _ctrl_route("omniroute/old/model", provider="omniroute", model="old/model")
    # Mark current hard-ineligible via session flags.
    current_ineligible = ConcreteRoute(
        route_id=current.route_id,
        gateway=current.gateway,
        provider=current.provider,
        model=current.model,
        credential_pool=current.credential_pool,
        capability_tier=current.capability_tier,
        eligible=False,
        excluded=True,
        exclusion_reason="quota_exhausted",
    )
    fresh = _ctrl_route("omniroute/new/model", provider="omniroute", model="new/model")
    fresh_offer = _ctrl_offer(fresh, ctx_digest="ctx-new")
    session = SessionState(
        session_id="sess-2",
        current_route=current_ineligible,
        health_unusable=True,
        quota_exhausted=True,
    )
    hooks, _, _ = _hooks(seed=[fresh_offer])
    decision = select_controller_launch(_mission(), hooks=hooks, session_state=session, now=NOW)
    assert decision.session_decision == "SWITCH"
    assert decision.prime_target.prime_model == "new/model"


def test_blocked_when_no_eligible_route() -> None:
    offer = _ctrl_offer(
        _ctrl_route("omniroute/x/y", provider="omniroute", model="x/y"), ctx_digest="ctx-x"
    )
    hooks, _, persist_calls = _hooks(seed=[offer], exclude_ids={"omniroute/x/y"})
    with pytest.raises(ControllerLaunchError) as exc:
        select_controller_launch(_mission(), hooks=hooks, now=NOW)
    assert exc.value.reason_code == "no_eligible_route"
    assert persist_calls == []


def test_missing_snapshot_blocks() -> None:
    offer = _ctrl_offer(
        _ctrl_route("omniroute/gc/grok-4.5", provider="omniroute", model="gc/grok-4.5"),
        ctx_digest="ctx-plan-1",
    )

    def prepare(task, criticality, context, request):
        from verdict.execution_path import ExecutionPathError

        raise ExecutionPathError("missing live admit snapshot")

    hooks, _, _ = _hooks(seed=[offer], prepare=prepare)
    with pytest.raises(ControllerLaunchError) as exc:
        select_controller_launch(_mission(), hooks=hooks, now=NOW)
    assert exc.value.reason_code == "missing_snapshot"


def test_override_not_in_eligible_set_blocks() -> None:
    offer = _ctrl_offer(
        _ctrl_route("omniroute/gc/grok-4.5", provider="omniroute", model="gc/grok-4.5"),
        ctx_digest="ctx-plan-1",
    )
    hooks, _, _ = _hooks(seed=[offer])
    ov = OperatorOverride(
        provider="omniroute",
        model="other/not-eligible",
        source="cli",
        reason="pin",
        timestamp=NOW.isoformat(),
    )
    with pytest.raises(ControllerLaunchError) as exc:
        select_controller_launch(_mission(), hooks=hooks, override=ov, now=NOW)
    assert exc.value.reason_code == "override_not_eligible"


def test_auto_identity_rejected_in_seed_offers() -> None:
    bad_route = ConcreteRoute(
        route_id="auto/best-coding",
        gateway="omniroute",
        provider="auto",
        model="best-coding",
        credential_pool="pool-a",
        capability_tier=2,
        eligible=True,
        excluded=False,
    )
    # Build offer bypassing PrimeLaunchTarget checks; selector must still reject.
    plan = _ctrl_plan("auto/best-coding", ctx_digest="ctx-auto")
    bad_offer = ExecutionPathOffer(
        strategy="direct_cheap",
        route=bad_route,
        assistance_plan=plan,
        expected_cost=build_strategy_from_assistance(
            strategy_id="direct_cheap:auto/best-coding",
            trajectory_id="traj-ctrl",
            assistance=plan.assistance_cost,
            execution_tokens=1000,
            price=PRICE,
            is_free=True,
            now=NOW,
        ),
        certification_state=CertificationState.READY,
        certification_freshness="fresh",
        is_cheap=True,
    )
    hooks, _, _ = _hooks(seed=[bad_offer])
    with pytest.raises(ControllerLaunchError) as exc:
        select_controller_launch(_mission(), hooks=hooks, now=NOW)
    assert exc.value.reason_code == "forbidden_identity"


# ---------------------------------------------------------------------------
# Production factory (build_production_controller_selection_hooks)
# ---------------------------------------------------------------------------


def _factory_passport(model_id: str) -> object:
    from verdict.model_passports import ModelPassport

    return ModelPassport(
        provider=model_id.split("/", 1)[0],
        model_id=model_id,
        auth_state="authorized",
        availability_state="eligible",
        qualified_at=NOW - timedelta(minutes=1),
        last_verified_timestamp=NOW - timedelta(minutes=1),
        expires_at=NOW + timedelta(minutes=30),
        tool_support=True,
        token_cost_per_1k=0.001,
    )


def _factory_target(route_id: str, *, provider: str, model: str) -> PrimeLaunchTarget:
    return PrimeLaunchTarget(
        upstream_provider=provider,
        upstream_model=model,
        prime_provider=provider,
        prime_model=model,
        binding_digest=f"trusted-bind:{route_id}",
        reasoning_effort=None,
        binding_evidence_ref=f"map://{route_id}",
    )


def test_production_factory_fails_closed_without_prime_binding() -> None:
    from verdict.controller_selection import build_production_controller_selection_hooks

    with pytest.raises(ControllerLaunchError) as exc:
        build_production_controller_selection_hooks(
            prepare_execution_request=lambda *a, **k: a[-1],
            seed_offers=lambda mission, when: [],
            require_live_sources=False,
        )
    assert exc.value.reason_code == "missing_prime_binding"


def test_production_factory_fails_closed_without_live_passports(tmp_path: Path) -> None:
    from verdict.controller_selection import build_production_controller_selection_hooks

    missing = tmp_path / "no-passports.json"
    with pytest.raises(ControllerLaunchError) as exc:
        build_production_controller_selection_hooks(
            prepare_execution_request=lambda *a, **k: a[-1],
            prime_target_map={
                "omniroute/x/y": _factory_target("omniroute/x/y", provider="omniroute", model="x/y")
            },
            passport_store_path=missing,
            load_healthy_passports_fn=lambda path: {},
            load_metadata_fn=lambda path: object(),
            require_live_sources=True,
        )
    assert exc.value.reason_code == "missing_healthy_passports"


def test_production_factory_fails_closed_without_metadata() -> None:
    from verdict.controller_selection import build_production_controller_selection_hooks

    with pytest.raises(ControllerLaunchError) as exc:
        build_production_controller_selection_hooks(
            prepare_execution_request=lambda *a, **k: a[-1],
            prime_target_map={
                "omniroute/gc/grok-4.5": _factory_target(
                    "omniroute/gc/grok-4.5", provider="omniroute", model="gc/grok-4.5"
                )
            },
            healthy_passports={"omniroute/gc/grok-4.5": _factory_passport("omniroute/gc/grok-4.5")},
            load_metadata_fn=lambda path: (_ for _ in ()).throw(FileNotFoundError("no metadata")),
            require_live_sources=True,
        )
    assert exc.value.reason_code == "missing_metadata"


def test_production_factory_fails_closed_without_intelligence_service() -> None:
    from verdict.controller_selection import build_production_controller_selection_hooks

    with pytest.raises(ControllerLaunchError) as exc:
        build_production_controller_selection_hooks(
            prime_target_map={
                "omniroute/gc/grok-4.5": _factory_target(
                    "omniroute/gc/grok-4.5", provider="omniroute", model="gc/grok-4.5"
                )
            },
            healthy_passports={"omniroute/gc/grok-4.5": _factory_passport("omniroute/gc/grok-4.5")},
            metadata_snapshot=object(),
            require_live_sources=True,
        )
    assert exc.value.reason_code == "missing_intelligence_service"


def test_production_factory_injected_path_compiles_real_prompt_digest() -> None:
    from hashlib import sha256

    from verdict.context_pack import ContextPlan
    from verdict.controller_selection import (
        build_production_controller_selection_hooks,
        select_controller_launch,
    )

    route = _ctrl_route("omniroute/gc/grok-4.5", provider="omniroute", model="gc/grok-4.5")
    seed = [_ctrl_offer(route, ctx_digest="ignored-will-rebind")]
    prepare_calls: list[ExecutionPathRequest] = []

    def prepare(task, criticality, context, request):
        prepare_calls.append(request)
        assert request.offers, "live seed offers must reach prepare"
        # Bind a real ContextPlan onto the surviving offer (simulates BOD-143).
        plan = ContextPlan(
            plan_id="prehydrate:omniroute/gc/grok-4.5",
            candidate_id=route.route_id,
            token_budget=4096,
            output_token_reserve=256,
            tool_token_reserve=128,
            estimated_input_tokens=400,
            required_slot_types=("instructions", "state"),
            created_at=NOW.isoformat(),
        )
        rebound = []
        for offer in request.offers:
            rebound.append(
                replace(
                    offer,
                    assistance_plan=replace(
                        offer.assistance_plan, context_plan_requirements=plan.to_dict()
                    ),
                )
            )
        return replace(
            request,
            offers=tuple(rebound),
            assumptions=tuple([*request.assumptions, "live_eligibility_applied"]),
        )

    persist_calls: list[object] = []

    def build_receipt(**kwargs):
        receipt = MagicMock()
        receipt.receipt_id = "rr-prod-1"
        receipt.kwargs = kwargs
        return receipt

    def persist_receipt(store, receipt):
        persist_calls.append(receipt)
        return _FakeRecord("rr-prod-1")

    # Fake compiler that still generates real prompt bytes + sha256 digests.
    class _FakeCompiler:
        def compile_units(self, units, plan):
            from verdict.context_pack import ContextPack, ContextReceipt

            body = "\n".join(getattr(u, "content", str(u)) for u in units)
            prompt = f"CONTROLLER\nplan={plan.plan_id}\n{body}\n"
            pack = ContextPack(
                pack_id="pack-prod-1",
                compiled_prompt=prompt,
                used_tokens=max(1, len(prompt) // 4),
                token_budget=plan.token_budget,
                slots=(),
                conflicts=(),
                truncated_count=0,
                created_at=0.0,
                plan_id=plan.plan_id,
                plan_digest=plan.digest,
                candidate_id=plan.candidate_id,
                units=tuple(units),
                decisions=(),
                receipt_id="receipt:pack-prod-1",
            )
            # Ensure ContextReceipt.from_pack can read digests.
            assert ContextReceipt.from_pack(pack).pack_digest == pack.digest
            return pack

    target_map = {
        route.route_id: _factory_target(route.route_id, provider="omniroute", model="gc/grok-4.5")
    }

    bundle = build_production_controller_selection_hooks(
        prepare_execution_request=prepare,
        seed_offers=lambda mission, when: seed,
        prime_target_map=target_map,
        context_compiler=_FakeCompiler(),
        build_receipt=build_receipt,
        persist_receipt=persist_receipt,
        receipt_store=MagicMock(),
        cost_state_factory=_cost_state,
        task_state_factory=_task_state,
        require_live_sources=False,
    )
    decision = select_controller_launch(_mission(), hooks=bundle.hooks, now=NOW)

    assert prepare_calls, "prepare_controller path must be invoked"
    assert persist_calls, "receipt must persist before return"
    receipt_kwargs = persist_calls[0].kwargs
    assert receipt_kwargs.get("context_plan") is not None
    assert receipt_kwargs.get("context_pack") is not None
    assert receipt_kwargs.get("context_receipt") is not None
    assert decision.prime_target.binding_evidence_ref == f"map://{route.route_id}"
    assert decision.selected_prompt_digest.startswith("sha256:")
    expected_hex = sha256(
        bundle.artifacts.last_compiled.compiled_prompt.encode("utf-8")
    ).hexdigest()
    assert decision.selected_prompt_digest == f"sha256:{expected_hex}"
    assert bundle.artifacts.last_compiled is not None
    assert "CONTROLLER" in bundle.artifacts.last_compiled.compiled_prompt
    assert decision.context_plan_digest.startswith("sha256:")
    assert decision.context_pack_digest.startswith("sha256:")
    assert decision.context_receipt_digest.startswith("sha256:")


def test_production_factory_seed_offers_use_passport_price_evidence() -> None:
    from verdict.controller_selection import build_evidence_backed_seed_offers
    from verdict.runtime_certification import CertificationState

    identity = "omniroute/gc/grok-4.5"
    offers = build_evidence_backed_seed_offers(
        _mission(),
        NOW,
        healthy_passports={identity: _factory_passport(identity)},
        free_identity_ids=frozenset({identity}),
        certification_by_id={identity: (CertificationState.READY, "fresh")},
    )
    assert len(offers) == 1
    offer = offers[0]
    assert offer.route.route_id == identity
    assert offer.route.provider == "omniroute"
    assert offer.route.model == "gc/grok-4.5"  # leaf, not full identity
    assert offer.route.eligible is False  # M1: seeds are non-authority until prepare
    assert offer.certification_state is CertificationState.READY
    assert offer.certification_freshness == "fresh"
    # AssistancePlan must come from plan_effective_capability, not canned sufficiency.
    assert offer.assistance_plan.plan_id.startswith("ecp:")
    assert offer.assistance_plan.result == "sufficient"
    assert offer.assistance_plan.provenance
    assert any(
        getattr(p, "source", "") == "candidate_capability_evidence"
        for p in offer.assistance_plan.provenance
    )
    # Evidence-backed: expected cost terms exist (not empty fabricated offer).
    assert offer.expected_cost.terms


def test_expired_authorized_passport_still_seeds_offer() -> None:
    """Operational expiry must not remove a previously authorized passport."""
    from verdict.controller_selection import build_evidence_backed_seed_offers
    from verdict.model_passports import ModelPassport
    from verdict.runtime_certification import CertificationState

    identity = "omniroute/gc/grok-4.5"
    expired = ModelPassport(
        provider="omniroute",
        model_id=identity,
        auth_state="authorized",
        availability_state="eligible",
        qualified_at=NOW - timedelta(minutes=30),
        last_verified_timestamp=NOW - timedelta(minutes=30),
        expires_at=NOW - timedelta(minutes=5),
        tool_support=True,
        token_cost_per_1k=0.001,
    )
    denied = replace(expired, model_id="omniroute/denied", availability_state="denied")
    offers = build_evidence_backed_seed_offers(
        _mission(),
        NOW,
        healthy_passports={identity: expired, "omniroute/denied": denied},
        free_identity_ids=frozenset({identity, "omniroute/denied"}),
        certification_by_id={
            identity: (CertificationState.READY, "fresh"),
            "omniroute/denied": (CertificationState.READY, "fresh"),
        },
    )
    assert [offer.route.route_id for offer in offers] == [identity]


def test_select_fails_closed_without_context_compiler() -> None:
    offer = _ctrl_offer(
        _ctrl_route("omniroute/gc/grok-4.5", provider="omniroute", model="gc/grok-4.5"),
        ctx_digest="ctx-plan-1",
    )
    hooks, _, _ = _hooks(seed=[offer])
    hooks = replace(hooks, compile_context_digests=None)
    with pytest.raises(ControllerLaunchError) as exc:
        select_controller_launch(_mission(), hooks=hooks, now=NOW)
    assert exc.value.reason_code == "missing_context_compiler"


def test_select_fails_closed_without_prime_binding() -> None:
    offer = _ctrl_offer(
        _ctrl_route("omniroute/gc/grok-4.5", provider="omniroute", model="gc/grok-4.5"),
        ctx_digest="ctx-plan-1",
    )
    hooks, _, _ = _hooks(seed=[offer])
    hooks = replace(hooks, bind_prime_target=None)
    with pytest.raises(ControllerLaunchError) as exc:
        select_controller_launch(_mission(), hooks=hooks, now=NOW)
    assert exc.value.reason_code == "missing_prime_binding"


def test_seed_eligibility_alone_cannot_launch_without_prepare() -> None:
    """M1: inventory seeds with eligible=False cannot skip prepare authority."""
    from verdict.controller_selection import build_evidence_backed_seed_offers
    from verdict.runtime_certification import CertificationState

    identity = "omniroute/gc/grok-4.5"
    seeds = build_evidence_backed_seed_offers(
        _mission(),
        NOW,
        healthy_passports={identity: _factory_passport(identity)},
        free_identity_ids=frozenset({identity}),
        certification_by_id={identity: (CertificationState.READY, "fresh")},
    )
    assert seeds and seeds[0].route.eligible is False

    prepare_calls: list[ExecutionPathRequest] = []

    def prepare_drops_all(task, criticality, context, request):
        # Simulate live eligibility rejecting every inventory seed.
        prepare_calls.append(request)
        assert all(not offer.route.eligible for offer in request.offers)
        return replace(request, offers=())

    hooks, _, persist_calls = _hooks(seed=list(seeds), prepare=prepare_drops_all)
    with pytest.raises(ControllerLaunchError) as exc:
        select_controller_launch(_mission(), hooks=hooks, now=NOW)
    assert exc.value.reason_code == "no_eligible_route"
    assert prepare_calls, "prepare must still run"
    assert persist_calls == []


def test_passport_identity_normalizes_to_leaf_model() -> None:
    """M2: full identity_id becomes leaf ConcreteRoute.model; route_id may keep full id."""
    from verdict.controller_selection import build_evidence_backed_seed_offers
    from verdict.runtime_certification import CertificationState

    identity = "omniroute/gc/grok-4.5"
    # Passport stores full identity in model_id (prove_at_rest behavior).
    passport = _factory_passport(identity)
    assert passport.model_id == identity
    offers = build_evidence_backed_seed_offers(
        _mission(),
        NOW,
        healthy_passports={identity: passport},
        free_identity_ids=frozenset({identity}),
        certification_by_id={identity: (CertificationState.READY, "fresh")},
    )
    route = offers[0].route
    assert route.route_id == identity
    assert route.provider == "omniroute"
    assert route.model == "gc/grok-4.5"
    assert not route.model.startswith("omniroute/")


def test_bind_rejects_identity_shaped_prime_model() -> None:
    """M2: trusted bind validation rejects gateway-prefixed prime_model."""
    offer = _ctrl_offer(
        _ctrl_route("omniroute/gc/grok-4.5", provider="omniroute", model="gc/grok-4.5"),
        ctx_digest="ctx-plan-1",
    )

    def bad_bind(route: ConcreteRoute) -> PrimeLaunchTarget:
        return PrimeLaunchTarget(
            upstream_provider=route.provider,
            upstream_model=route.model,
            prime_provider=route.provider,
            # Identity-shaped model — forbidden.
            prime_model="omniroute/gc/grok-4.5",
            binding_digest="bad",
            reasoning_effort=None,
            binding_evidence_ref="map://bad",
        )

    hooks, _, _ = _hooks(seed=[offer])
    hooks = replace(hooks, bind_prime_target=bad_bind)
    with pytest.raises(ControllerLaunchError) as exc:
        select_controller_launch(_mission(), hooks=hooks, now=NOW)
    assert exc.value.reason_code == "identity_shaped_model"


def test_session_stay_mismatched_ep_route_fails_closed() -> None:
    """M3: never receipt-claim STAY while launching a different EP route."""
    current = _ctrl_route("omniroute/current/model", provider="omniroute", model="current/model")
    fresh = _ctrl_route("omniroute/fresh/model", provider="omniroute", model="fresh/model")
    current_offer = _ctrl_offer(current, ctx_digest="ctx-current", execution_tokens=20_000)
    fresh_offer = _ctrl_offer(fresh, ctx_digest="ctx-fresh", execution_tokens=1_000)
    session = SessionState(
        session_id="sess-mismatch",
        current_route=current,
        cache=PromptCacheState(
            warm=True,
            savings_usd=None,
            cached_tokens=100,
            fresh_until=NOW + timedelta(hours=1),
            status="observed",
        ),
        health_unusable=False,
        quota_exhausted=False,
    )

    def force_ep_switch(request: ExecutionPathRequest):
        # Pretend BOD-104 ignored authoritative STAY and picked fresh.
        from verdict.execution_path import ExecutionPathDecision

        return ExecutionPathDecision(
            selected_strategy="direct_cheap",
            selected_candidate_id=fresh.route_id,
            selected_route=fresh,
            rejected=(),
            assistance_plan_id="plan-forced",
            assistance_plan_digest="digest-forced",
            expected_cost=fresh_offer.expected_cost,
            expected_cost_terms=tuple(fresh_offer.expected_cost.terms),
            budget_state=None,
            tools_surface=(),
            session_decision=None
            if request.session_decision is None
            else request.session_decision.to_dict(),
            recovery_policy=None,
            verification_requirements=(),
            assumptions=("forced_ep_switch",),
            unknowns=(),
            freshness={"decision_at": NOW.isoformat()},
            evidence_digests={},
            why_selected="forced_for_mismatch_test",
            decision_digest="sha256:forced-mismatch",
            trajectory_id=request.trajectory_id,
            task_slice_id=request.task_slice.slice_id,
            strategy_selection_reason="forced",
        )

    hooks, _, _ = _hooks(seed=[current_offer, fresh_offer], optimize=force_ep_switch)
    with pytest.raises(ControllerLaunchError) as exc:
        select_controller_launch(_mission(), hooks=hooks, session_state=session, now=NOW)
    assert exc.value.reason_code == "session_ep_mismatch"


def test_production_factory_fails_closed_without_session_factories() -> None:
    """M5: None cost/task factories must fail at factory build, not later."""
    from verdict.controller_selection import build_production_controller_selection_hooks

    with pytest.raises(ControllerLaunchError) as exc:
        build_production_controller_selection_hooks(
            prepare_execution_request=lambda *a, **k: a[-1],
            seed_offers=lambda mission, when: [],
            prime_target_map={
                "omniroute/gc/grok-4.5": _factory_target(
                    "omniroute/gc/grok-4.5", provider="omniroute", model="gc/grok-4.5"
                )
            },
            require_live_sources=False,
        )
    assert exc.value.reason_code == "missing_session_hooks"


def test_receipt_build_receives_context_objects() -> None:
    """M4: build_routing_receipt receives plan/pack/receipt objects, not digests only."""
    offer = _ctrl_offer(
        _ctrl_route("omniroute/gc/grok-4.5", provider="omniroute", model="gc/grok-4.5"),
        ctx_digest="ctx-plan-objects",
    )
    hooks, _, persist_calls = _hooks(seed=[offer])
    select_controller_launch(_mission(), hooks=hooks, now=NOW)
    assert persist_calls
    kwargs = persist_calls[0].kwargs
    assert kwargs["context_plan"] is not None
    assert kwargs["context_pack"] is not None
    assert kwargs["context_receipt"] is not None
    assert getattr(kwargs["context_plan"], "digest", None)


def test_seed_offers_fail_closed_without_certification_evidence() -> None:
    """MF2: missing per-route certification must not default UNKNOWN or invent READY."""
    from verdict.controller_selection import build_evidence_backed_seed_offers

    identity = "omniroute/gc/grok-4.5"
    with pytest.raises(ControllerLaunchError) as missing_map:
        build_evidence_backed_seed_offers(
            _mission(),
            NOW,
            healthy_passports={identity: _factory_passport(identity)},
            free_identity_ids=frozenset({identity}),
            certification_by_id=None,
        )
    assert missing_map.value.reason_code == "missing_certification_evidence"

    with pytest.raises(ControllerLaunchError) as missing_route:
        build_evidence_backed_seed_offers(
            _mission(),
            NOW,
            healthy_passports={identity: _factory_passport(identity)},
            free_identity_ids=frozenset({identity}),
            certification_by_id={},  # empty map: no per-route entry
        )
    assert missing_route.value.reason_code == "missing_certification_evidence"


def test_seed_offers_fail_closed_without_effective_capability_evidence() -> None:
    """MF1: seeds cannot invent AssistancePlan sufficiency without capability evidence."""
    from verdict.controller_selection import build_evidence_backed_seed_offers
    from verdict.model_passports import ModelPassport
    from verdict.runtime_certification import CertificationState

    identity = "omniroute/gc/grok-4.5"
    # Passport with no tool_support and no context_window, no metadata → cannot assemble.
    bare = ModelPassport(
        provider="omniroute",
        model_id=identity,
        auth_state="authorized",
        availability_state="eligible",
        qualified_at=NOW - timedelta(minutes=1),
        last_verified_timestamp=NOW - timedelta(minutes=1),
        expires_at=NOW + timedelta(minutes=30),
        tool_support=False,
        context_window=-1,
        token_cost_per_1k=0.001,
    )
    with pytest.raises(ControllerLaunchError) as exc:
        build_evidence_backed_seed_offers(
            _mission(),
            NOW,
            healthy_passports={identity: bare},
            free_identity_ids=frozenset({identity}),
            certification_by_id={identity: (CertificationState.READY, "fresh")},
            metadata_snapshot=None,
        )
    assert exc.value.reason_code == "missing_effective_capability_evidence"


def test_evidence_backed_seed_reaches_bod104_only_with_certified_effective_capability() -> None:
    """MF1/MF2: prepared certified seed can reach BOD-104; UNKNOWN cert cannot win."""
    from verdict.controller_selection import build_evidence_backed_seed_offers
    from verdict.runtime_certification import CertificationState

    identity = "omniroute/gc/grok-4.5"
    seeds = build_evidence_backed_seed_offers(
        _mission(),
        NOW,
        healthy_passports={identity: _factory_passport(identity)},
        free_identity_ids=frozenset({identity}),
        certification_by_id={identity: (CertificationState.READY, "fresh")},
    )
    assert len(seeds) == 1
    seed = seeds[0]
    assert seed.assistance_plan.result == "sufficient"
    assert seed.assistance_plan.plan_id.startswith("ecp:")
    assert seed.certification_state is CertificationState.READY
    assert seed.route.eligible is False

    # Prepare promotes eligibility; real optimize_execution_path is BOD-104.
    def prepare_promote(task, criticality, context, request):
        assert request.offers
        assert all(o.assistance_plan.plan_id.startswith("ecp:") for o in request.offers)
        promoted = []
        for offer in request.offers:
            route = offer.route
            promoted.append(
                replace(
                    offer,
                    route=ConcreteRoute(
                        route_id=route.route_id,
                        gateway=route.gateway,
                        provider=route.provider,
                        model=route.model,
                        credential_pool=route.credential_pool,
                        capability_tier=route.capability_tier,
                        eligible=True,
                        excluded=False,
                        exclusion_reason=None,
                    ),
                )
            )
        return replace(request, offers=tuple(promoted))

    hooks, _, persist_calls = _hooks(seed=list(seeds), prepare=prepare_promote)
    # Use real BOD-104 optimizer (already default in _hooks via optimize_execution_path).
    decision = select_controller_launch(_mission(), hooks=hooks, now=NOW)
    assert decision.mode == "automatic"
    assert decision.prime_target.prime_model == "gc/grok-4.5"
    assert persist_calls, "receipt must persist before launch"
    assert decision.execution_path_decision_digest

    # Same seed with UNKNOWN certification evidence must not win BOD-104.
    unknown_seeds = build_evidence_backed_seed_offers(
        _mission(),
        NOW,
        healthy_passports={identity: _factory_passport(identity)},
        free_identity_ids=frozenset({identity}),
        certification_by_id={identity: (CertificationState.UNKNOWN, "fresh")},
    )
    assert unknown_seeds[0].certification_state is CertificationState.UNKNOWN
    hooks_unknown, _, persist_unknown = _hooks(seed=list(unknown_seeds), prepare=prepare_promote)
    with pytest.raises(ControllerLaunchError) as blocked:
        select_controller_launch(_mission(), hooks=hooks_unknown, now=NOW)
    assert blocked.value.reason_code == "no_eligible_route"
    assert persist_unknown == []


@pytest.mark.parametrize("identity", ["omniroute/gc/grok-4.5", "inventory-key"])
def test_production_passports_certified_before_automatic_selection(identity) -> None:
    """Healthy authorized+eligible passports certify READY and reach BOD-104."""
    from verdict.controller_selection import build_production_controller_selection_hooks
    from verdict.runtime_certification import ComponentKind, certify_runtime

    passport = _factory_passport("omniroute/gc/grok-4.5")
    snapshots_seen = []
    ranked = []
    base, _, persisted = _hooks(seed=[])

    def certify(*, snapshots, now, run_registered_detectors):
        assert now == NOW
        assert run_registered_detectors is False
        snapshots_seen.extend(snapshots)
        assert len(snapshots) == 1
        snapshot = snapshots[0]
        assert snapshot.component_id == snapshot.identity == identity
        assert snapshot.kind is ComponentKind.PROVIDER
        assert snapshot.health_claim == "ready"
        assert snapshot.observed_at == passport.last_verified_timestamp
        assert not snapshot.requires_probe and not snapshot.premium_probe
        return certify_runtime(snapshots=snapshots, now=now)

    def optimize(request):
        decision = optimize_execution_path(request)
        ranked.append(decision)
        return decision

    bundle = build_production_controller_selection_hooks(
        healthy_passports={identity: passport},
        metadata_snapshot=object(),
        free_identity_ids=frozenset({identity}),
        certify_runtime_fn=certify,
        prepare_execution_request=base.prepare_execution_request,
        bind_prime_target=base.bind_prime_target,
        compile_context_digests=base.compile_context_digests,
        build_receipt=base.build_receipt,
        persist_receipt=base.persist_receipt,
        receipt_store=base.receipt_store,
        cost_state_factory=base.cost_state_factory,
        task_state_factory=base.task_state_factory,
        optimize=optimize,
    )
    decision = select_controller_launch(_mission(), hooks=bundle.hooks, now=NOW)
    assert decision.mode == "automatic"
    assert decision.prime_target.prime_model == "gc/grok-4.5"
    assert ranked[0].selected_route is not None
    # Lookup resolves via passport identity_id even when it differs from route_id.
    assert ranked[0].selected_route.model == "gc/grok-4.5"
    assert identity in {snapshots_seen[0].component_id, ranked[0].selected_route.route_id}
    assert persisted
    assert len(snapshots_seen) == 1


@pytest.mark.parametrize(
    ("availability", "auth", "claim", "state", "blocks"),
    [
        ("eligible", "authorized", "ready", CertificationState.READY, False),
        ("degraded", "authorized", "degraded", CertificationState.DEGRADED, True),
        ("quarantined", "authorized", "unavailable", CertificationState.UNAVAILABLE, True),
        ("denied", "authorized", "unavailable", CertificationState.UNAVAILABLE, True),
        ("eligible", "unauthorized", "unknown", CertificationState.UNKNOWN, True),
        ("eligible", "unknown", "unknown", CertificationState.UNKNOWN, True),
        ("unknown", "authorized", "unknown", CertificationState.UNKNOWN, True),
    ],
)
def test_passport_certifier_health_claims_are_evidence_only(
    availability, auth, claim, state, blocks
):
    from verdict.controller_selection import (
        _certify_controller_passports,
        build_production_controller_selection_hooks,
    )
    from verdict.runtime_certification import certify_runtime

    identity = "omniroute/gc/model"
    passport = SimpleNamespace(
        availability_state=availability,
        auth_state=auth,
        last_verified_timestamp=NOW,
        provider="omniroute",
        model_id="omniroute/gc/model",
        tool_support=True,
        token_cost_per_1k=0.001,
        qualified_at=NOW,
        expires_at=NOW + timedelta(minutes=30),
    )

    def certify(**kwargs):
        assert kwargs["snapshots"][0].health_claim == claim
        assert kwargs["run_registered_detectors"] is False
        return certify_runtime(**kwargs)

    evidence = _certify_controller_passports(
        {identity: passport}, now=NOW, certify_runtime_fn=certify
    )
    assert evidence == {identity: (state, "fresh")}

    base, _, persisted = _hooks(seed=[])
    bundle = build_production_controller_selection_hooks(
        healthy_passports={identity: passport},
        metadata_snapshot=object(),
        free_identity_ids=frozenset({identity}),
        certify_runtime_fn=certify,
        prepare_execution_request=base.prepare_execution_request,
        bind_prime_target=base.bind_prime_target,
        compile_context_digests=base.compile_context_digests,
        build_receipt=base.build_receipt,
        persist_receipt=base.persist_receipt,
        receipt_store=base.receipt_store,
        cost_state_factory=base.cost_state_factory,
        task_state_factory=base.task_state_factory,
    )
    if blocks:
        with pytest.raises(ControllerLaunchError) as blocked:
            select_controller_launch(_mission(), hooks=bundle.hooks, now=NOW)
        assert blocked.value.reason_code in {
            "no_eligible_route",
            "missing_certification_evidence",
            "seed_evidence_incomplete",
        }
        assert not persisted
    else:
        decision = select_controller_launch(_mission(), hooks=bundle.hooks, now=NOW)
        assert decision.mode == "automatic"
        assert persisted


def test_passport_certifier_preserves_staleness_and_report_identity():
    from verdict.controller_selection import _certify_controller_passports
    from verdict.runtime_certification import certify_runtime

    identity = "omniroute/gc/model"
    passport = SimpleNamespace(
        availability_state="eligible",
        auth_state="authorized",
        last_verified_timestamp=NOW - timedelta(days=1),
    )
    evidence = _certify_controller_passports(
        {identity: passport}, now=NOW, certify_runtime_fn=certify_runtime
    )
    assert evidence == {identity: (CertificationState.UNAVAILABLE, "stale")}

    def unrelated_report(**kwargs):
        report = certify_runtime(**kwargs)
        return replace(
            report,
            components=tuple(
                replace(component, identity="other/model") for component in report.components
            ),
        )

    assert (
        _certify_controller_passports(
            {identity: passport}, now=NOW, certify_runtime_fn=unrelated_report
        )
        == {}
    )


@pytest.mark.parametrize("invalid_snapshot", [False, True])
def test_passport_certification_failure_is_named_and_redacted(invalid_snapshot):
    from verdict.controller_selection import _certify_controller_passports

    def broken_certifier(**kwargs):
        raise RuntimeError("private /home/operator/credentials secret")

    identity = "/home/operator/credentials" if invalid_snapshot else "omniroute/gc/model"
    with pytest.raises(ControllerLaunchError) as blocked:
        _certify_controller_passports(
            {identity: _factory_passport("omniroute/gc/model")},
            now=NOW,
            certify_runtime_fn=broken_certifier,
        )
    assert blocked.value.reason_code == "runtime_certification_failed"
    assert "/home/" not in str(blocked.value)
    assert "secret" not in str(blocked.value)
