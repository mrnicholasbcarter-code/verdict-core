"""BOD-108: cold-start AA prior attribution through IntelligenceService caller."""

from __future__ import annotations

from datetime import datetime, timezone

from verdict.free_tier_admit import FreeTierAdmitReceipt
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
from verdict.task_profile import TaskProfile

FETCHED = "2026-09-19T03:00:00Z"
NOW = datetime(2026, 9, 19, 3, 0, tzinfo=timezone.utc)


def _prov(value: bool | int | float) -> ProvenancedField:
    return ProvenancedField(
        value=value, provenance=FieldProvenance(source=SOURCE_MODELS_DEV, fetched_at=FETCHED)
    )


def _record(model_id: str, *, scores: SoftScores) -> ModelMetadataRecord:
    return ModelMetadataRecord(
        id=model_id,
        caps=CapabilityCaps(tools=_prov(True), context=_prov(128000)),
        scores=scores,
        omniroute_ids=(model_id,),
        provider=model_id.split("/", 1)[0],
    )


def _profile(task_family: str) -> TaskProfile:
    return TaskProfile(
        digest="sha256:test",
        task_family=task_family,
        required_capabilities=(),
        min_context=None,
        spend_policy="free_first",
        task_class_hint=None,
        requirements_reasons=(),
        objective_preview="test",
    )


def test_apply_candidate_pool_serializes_eval_prior_in_admit_receipt() -> None:
    high_code = "model/high-code"
    high_intel = "model/high-intel"
    snapshot = MetadataSnapshot(
        schema_version="1",
        refreshed_at=FETCHED,
        sources={},
        records=(
            _record(
                high_code,
                scores=SoftScores(aa_coding=_prov(90.0), aa_intelligence=_prov(10.0)),
            ),
            _record(
                high_intel,
                scores=SoftScores(aa_coding=_prov(10.0), aa_intelligence=_prov(90.0)),
            ),
        ),
    )
    service = IntelligenceService(
        primary_model="anthropic/claude-3-opus",
        providers={},
        profile="development",
        log_path="",
        log_full_task=False,
        discovery_ttl=60,
        metadata_snapshot=snapshot,
        allow_offline=True,
        persist_routing_receipts=False,
        candidate_top_k=2,
    )
    # Seed health via passports? _apply_candidate_pool builds HEALTH_OK for advertised.
    base = FreeTierAdmitReceipt(
        admitted=(high_code, high_intel),
        exclusions=(),
        chosen=high_code,
        empty_intersection=False,
        active_providers=(),
        free_tier_providers=(),
    )
    receipt, pool = service._apply_candidate_pool(
        base,
        profile=_profile("code_edit"),
        metadata=snapshot,
        identity_map=None,
    )
    assert receipt.candidate_pool is not None
    shortlist = receipt.candidate_pool["shortlist"]
    assert shortlist[0]["route_id"] == high_code
    assert shortlist[0]["eval_prior_kind"] == "metadata"
    assert shortlist[0]["eval_prior_metric"] == "aa_coding"
    assert shortlist[0]["eval_prior_value"] == 90.0
    assert shortlist[0]["eval_prior_provenance"]["source"] == SOURCE_MODELS_DEV
    assert shortlist[0]["eval_prior_metadata_id"] == high_code
    # Pool object agrees
    assert pool.shortlist[0].eval_prior_metric == "aa_coding"
