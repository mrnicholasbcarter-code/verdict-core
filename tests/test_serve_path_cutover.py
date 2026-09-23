"""BOD-127 invariants: no silent strategy bypass of optimize_execution_path."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from verdict.autodev_routing import CandidateEvidence
from verdict.availability import AvailabilityState
from verdict.chooser import choose_route
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
    ExecutionPathError,
    ExecutionPathOffer,
    ExecutionPathRequest,
    optimize_execution_path,
)
from verdict.expected_cost import ExpectedStrategyCost, build_strategy_from_assistance
from verdict.failover_engine import FailoverEngine, FailoverEngineError
from verdict.gateway_adapters import AdapterRouteIdentity
from verdict.intelligence import IntelligenceService
from verdict.live_routing import ConcreteIdentity, select_route
from verdict.models import ModelConfig, ProviderConfig
from verdict.runtime_certification import CertificationState
from verdict.serve_path import (
    CONTEXT_ALLOW_LEGACY,
    CONTEXT_EP_DECISION,
    CONTEXT_EP_REQUEST,
    CONTEXT_REQUIRE_AUTHORITY,
    consume_selected_route,
    failover_must_defer_to_bounded_recovery,
    free_tier_feed_identities,
    match_candidate_to_selected_route,
    require_serve_path_decision,
    resolve_execution_path_decision,
    selected_route_dispatch_identity,
    serve_path_authority_required,
)
from verdict.session_economics import ConcreteRoute

NOW = datetime(2026, 9, 19, 12, 0, tzinfo=timezone.utc)
PRICE: PriceEvidenceInput = {"input_usd_per_mtok": "1.00", "evidence_id": "price-1"}


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
) -> ConcreteRoute:
    return ConcreteRoute(
        route_id=route_id,
        gateway=gateway,
        provider=provider,
        model=model,
        credential_pool="pool-a",
        capability_tier=tier,
        eligible=True,
        excluded=False,
    )


def _plan(*, candidate_id: str) -> AssistancePlan:
    cost = AssistanceCost(
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
        selected_tool_surface=("pytest",),
        decomposition=DecompositionRequirement(
            required=False,
            child_slices=(),
            preserves_parent_acceptance=True,
            preserves_parent_proof=True,
            reason="",
            planner_role="",
        ),
        verification=VerificationStrategy(kind="pytest", proof_criteria=("pytest green",)),
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
                digest="sha256:plan",
                observed_at=NOW.isoformat(),
                freshness="fresh",
                fresh=True,
            ),
        ),
        evidence_digest="sha256:plan",
    )


def _cost(strategy_id: str, *, assistance: AssistanceCost) -> ExpectedStrategyCost:
    return build_strategy_from_assistance(
        strategy_id=strategy_id,
        trajectory_id="traj-127",
        assistance=assistance,
        execution_tokens=8_000,
        price=PRICE,
        is_free=True,
        now=NOW,
    )


def _offer(*, strategy: str, route: ConcreteRoute, plan: AssistancePlan) -> ExecutionPathOffer:
    return ExecutionPathOffer(
        strategy=strategy,  # type: ignore[arg-type]
        route=route,
        assistance_plan=plan,
        expected_cost=_cost(f"{strategy}:{route.route_id}", assistance=plan.assistance_cost),
        certification_state=CertificationState.READY,
        certification_freshness="fresh",
        is_cheap=True,
    )


def _decision():
    cheap = _route("auth-1")
    plan = _plan(candidate_id="auth-1")
    return optimize_execution_path(
        ExecutionPathRequest(
            task_slice=_slice(),
            trajectory_id="traj-127",
            offers=(_offer(strategy="direct_cheap", route=cheap, plan=plan),),
            now=NOW,
        )
    )


def _offline_svc(**kwargs):
    return IntelligenceService(
        primary_model="anthropic/claude-3-opus",
        providers={
            "local": ProviderConfig(
                base_url="http://localhost:1/v1",
                models={"meta/llama": ModelConfig(cost_per_1k=0.1)},
            )
        },
        profile=kwargs.pop("profile", "development"),
        log_path="",
        log_full_task=False,
        discovery_ttl=60,
        allow_offline=True,
        **kwargs,
    )


def test_serve_path_requires_authority_for_production_and_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert serve_path_authority_required(profile="production") is True
    assert serve_path_authority_required(profile="development") is False
    monkeypatch.setenv("VERDICT_REQUIRE_EXECUTION_PATH", "1")
    assert serve_path_authority_required(profile="development") is True
    monkeypatch.setenv("VERDICT_REQUIRE_EXECUTION_PATH", "0")
    assert serve_path_authority_required(profile="production") is False
    assert (
        serve_path_authority_required(
            profile="development", context={CONTEXT_REQUIRE_AUTHORITY: True}
        )
        is True
    )
    assert (
        serve_path_authority_required(profile="production", context={CONTEXT_ALLOW_LEGACY: True})
        is False
    )


def test_require_serve_path_decision_fail_closed_without_ep() -> None:
    with pytest.raises(ExecutionPathError, match="missing ExecutionPathDecision"):
        require_serve_path_decision(None, surface="api_v1_route")


def test_blocked_dispatch_names_unmapped_pool_reason() -> None:
    from verdict.candidate_pool import (
        DROP_UNMAPPED,
        CandidatePoolReceipt,
        HardDrop,
        TaskFingerprint,
    )

    route = _route("agy/gemini-3.7-flash-low", model="agy/gemini-3.7-flash-low")
    plan = _plan(candidate_id=route.route_id)
    receipt = CandidatePoolReceipt(
        task_fingerprint=TaskFingerprint(digest="sha256:task", task_family="chat"),
        discovered_count=1,
        hard_drops=(HardDrop(route.route_id, DROP_UNMAPPED, "no unique models.json leaf"),),
        probes=(),
        shortlist=(),
        uncertainty=(f"{route.route_id}:{DROP_UNMAPPED}",),
        evidence_digest="sha256:evidence",
        shortlist_digest="sha256:shortlist",
    )
    decision = optimize_execution_path(
        ExecutionPathRequest(
            task_slice=_slice(),
            trajectory_id="traj-unmapped",
            offers=(_offer(strategy="direct_cheap", route=route, plan=plan),),
            pool_receipt=receipt,
            now=NOW,
        )
    )
    with pytest.raises(ExecutionPathError, match="unmapped") as caught:
        require_serve_path_decision(decision, surface="intelligence.route")
    message = str(caught.value)
    assert "why=no_qualified_complete_strategies" in message
    assert "reasons=unmapped" in message
    assert "not_in_candidate_pool_shortlist" not in message


def test_resolve_and_dispatch_from_ep_request() -> None:
    cheap = _route("auth-1")
    plan = _plan(candidate_id="auth-1")
    request = ExecutionPathRequest(
        task_slice=_slice(),
        trajectory_id="traj-127",
        offers=(_offer(strategy="direct_cheap", route=cheap, plan=plan),),
        now=NOW,
    )
    decision = resolve_execution_path_decision({CONTEXT_EP_REQUEST: request})
    assert decision is not None
    identity = selected_route_dispatch_identity(decision)
    assert identity["model"] == "cheap-model"
    assert identity["strategy_authority"] == STRATEGY_AUTHORITY
    assert identity["selected_strategy"] == "direct_cheap"


def test_client_dict_ep_decision_rejected() -> None:
    with pytest.raises(ExecutionPathError, match="ExecutionPathDecision"):
        resolve_execution_path_decision(
            {
                CONTEXT_EP_DECISION: {
                    "selected_strategy": "direct_cheap",
                    "selected_candidate_id": "evil",
                }
            }
        )


def test_intelligence_production_serve_fails_closed_without_ep() -> None:
    svc = _offline_svc(profile="production", require_execution_path_authority=True)
    with pytest.raises(ExecutionPathError, match="missing ExecutionPathDecision"):
        asyncio.run(svc.route("refactor module"))


def test_intelligence_dispatches_ep_only_on_authoritative_serve() -> None:
    decision = _decision()
    svc = _offline_svc(profile="production", require_execution_path_authority=True)
    dec = asyncio.run(svc.route("refactor module", context={CONTEXT_EP_DECISION: decision}))
    assert dec.model == "cheap-model"
    assert "bod104_execution_path_authority" in dec.safety_flags
    assert "legacy_non_authority_selector" not in dec.safety_flags


def test_intelligence_legacy_escape_is_explicit_not_silent() -> None:
    svc = _offline_svc(profile="production", require_execution_path_authority=True)
    dec = asyncio.run(svc.route("summarize readme", context={CONTEXT_ALLOW_LEGACY: True}))
    assert "legacy_non_authority_selector" in dec.safety_flags


def test_chooser_yields_to_ep_and_cannot_invent() -> None:
    decision = _decision()
    rogue = CandidateEvidence(
        requested_alias="rogue",
        route=AdapterRouteIdentity(
            gateway_id="gw-x",
            route_id="route-rogue",
            provider="x",
            model_id="rogue-model",
            protocol="openai.chat",
        ),
        availability=AvailabilityState.ELIGIBLE,
        capabilities={"resource_class": "free", "tools": "observed", "code": "observed"},
        observed_at=NOW,
        ttl_seconds=60,
        source="fixture",
    )
    receipt = choose_route([rogue], task_class="implementation", execution_path_decision=decision)
    assert receipt.selected is not None
    assert receipt.selected["model"] == "cheap-model"


def test_live_routing_select_route_consumes_ep() -> None:
    decision = _decision()
    identity = ConcreteIdentity(
        identity_id="other-paid",
        provider_id="p",
        gateway_id="gw",
        cost_class="paid",
        context_limit=128000,
        output_limit=4096,
        tools=True,
        modalities=("text",),
        spec_captured_at=NOW,
    )
    from verdict.live_routing import Candidate

    selection = select_route(
        (Candidate("other-paid", "kept", None, identity=identity),),
        execution_path_decision=decision,
    )
    assert selection.chosen.ref in {"cheap-model", "auth-1"}


def test_failover_engine_defers_when_ep_bound(tmp_path) -> None:
    from verdict.execution_session import ExecutionSession
    from verdict.memory_plane import MemoryPlane

    decision = _decision()
    with pytest.raises(ExecutionPathError, match=r"BoundedRecovery|BOD-104"):
        failover_must_defer_to_bounded_recovery(execution_path_decision=decision)

    plane = MemoryPlane(str(tmp_path / "plane.db"))
    session = ExecutionSession.create(
        "s-fail",
        {"task": "resolve", "requirements": {"required": ["tools"]}},
        steps=[("qualify", "qualify"), ("route", "route")],
        plane=plane,
        model_id="provider-a/cheap-model",
    )
    session.start(plane)
    engine = FailoverEngine()
    with pytest.raises(
        (FailoverEngineError, ExecutionPathError), match=r"BoundedRecovery|BOD-104|defer"
    ):
        engine.failover(
            session,
            plane,
            provider="provider-a",
            model_id="cheap-model",
            error_class="timeout",
            execution_path_decision=decision,
        )


def _runtime(runtime_id: str, *, cost: float) -> RuntimeCandidate:
    return RuntimeCandidate(
        runtime_id=runtime_id,
        catalog_present=True,
        live_eligible=True,
        availability="eligible",
        signals={"cost": {"cost_usd": cost}},
        capabilities=["tools"],
        provider="provider-a",
        model=runtime_id,
    )


def test_consume_selected_route_rejects_bare_invented_mapping() -> None:
    """Phase-6 P0: loose Mapping payloads must not invent dispatch identity."""
    with pytest.raises(ExecutionPathError, match=r"strategy_authority|bare invented"):
        consume_selected_route({"model": "x"})
    with pytest.raises(ExecutionPathError, match=r"strategy_authority|bare invented"):
        consume_selected_route(
            {"model": "x", "provider": "p", "route_id": "r", "capability_tier": 1}
        )
    with pytest.raises(ExecutionPathError, match="missing non-empty model"):
        consume_selected_route(
            {
                "strategy_authority": STRATEGY_AUTHORITY,
                "provider": "p",
                "route_id": "r",
                "capability_tier": 1,
            }
        )
    with pytest.raises(ExecutionPathError, match="missing provider"):
        consume_selected_route(
            {
                "strategy_authority": STRATEGY_AUTHORITY,
                "model": "x",
                "route_id": "r",
                "capability_tier": 1,
            }
        )
    with pytest.raises(ExecutionPathError, match="missing capability_tier"):
        consume_selected_route(
            {
                "strategy_authority": STRATEGY_AUTHORITY,
                "model": "x",
                "provider": "p",
                "route_id": "r",
            }
        )


def test_consume_selected_route_accepts_ep_decision_and_concrete_route() -> None:
    decision = _decision()
    from_ep = consume_selected_route(decision)
    assert from_ep["model"] == "cheap-model"
    assert from_ep["strategy_authority"] == STRATEGY_AUTHORITY

    route = _route("auth-1")
    from_route = consume_selected_route(route)
    assert from_route["model"] == "cheap-model"
    assert from_route["provider"] == "provider-a"
    assert from_route["strategy_authority"] == STRATEGY_AUTHORITY


def test_consume_selected_route_accepts_stamped_authorized_mapping() -> None:
    stamped = {
        "route_id": "auth-1",
        "gateway": "gateway-a",
        "provider": "provider-a",
        "model": "cheap-model",
        "capability_tier": 1,
        "strategy_authority": STRATEGY_AUTHORITY,
    }
    identity = consume_selected_route(stamped)
    assert identity["model"] == "cheap-model"
    assert identity["provider"] == "provider-a"
    assert identity["capability_tier"] == 1
    assert identity["strategy_authority"] == STRATEGY_AUTHORITY


def test_swarm_binds_authorized_selected_route_not_cheapest() -> None:
    decision = _decision()
    snap = AvailabilitySnapshot(
        observed_at=NOW.isoformat(),
        state="ready",
        candidates=[_runtime("other-model", cost=0.01), _runtime("cheap-model", cost=1.0)],
        ttl_seconds=60,
    )
    result = SwarmDispatcher().dispatch(snap, selected_route=decision.selected_route, now=NOW)
    assert result.selected is not None
    assert result.selected.runtime_id == "cheap-model"
    assert "selected_route" in result.reason


def test_swarm_fails_closed_without_matching_selected_route() -> None:
    decision = _decision()
    snap = AvailabilitySnapshot(
        observed_at=NOW.isoformat(),
        state="ready",
        candidates=[_runtime("unrelated", cost=0.01)],
        ttl_seconds=60,
    )
    with pytest.raises(ExecutionPathError, match="no candidate matches"):
        SwarmDispatcher().dispatch(snap, selected_route=decision.selected_route, now=NOW)


def test_free_tier_is_feed_not_authority() -> None:
    class _Receipt:
        admitted = ("free-a", "free-b")
        chosen = "free-a"

    assert free_tier_feed_identities(_Receipt()) == ("free-a", "free-b")
    with pytest.raises(ExecutionPathError):
        require_serve_path_decision(None)


def test_match_candidate_helper_no_invent() -> None:
    decision = _decision()
    matched = match_candidate_to_selected_route(
        [{"runtime_id": "cheap-model"}, {"runtime_id": "other"}], decision
    )
    assert matched["runtime_id"] == "cheap-model"
    with pytest.raises(ExecutionPathError):
        match_candidate_to_selected_route([{"runtime_id": "nope"}], decision)


def test_api_route_injects_require_authority(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default API serve path must not silently invent outside BOD-104."""
    import asyncio

    import verdict.api as api

    captured: dict[str, object] = {}

    class CapturingIntel:
        async def route(self, task, criticality="medium", context=None, *, request_id=None):
            captured["context"] = context
            raise ExecutionPathError("missing ExecutionPathDecision; strategy must come from")

    monkeypatch.setattr(api, "intelligence_instance", CapturingIntel())

    async def _call() -> None:
        await api._route_with_intelligence("refactor module", "medium", context={"task": "x"})

    with pytest.raises(api.HTTPException) as exc_info:
        asyncio.run(_call())
    assert exc_info.value.status_code == 400
    ctx = captured.get("context")
    assert isinstance(ctx, dict)
    assert ctx.get(CONTEXT_REQUIRE_AUTHORITY) is True


def test_legacy_selectors_are_feeds_not_authority() -> None:
    """select_best_eligible_model remains a feed/compat helper, never serve authority."""
    import inspect

    from verdict import serve_path as serve_mod
    from verdict.dispatcher import SwarmDispatcher as AuthorizedRouteDispatcher
    from verdict.router import select_best_eligible_model

    assert "select_best_eligible_model" not in inspect.getsource(serve_mod)
    assert "select_best_eligible_model" not in inspect.getsource(AuthorizedRouteDispatcher.dispatch)
    src = inspect.getsource(select_best_eligible_model)
    assert "eligible" in src.lower()
