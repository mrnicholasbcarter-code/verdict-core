"""BOD-142 live task-fit shortlist integration and authority proofs."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from verdict.candidate_pool import DROP_CAPABILITY_MISMATCH
from verdict.cost_ledger import PriceEvidenceInput
from verdict.effective_capability import (
    AssistanceCost,
    AssistancePlan,
    DecompositionRequirement,
    ProvenanceClaim,
    TaskSlice,
    VerificationStrategy,
)
from verdict.execution_path import ExecutionPathError, ExecutionPathOffer, ExecutionPathRequest
from verdict.expected_cost import build_strategy_from_assistance
from verdict.free_tier_admit import snapshot_from_payloads
from verdict.intelligence import IntelligenceService
from verdict.metadata.records import (
    SOURCE_MODELS_DEV,
    CapabilityCaps,
    FieldProvenance,
    ModelMetadataRecord,
    ProvenancedField,
    SoftScores,
)
from verdict.metadata.store import MetadataSnapshot
from verdict.model_passports import ModelPassport
from verdict.models import ProviderConfig
from verdict.runtime_certification import CertificationState
from verdict.session_economics import ConcreteRoute

NOW = datetime(2026, 9, 21, 20, 0, tzinfo=timezone.utc)
FETCHED = "2026-09-21T20:00:00Z"
PRICE: PriceEvidenceInput = {"input_usd_per_mtok": "1.00", "evidence_id": "price-bod142"}
STRONG = "quality/strong-code"
FAST = "speed/fast-weak"
NEW = "newco/newly-discovered"
NO_TOOLS = "limited/no-tools"


def _prov(value: bool | int | float) -> ProvenancedField:
    return ProvenancedField(
        value=value, provenance=FieldProvenance(source=SOURCE_MODELS_DEV, fetched_at=FETCHED)
    )


def _metadata() -> MetadataSnapshot:
    rows = ((STRONG, True, 95.0), (FAST, True, 25.0), (NEW, True, 99.0), (NO_TOOLS, False, 100.0))
    return MetadataSnapshot(
        schema_version="1",
        refreshed_at=FETCHED,
        sources={},
        records=tuple(
            ModelMetadataRecord(
                id=model_id,
                provider=model_id.split("/", 1)[0],
                omniroute_ids=(model_id,),
                caps=CapabilityCaps(tools=_prov(tools), context=_prov(128_000)),
                scores=SoftScores(aa_coding=_prov(score)),
            )
            for model_id, tools, score in rows
        ),
    )


def _snapshot():
    identities = (STRONG, FAST, NEW, NO_TOOLS)
    providers = tuple(model_id.split("/", 1)[0] for model_id in identities)
    return snapshot_from_payloads(
        catalog={
            "data": [
                {"id": model_id, "owned_by": model_id.split("/", 1)[0]} for model_id in identities
            ]
        },
        free_tier={
            "perModel": [
                {
                    "modelId": model_id.split("/", 1)[1],
                    "provider": model_id.split("/", 1)[0],
                    "freeType": "keyless",
                }
                for model_id in identities
            ]
        },
        providers={
            "connections": [
                {"provider": provider, "isActive": True, "testStatus": "active"}
                for provider in providers
            ]
        },
    )


def _passport(model_id: str, *, latency_ms: float) -> ModelPassport:
    return ModelPassport(
        provider=model_id.split("/", 1)[0],
        model_id=model_id,
        auth_state="authorized",
        availability_state="eligible",
        qualified_at=NOW - timedelta(minutes=1),
        last_verified_timestamp=NOW - timedelta(minutes=1),
        expires_at=NOW + timedelta(minutes=10),
        latency_p95=latency_ms,
        tool_support=model_id != NO_TOOLS,
    )


def _slice() -> TaskSlice:
    return TaskSlice(
        slice_id="slice-bod142",
        objective="fix code",
        acceptance_criteria=("tests pass",),
        proof_criteria=("pytest green",),
    )


def _plan(candidate_id: str, *, assisted: bool) -> AssistancePlan:
    assistance = AssistanceCost(
        context_tokens=50_000 if assisted else 0,
        tool_tokens=2_000 if assisted else 0,
        planning_tokens=0,
        verification_tokens=50,
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
        assistance_cost=assistance,
        result="sufficient",
        reasons=("needs_assistance",) if assisted else ("intrinsic_ok",),
        intrinsic_sufficient=not assisted,
        assisted_sufficient=True,
        assistance_delta=("context:evidence",) if assisted else (),
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
    )


def _offer(model_id: str, *, assisted: bool = False) -> ExecutionPathOffer:
    route = ConcreteRoute(
        route_id=model_id,
        gateway="omniroute",
        provider=model_id.split("/", 1)[0],
        model=model_id.split("/", 1)[1],
        credential_pool="pool-a",
        capability_tier=2,
        eligible=True,
        excluded=False,
    )
    plan = _plan(model_id, assisted=assisted)
    strategy = "cheap_with_assistance" if assisted else "direct_cheap"
    return ExecutionPathOffer(
        strategy=strategy,
        route=route,
        assistance_plan=plan,
        expected_cost=build_strategy_from_assistance(
            strategy_id=f"{strategy}:{model_id}",
            trajectory_id="traj-bod142",
            assistance=plan.assistance_cost,
            execution_tokens=8_000,
            price=PRICE,
            is_free=True,
            now=NOW,
        ),
        certification_state=CertificationState.READY,
        certification_freshness="fresh",
        is_cheap=True,
    )


def _request(*model_ids: str) -> ExecutionPathRequest:
    return ExecutionPathRequest(
        task_slice=_slice(),
        trajectory_id="traj-bod142",
        offers=tuple(_offer(model_id, assisted=model_id == STRONG) for model_id in model_ids),
        now=NOW,
    )


def _service(probed: list[str], *, top_k: int = 2) -> IntelligenceService:
    def transport(model_id: str, payload: object, timeout: float) -> dict[str, object]:
        probed.append(model_id)
        return {
            "status_code": 200,
            "body": {
                "choices": [{"message": {"role": "assistant", "content": "OK"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            },
        }

    return IntelligenceService(
        primary_model="primary/frontier",
        providers={"omniroute": ProviderConfig(base_url="http://127.0.0.1:20128/v1")},
        profile="production",
        log_path="",
        log_full_task=False,
        discovery_ttl=60,
        admit_snapshot=_snapshot(),
        metadata_snapshot=_metadata(),
        passports={
            STRONG: _passport(STRONG, latency_ms=5_000.0),
            FAST: _passport(FAST, latency_ms=1.0),
            NEW: _passport(NEW, latency_ms=2_000.0),
            NO_TOOLS: _passport(NO_TOOLS, latency_ms=0.5),
        },
        confirm_transport=transport,
        admit_now=NOW,
        require_execution_path_authority=True,
        candidate_top_k=top_k,
        context_roots=(),
        mcp_root="",
    )


def _context(request: ExecutionPathRequest) -> dict[str, object]:
    return {
        "execution_path_request": request,
        "tools_required": True,
        "task_family": "coding",
        "spend_policy": "free_only",
    }


def test_live_confirm_only_touches_candidate_pool_shortlist() -> None:
    probed: list[str] = []
    service = _service(probed, top_k=2)
    prepared = service._prepare_execution_path_request(
        "fix code",
        "low",
        _context(_request(STRONG, FAST, NEW, NO_TOOLS)),
        _request(STRONG, FAST, NEW, NO_TOOLS),
    )
    assert prepared.pool_receipt is not None
    shortlist = {item.route_id for item in prepared.pool_receipt.shortlist}
    assert set(probed) == shortlist
    assert len(probed) == 2
    assert NO_TOOLS not in probed
    assert all(offer.route.route_id in shortlist for offer in prepared.offers)


def test_missing_hard_capability_is_named_drop_and_cannot_return() -> None:
    probed: list[str] = []
    service = _service(probed, top_k=4)
    request = _request(STRONG, FAST, NEW, NO_TOOLS)
    prepared = service._prepare_execution_path_request(
        "fix code", "low", _context(request), request
    )
    assert prepared.pool_receipt is not None
    drops = {item.route_id: item.reason for item in prepared.pool_receipt.hard_drops}
    assert drops[NO_TOOLS] == DROP_CAPABILITY_MISMATCH
    assert NO_TOOLS in prepared.hard_excluded_ids
    assert NO_TOOLS not in {offer.route.route_id for offer in prepared.offers}
    assert NO_TOOLS not in probed


def test_newly_discovered_candidate_can_top_replayable_diverse_shortlist() -> None:
    service = _service([], top_k=2)
    request = _request(STRONG, FAST, NEW)
    first = service._prepare_execution_path_request("fix code", "low", _context(request), request)
    second = service._prepare_execution_path_request("fix code", "low", _context(request), request)
    assert first.pool_receipt is not None and second.pool_receipt is not None
    assert first.pool_receipt.shortlist[0].route_id == NEW
    assert first.pool_receipt.shortlist_digest == second.pool_receipt.shortlist_digest
    assert first.pool_receipt.evidence_digest == second.pool_receipt.evidence_digest
    assert len({item.provider for item in first.pool_receipt.shortlist}) == 2


def test_pool_constrains_bod104_but_does_not_choose_strategy() -> None:
    probed: list[str] = []
    service = _service(probed, top_k=2)
    request = _request(STRONG, FAST)
    prepared = service._prepare_execution_path_request(
        "fix code", "low", _context(request), request
    )
    assert prepared.pool_receipt is not None
    assert prepared.pool_receipt.shortlist[0].route_id == STRONG
    decision = asyncio.run(service.route("fix code", criticality="low", context=_context(prepared)))
    assert decision.model == FAST.split("/", 1)[1]
    assert "bod104_execution_path_authority" in decision.safety_flags
    assert "legacy_non_authority_selector" not in decision.safety_flags


def test_launch_still_fails_closed_without_execution_path_decision() -> None:
    service = _service([], top_k=2)
    with pytest.raises(ExecutionPathError, match="missing ExecutionPathDecision"):
        asyncio.run(service.route("fix code", criticality="low"))
