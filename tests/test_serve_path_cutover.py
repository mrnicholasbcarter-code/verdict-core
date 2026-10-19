"""invariants: no silent strategy bypass of optimize_execution_path."""

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


def _receipt(*route_ids: str):
    """Trusted pool receipt fixture: shortlist in the given rank order."""
    from verdict.candidate_pool import CandidatePoolReceipt, ShortlistEntry, TaskFingerprint

    return CandidatePoolReceipt(
        task_fingerprint=TaskFingerprint(digest="sha256:task", task_family="chat"),
        discovered_count=len(route_ids),
        hard_drops=(),
        probes=(),
        shortlist=tuple(
            ShortlistEntry(
                route_id=route_id,
                metadata_id=None,
                provider="provider-a",
                family_key=route_id,
                score=1.0,
                confidence=1.0,
                score_features={},
                inclusion_reason="fixture_shortlist",
            )
            for route_id in route_ids
        ),
        uncertainty=(),
        evidence_digest="sha256:pool-evidence",
        shortlist_digest="sha256:pool-shortlist",
    )


def _decision():
    cheap = _route("auth-1")
    plan = _plan(candidate_id="auth-1")
    return optimize_execution_path(
        ExecutionPathRequest(
            task_slice=_slice(),
            trajectory_id="traj-127",
            offers=(_offer(strategy="direct_cheap", route=cheap, plan=plan),),
            pool_receipt=_receipt("auth-1"),
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
        pool_receipt=_receipt("auth-1"),
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


def test_select_route_consumes_execution_path() -> None:
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
    """Default API serve path must not silently invent outside the execution-path authority optimizer (ADR-035)."""
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


def test_public_policy_cannot_be_weakened_and_optimizer_is_price_invariant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import verdict.api as api
    from verdict.api import RouteRequest, _authority_context
    from verdict.candidate_pool import HEALTH_OK, RouteEvidence, RouteHealth, build_candidate_pool
    from verdict.execution_path import (
        ExecutionPathError,
        ExecutionPathOffer,
        ExecutionPathRequest,
        optimize_execution_path,
    )
    from verdict.metadata.records import (
        CapabilityCaps,
        FieldProvenance,
        ModelMetadataRecord,
        ProvenancedField,
        SoftScores,
    )
    from verdict.metadata.store import MetadataSnapshot
    from verdict.runtime_certification import CertificationState
    from verdict.serve_path import consume_selected_route
    from verdict.task_profile import profile_task

    # -------------------------------------------------------------------------
    # 1. Both raw chat/Responses reserved flags fail before selector/send;
    #    false protected/client certificate fields cannot lower server policy.
    # -------------------------------------------------------------------------
    for reserved in (
        "allow_legacy_selector",
        "require_execution_path_authority",
        "execution_path_decision",
        "legacy_non_authority_selector",
        "bod104_execution_path_authority",
        "strategy_authority",
        "client_certificate",
    ):
        with pytest.raises(ExecutionPathError, match=r"(reserved|client-supplied|not accepted)"):
            _authority_context({"task": "deploy prod", reserved: True}, task="deploy prod")

    # Critical criticality represents server-protected policy: client protected=False cannot lower it
    ctx_crit = _authority_context(
        {"task": "audit security", "criticality": "critical", "protected": False},
        task="audit security",
    )
    assert ctx_crit.get("protected") is True

    # Test HTTP chat/responses endpoints: reserved flags fail before selector/send
    from fastapi.testclient import TestClient

    from tests.test_api_relay_context_length import ContextLengthTransport
    from tests.test_proxy import _configure_test_app

    dummy_transport = ContextLengthTransport([])
    _configure_test_app(monkeypatch, dummy_transport)

    with TestClient(api.app) as client:
        resp_chat = client.post(
            "/v1/chat/completions",
            json={
                "model": "selected-model",
                "messages": [{"role": "user", "content": "hi"}],
                "allow_legacy_selector": True,
            },
        )
        assert resp_chat.status_code == 400
        assert "reserved control field 'allow_legacy_selector'" in resp_chat.text
        assert len(dummy_transport.requests) == 0

        resp_responses = client.post(
            "/v1/responses",
            json={
                "model": "selected-model",
                "input": "hi",
                "require_execution_path_authority": False,
            },
        )
        assert resp_responses.status_code == 400
        assert "reserved control field 'require_execution_path_authority'" in resp_responses.text
        assert len(dummy_transport.requests) == 0

    # -------------------------------------------------------------------------
    # 2. Context/reasoning/tool/schema/stream requirements survive typed HTTP,
    #    CLI live selection and orchestration; insufficient routes fail.
    # -------------------------------------------------------------------------
    with pytest.raises(ValueError):
        RouteRequest(task="test", min_context_tokens=0)
    with pytest.raises(ValueError):
        RouteRequest(task="test", min_context_tokens=-10)

    req = RouteRequest(
        task="refactor parser",
        task_family="coding",
        min_context_tokens=32000,
        reasoning_required=True,
        tools_required=True,
        structured_output_required=True,
        streaming_required=True,
    )
    assert req.task_family == "coding"
    assert req.min_context_tokens == 32000
    assert req.reasoning_required is True

    raw_context = req.model_dump() if hasattr(req, "model_dump") else req.dict()
    raw_context[CONTEXT_EP_REQUEST] = {
        "schema_version": "1",
        "trajectory_id": "traj-typed-1",
        "slice_id": "slice-typed-1",
        "acceptance_criteria": ["tests pass"],
        "proof_criteria": ["pytest green"],
        "candidates": [
            {
                "route_id": "r1",
                "gateway": "gw1",
                "provider": "p1",
                "model": "m1",
                "certification_state": "ready",
                "certification_freshness": "fresh",
                "eligible": True,
            }
        ],
    }
    ctx = _authority_context(raw_context, task=req.task)
    ep_req = ctx[CONTEXT_EP_REQUEST]
    assert ep_req.task_family == "coding"
    assert ep_req.min_context_tokens == 32000
    assert ep_req.reasoning_required is True

    # Offline catalog mode cannot authorize send
    with pytest.raises(ExecutionPathError, match="offline catalog mode cannot authorize execution"):
        consume_selected_route(
            {
                "managed_backend_status": "offline",
                "strategy_authority": STRATEGY_AUTHORITY,
                "route_id": "offline-1",
                "model": "offline-m",
                "provider": "offline-p",
                "capability_tier": 2,
            }
        )

    # Set up metadata records for routes A and B
    now = datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc)
    now_iso = now.isoformat()
    prov = FieldProvenance(source="test", fetched_at=now_iso)
    rec_a = ModelMetadataRecord(
        id="route-a",
        provider="prov-a",
        caps=CapabilityCaps(context=ProvenancedField(value=32000, provenance=prov)),
        scores=SoftScores(
            aa_coding=ProvenancedField(value=0.9, provenance=prov),
            aa_intelligence=ProvenancedField(value=0.8, provenance=prov),
        ),
    )
    rec_b = ModelMetadataRecord(
        id="route-b",
        provider="prov-b",
        caps=CapabilityCaps(context=ProvenancedField(value=32000, provenance=prov)),
        scores=SoftScores(
            aa_coding=ProvenancedField(value=0.7, provenance=prov),
            aa_intelligence=ProvenancedField(value=0.7, provenance=prov),
        ),
    )
    meta = MetadataSnapshot(
        schema_version="1", refreshed_at=now_iso, sources={}, records=(rec_a, rec_b)
    )

    # Insufficient routes fail capability check
    from verdict.capability_gate import derive_requirements, gate_capability
    from verdict.free_tier_admit import FreeTierAdmitReceipt

    reqs_insufficient = derive_requirements(
        "complex reasoning", {"reasoning_required": True, "min_context_tokens": 100000}
    )
    receipt_base = FreeTierAdmitReceipt(
        admitted=("route-a", "route-b"),
        exclusions=(),
        chosen=None,
        empty_intersection=False,
        active_providers=(),
        free_tier_providers=(),
        free_admitted=(),
        paid_admitted=("route-a", "route-b"),
    )
    gated_receipt = gate_capability(receipt_base, reqs_insufficient, snapshot=meta, now=now)
    assert "route-a" not in gated_receipt.admitted
    assert "route-b" not in gated_receipt.admitted
    assert len(gated_receipt.admitted) == 0
    assert len(gated_receipt.exclusions) == 2

    # -------------------------------------------------------------------------
    # 3. Price-only/class-label changes within the same explicit authorization/
    #    budget constraints leave pool, optimizer and public selection unchanged;
    #    missing price alone is not a gate.
    # -------------------------------------------------------------------------
    # A) Candidate pool: removing cost_fit ensures price change leaves shortlist unchanged
    profile = profile_task("fix bug", context={"task_family": "coding"})

    pool_low_price = build_candidate_pool(
        ("route-a", "route-b"),
        task_profile=profile,
        metadata=meta,
        health={"route-a": RouteHealth(state=HEALTH_OK), "route-b": RouteHealth(state=HEALTH_OK)},
        evidence={
            "route-a": RouteEvidence(latency_ms=100.0, cost_per_million=10.0),
            "route-b": RouteEvidence(latency_ms=100.0, cost_per_million=1.0),
        },
    )
    pool_high_price = build_candidate_pool(
        ("route-a", "route-b"),
        task_profile=profile,
        metadata=meta,
        health={"route-a": RouteHealth(state=HEALTH_OK), "route-b": RouteHealth(state=HEALTH_OK)},
        evidence={
            "route-a": RouteEvidence(latency_ms=100.0, cost_per_million=1000.0),
            "route-b": RouteEvidence(latency_ms=100.0, cost_per_million=1.0),
        },
    )
    # Shortlist order and scores must be identical regardless of cost_per_million
    assert [e.route_id for e in pool_low_price.shortlist] == [
        e.route_id for e in pool_high_price.shortlist
    ]
    assert [e.score for e in pool_low_price.shortlist] == [
        e.score for e in pool_high_price.shortlist
    ]

    # B) Execution path optimizer: orders by pool_receipt shortlist position then candidate_id
    trusted_pool = pool_low_price

    route_a = _route("route-a", model="m-a", provider="prov-a", tier=2)
    route_b = _route("route-b", model="m-b", provider="prov-b", tier=2)

    plan_a = _plan(candidate_id="route-a")
    plan_b = _plan(candidate_id="route-b")

    def _cost_with_price(
        strategy_id: str,
        plan: AssistancePlan,
        price: PriceEvidenceInput | None,
        is_free: bool = False,
    ) -> ExpectedStrategyCost:
        return build_strategy_from_assistance(
            strategy_id=strategy_id,
            trajectory_id="traj-127",
            assistance=plan.assistance_cost,
            execution_tokens=8000,
            price=price,
            is_free=is_free,
            now=NOW,
        )

    # Case 1: route-a is $50, route-b is $1. route-a is #1 on shortlist.
    offers_case1 = (
        ExecutionPathOffer(
            strategy="direct_paid",
            route=route_a,
            assistance_plan=plan_a,
            expected_cost=_cost_with_price(
                "direct_paid:route-a", plan_a, {"input_usd_per_mtok": "50.0", "evidence_id": "p-a"}
            ),
            certification_state=CertificationState.READY,
            certification_freshness="fresh",
            is_paid=True,
        ),
        ExecutionPathOffer(
            strategy="direct_paid",
            route=route_b,
            assistance_plan=plan_b,
            expected_cost=_cost_with_price(
                "direct_paid:route-b", plan_b, {"input_usd_per_mtok": "1.0", "evidence_id": "p-b"}
            ),
            certification_state=CertificationState.READY,
            certification_freshness="fresh",
            is_paid=True,
        ),
    )
    dec1 = optimize_execution_path(
        ExecutionPathRequest(
            task_slice=_slice(),
            trajectory_id="traj-127",
            offers=offers_case1,
            pool_receipt=trusted_pool,
            now=NOW,
        )
    )
    assert dec1.selected_candidate_id == "route-a"

    # Case 2: route-a has UNKNOWN price (missing price alone is not a gate without hard spend budget).
    offers_case2 = (
        ExecutionPathOffer(
            strategy="direct_paid",
            route=route_a,
            assistance_plan=plan_a,
            expected_cost=_cost_with_price("direct_paid:route-a", plan_a, None),
            certification_state=CertificationState.READY,
            certification_freshness="fresh",
            is_paid=True,
        ),
        offers_case1[1],
    )
    dec2 = optimize_execution_path(
        ExecutionPathRequest(
            task_slice=_slice(),
            trajectory_id="traj-127",
            offers=offers_case2,
            pool_receipt=trusted_pool,
            now=NOW,
        )
    )
    # route-a is still selected because missing price alone does not reject without hard spend budget
    assert dec2.selected_candidate_id == "route-a"

    # Case 3: An explicit hard spend budget DOES require proven cost bound.
    dec3 = optimize_execution_path(
        ExecutionPathRequest(
            task_slice=_slice(),
            trajectory_id="traj-127",
            offers=offers_case2,
            pool_receipt=trusted_pool,
            hard_spend_budget=10.0,
            now=NOW,
        )
    )
    # With hard spend budget, route-a is rejected for unknown price bound, route-b wins
    assert dec3.selected_candidate_id == "route-b"

    # Case 4: Class label flip (free vs paid) leaves selection unchanged
    offers_case4 = (
        ExecutionPathOffer(
            strategy="direct_paid",
            route=route_a,
            assistance_plan=plan_a,
            expected_cost=_cost_with_price(
                "direct_paid:route-a",
                plan_a,
                {"input_usd_per_mtok": "10.0", "evidence_id": "p-a"},
                is_free=False,
            ),
            certification_state=CertificationState.READY,
            certification_freshness="fresh",
            is_paid=True,
        ),
        ExecutionPathOffer(
            strategy="direct_cheap",
            route=route_b,
            assistance_plan=plan_b,
            expected_cost=_cost_with_price("direct_cheap:route-b", plan_b, None, is_free=True),
            certification_state=CertificationState.READY,
            certification_freshness="fresh",
            is_cheap=True,
        ),
    )
    dec4 = optimize_execution_path(
        ExecutionPathRequest(
            task_slice=_slice(),
            trajectory_id="traj-127",
            offers=offers_case4,
            pool_receipt=trusted_pool,
            now=NOW,
        )
    )
    # route-a is still selected because shortlist order dominates class labels
    assert dec4.selected_candidate_id == "route-a"
