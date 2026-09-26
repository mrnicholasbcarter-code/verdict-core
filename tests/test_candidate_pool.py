"""Candidate Pool Intelligence — evidence-fused Top-K shortlist."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest

from verdict.candidate_pool import (
    DROP_CAPABILITY_MISMATCH,
    DROP_HARD_HEALTH,
    DROP_REQUIRED_UNKNOWN,
    DROP_UNMAPPED,
    HEALTH_FAILED,
    HEALTH_OK,
    PROBE_CHEAP_HEALTH,
    PROBE_SMALL_QUAL,
    PROBE_STATIC_METADATA,
    CandidatePoolError,
    OutcomeObservation,
    ProbeBudget,
    RouteEvidence,
    RouteHealth,
    build_candidate_pool,
    fingerprint_task,
)
from verdict.metadata.records import (
    SOURCE_MODELS_DEV,
    CapabilityCaps,
    FieldProvenance,
    ModelMetadataRecord,
    ProvenancedField,
    SoftScores,
)
from verdict.metadata.store import MetadataSnapshot

NOW = datetime(2026, 9, 19, 3, 0, tzinfo=timezone.utc)
FETCHED = "2026-09-19T03:00:00Z"

FREE_NO_TOOLS = "openrouter/nvidia/nemotron-no-tools:free"
FREE_TOOLS = "opencode/hy3-free"
PAID_TOOLS = "groq/llama-3.3-70b-versatile"
FRONTIER = "anthropic/claude-3-opus"
ALIAS_A = "or/llama-3.3-70b-versatile"
ALIAS_B = "openrouter/meta-llama/llama-3.3-70b-versatile"
UNKNOWN_CAPS = "mystery/unknown-caps"
UNMAPPED = "ghost/never-joined"
HEALTH_DEAD = "openai/gpt-4o-mini-dead"
DIVERSE_FALLBACK = "mistral/mistral-small"


def _prov(value: bool | int | float) -> ProvenancedField:
    return ProvenancedField(
        value=value, provenance=FieldProvenance(source=SOURCE_MODELS_DEV, fetched_at=FETCHED)
    )


def _record(
    model_id: str,
    *,
    tools: bool | None = True,
    vision: bool | None = False,
    context: int | None = 128000,
    omniroute_ids: tuple[str, ...] = (),
    scores: SoftScores | None = None,
) -> ModelMetadataRecord:
    caps = CapabilityCaps(
        tools=None if tools is None else _prov(tools),
        vision=None if vision is None else _prov(vision),
        context=None if context is None else _prov(context),
    )
    aliases = omniroute_ids or (model_id,)
    return ModelMetadataRecord(
        id=model_id,
        caps=caps,
        scores=scores or SoftScores(),
        omniroute_ids=aliases,
        provider=model_id.split("/", 1)[0],
    )


def _store(records: tuple[ModelMetadataRecord, ...]) -> MetadataSnapshot:
    return MetadataSnapshot(schema_version="1", refreshed_at=FETCHED, sources={}, records=records)


def _base_store() -> MetadataSnapshot:
    return _store(
        (
            _record(FREE_NO_TOOLS, tools=False),
            _record(FREE_TOOLS, tools=True),
            _record(
                PAID_TOOLS,
                tools=True,
                omniroute_ids=(PAID_TOOLS, ALIAS_A, ALIAS_B),
                scores=SoftScores(aa_coding=_prov(70.0)),
            ),
            _record(FRONTIER, tools=True, vision=True, context=200000),
            _record(UNKNOWN_CAPS, tools=None, vision=None, context=None),
            _record(HEALTH_DEAD, tools=True),
            _record(DIVERSE_FALLBACK, tools=True, scores=SoftScores(aa_coding=_prov(55.0))),
        )
    )


def test_fingerprint_task_is_stable_and_structured() -> None:
    fp_a = fingerprint_task(
        "Fix the auth bug in src/login.py",
        context={
            "tools_required": True,
            "language": "python",
            "risk": "medium",
            "min_context": 8000,
        },
    )
    fp_b = fingerprint_task(
        "Fix the auth bug in src/login.py",
        context={
            "tools_required": True,
            "language": "python",
            "risk": "medium",
            "min_context": 8000,
        },
    )
    assert fp_a.digest == fp_b.digest
    assert fp_a.digest.startswith("sha256:")
    assert "tools" in fp_a.required_capabilities
    assert fp_a.language == "python"
    assert fp_a.to_dict()["digest"] == fp_a.digest


def test_three_thousand_advertised_reduces_to_bounded_topk_without_catalog_probe() -> None:
    store = _base_store()
    # 3000+ noisy advertised identities; only a handful are joinable + eligible.
    advertised = [f"noise/provider-{i}/model-{i}" for i in range(3000)]
    advertised.extend(
        [
            FREE_NO_TOOLS,
            FREE_TOOLS,
            PAID_TOOLS,
            ALIAS_A,
            ALIAS_B,
            FRONTIER,
            UNKNOWN_CAPS,
            UNMAPPED,
            HEALTH_DEAD,
            DIVERSE_FALLBACK,
        ]
    )
    probes_run: list[str] = []

    def probe(route_id: str, level: str) -> dict[str, Any]:
        probes_run.append(f"{route_id}:{level}")
        return {"ok": True, "level": level}

    receipt = build_candidate_pool(
        advertised,
        task="Implement a tools-required refactor",
        context={"tools_required": True},
        metadata=store,
        health={
            HEALTH_DEAD: RouteHealth(state=HEALTH_FAILED, detail="auth_failed"),
            FREE_TOOLS: RouteHealth(state=HEALTH_OK),
            PAID_TOOLS: RouteHealth(state=HEALTH_OK),
            ALIAS_A: RouteHealth(state=HEALTH_OK),
            ALIAS_B: RouteHealth(state=HEALTH_OK),
            FRONTIER: RouteHealth(state=HEALTH_OK),
            DIVERSE_FALLBACK: RouteHealth(state=HEALTH_OK),
        },
        top_k=5,
        probe_budget=ProbeBudget(max_probes=4, max_level=PROBE_CHEAP_HEALTH),
        probe_fn=probe,
    )

    assert receipt.discovered_count >= 3000
    assert len(receipt.shortlist) <= 5
    assert len(receipt.shortlist) >= 1
    # Must not actively probe the whole catalog.
    assert len(probes_run) <= 4
    assert all(not p.startswith("noise/") for p in probes_run)
    hard_ids = {drop.route_id for drop in receipt.hard_drops}
    assert FREE_NO_TOOLS in hard_ids
    assert UNMAPPED in hard_ids
    assert HEALTH_DEAD in hard_ids
    shortlist_ids = [entry.route_id for entry in receipt.shortlist]
    assert FREE_NO_TOOLS not in shortlist_ids
    assert HEALTH_DEAD not in shortlist_ids
    assert UNMAPPED not in shortlist_ids


def test_healthy_task_incompatible_removed_before_ranking() -> None:
    store = _base_store()
    receipt = build_candidate_pool(
        [FREE_NO_TOOLS, FREE_TOOLS, PAID_TOOLS],
        task="Call tools to edit files",
        context={"tools_required": True},
        metadata=store,
        health={
            FREE_NO_TOOLS: RouteHealth(state=HEALTH_OK),
            FREE_TOOLS: RouteHealth(state=HEALTH_OK),
            PAID_TOOLS: RouteHealth(state=HEALTH_OK),
        },
        top_k=3,
    )
    drop_reasons = {d.route_id: d.reason for d in receipt.hard_drops}
    assert drop_reasons[FREE_NO_TOOLS] == DROP_CAPABILITY_MISMATCH
    assert FREE_NO_TOOLS not in [e.route_id for e in receipt.shortlist]
    # Scoring cannot restore the hard drop.
    restored = build_candidate_pool(
        [FREE_NO_TOOLS, FREE_TOOLS],
        task="Call tools to edit files",
        context={"tools_required": True},
        metadata=store,
        health={
            FREE_NO_TOOLS: RouteHealth(state=HEALTH_OK),
            FREE_TOOLS: RouteHealth(state=HEALTH_OK),
        },
        evidence={FREE_NO_TOOLS: RouteEvidence(task_success_rate=1.0, eval_score=99.0)},
        top_k=2,
    )
    assert FREE_NO_TOOLS not in [e.route_id for e in restored.shortlist]
    assert any(d.route_id == FREE_NO_TOOLS for d in restored.hard_drops)


def test_unknown_capability_never_silently_capable() -> None:
    store = _base_store()
    receipt = build_candidate_pool(
        [UNKNOWN_CAPS, FREE_TOOLS],
        task="Need tools",
        context={"tools_required": True},
        metadata=store,
        health={
            UNKNOWN_CAPS: RouteHealth(state=HEALTH_OK),
            FREE_TOOLS: RouteHealth(state=HEALTH_OK),
        },
        evidence={UNKNOWN_CAPS: RouteEvidence(task_success_rate=1.0, eval_score=100.0)},
        top_k=2,
    )
    reasons = {d.route_id: d.reason for d in receipt.hard_drops}
    assert reasons[UNKNOWN_CAPS] == DROP_REQUIRED_UNKNOWN
    assert UNKNOWN_CAPS not in [e.route_id for e in receipt.shortlist]
    # Ambiguous/conflicting evidence stays explicit, not capable.
    assert receipt.uncertainty  # non-empty uncertainty notes for unknown/conflict cases


def test_matching_task_success_raises_rank_among_eligible() -> None:
    store = _base_store()
    fp = fingerprint_task(
        "refactor python module", context={"tools_required": True, "language": "python"}
    )
    receipt = build_candidate_pool(
        [FREE_TOOLS, PAID_TOOLS, DIVERSE_FALLBACK],
        task_fingerprint=fp,
        metadata=store,
        health={
            FREE_TOOLS: RouteHealth(state=HEALTH_OK),
            PAID_TOOLS: RouteHealth(state=HEALTH_OK),
            DIVERSE_FALLBACK: RouteHealth(state=HEALTH_OK),
        },
        outcomes=(
            OutcomeObservation(
                route_id=FREE_TOOLS,
                task_family=fp.task_family,
                success=True,
                fingerprint_digest=fp.digest,
            ),
            OutcomeObservation(
                route_id=PAID_TOOLS,
                task_family=fp.task_family,
                success=False,
                fingerprint_digest=fp.digest,
            ),
        ),
        top_k=3,
    )
    ids = [e.route_id for e in receipt.shortlist]
    assert ids[0] == FREE_TOOLS
    assert FREE_TOOLS in ids and PAID_TOOLS in ids


def test_failure_cannot_override_hard_health_exclusion() -> None:
    store = _base_store()
    fp = fingerprint_task("tools work", context={"tools_required": True})
    receipt = build_candidate_pool(
        [HEALTH_DEAD, FREE_TOOLS],
        task_fingerprint=fp,
        metadata=store,
        health={
            HEALTH_DEAD: RouteHealth(state=HEALTH_FAILED, detail="cooldown"),
            FREE_TOOLS: RouteHealth(state=HEALTH_OK),
        },
        outcomes=(
            OutcomeObservation(
                route_id=HEALTH_DEAD,
                task_family=fp.task_family,
                success=True,
                fingerprint_digest=fp.digest,
            ),
        ),
        evidence={HEALTH_DEAD: RouteEvidence(task_success_rate=1.0, eval_score=100.0)},
        top_k=2,
    )
    assert any(
        d.route_id == HEALTH_DEAD and d.reason == DROP_HARD_HEALTH for d in receipt.hard_drops
    )
    assert HEALTH_DEAD not in [e.route_id for e in receipt.shortlist]


def test_aliases_cannot_crowd_out_distinct_fallback() -> None:
    store = _base_store()
    receipt = build_candidate_pool(
        [ALIAS_A, ALIAS_B, PAID_TOOLS, DIVERSE_FALLBACK],
        task="tools required coding",
        context={"tools_required": True},
        metadata=store,
        health={
            ALIAS_A: RouteHealth(state=HEALTH_OK),
            ALIAS_B: RouteHealth(state=HEALTH_OK),
            PAID_TOOLS: RouteHealth(state=HEALTH_OK),
            DIVERSE_FALLBACK: RouteHealth(state=HEALTH_OK),
        },
        top_k=2,
    )
    ids = [e.route_id for e in receipt.shortlist]
    assert DIVERSE_FALLBACK in ids
    # At most one of the near-identical llama aliases/canonical.
    llama_hits = sum(1 for i in ids if "llama-3.3-70b" in i or i == PAID_TOOLS)
    assert llama_hits <= 1


def test_insufficient_evidence_gets_bounded_targeted_probe() -> None:
    store = _base_store()
    probed: list[tuple[str, str]] = []

    def probe(route_id: str, level: str) -> dict[str, Any]:
        probed.append((route_id, level))
        if level == PROBE_SMALL_QUAL:
            return {"ok": True, "qualified": True, "level": level}
        return {"ok": True, "level": level}

    receipt = build_candidate_pool(
        [FREE_TOOLS, PAID_TOOLS],
        task="Need tools for edit",
        context={"tools_required": True},
        metadata=store,
        health={FREE_TOOLS: RouteHealth(state=HEALTH_OK), PAID_TOOLS: RouteHealth(state=HEALTH_OK)},
        # FREE_TOOLS has metadata but no outcome/eval evidence → uncertain → probe.
        evidence={PAID_TOOLS: RouteEvidence(task_success_rate=0.9, eval_score=80.0)},
        top_k=2,
        probe_budget=ProbeBudget(max_probes=3, max_level=PROBE_SMALL_QUAL),
        probe_fn=probe,
    )
    assert probed, "promising uncertain candidate must receive a targeted probe"
    assert all(level != "larger_eval" for _, level in probed)
    assert len(probed) <= 3
    # Hard-excluded are never probed.
    assert all(route != FREE_NO_TOOLS for route, _ in probed)
    assert FREE_TOOLS in [e.route_id for e in receipt.shortlist]
    assert any(p.route_id == FREE_TOOLS for p in receipt.probes)
    # Ladder starts at cheapest relevant step.
    first_levels = [level for _, level in probed]
    assert first_levels[0] in {PROBE_STATIC_METADATA, PROBE_CHEAP_HEALTH, PROBE_SMALL_QUAL}


def test_probe_failure_keeps_candidate_out_until_qualified() -> None:
    store = _base_store()

    def probe(route_id: str, level: str) -> dict[str, Any]:
        return {"ok": False, "qualified": False, "level": level, "detail": "qual_failed"}

    receipt = build_candidate_pool(
        [FREE_TOOLS],
        task="Need tools",
        context={"tools_required": True},
        metadata=store,
        health={FREE_TOOLS: RouteHealth(state=HEALTH_OK)},
        top_k=1,
        probe_budget=ProbeBudget(max_probes=2, max_level=PROBE_SMALL_QUAL),
        probe_fn=probe,
        require_min_qualification=True,
    )
    assert FREE_TOOLS not in [e.route_id for e in receipt.shortlist]
    assert any(p.route_id == FREE_TOOLS and p.passed is False for p in receipt.probes)


def test_receipt_records_fingerprint_drops_probes_shortlist_digests() -> None:
    store = _base_store()
    receipt = build_candidate_pool(
        [FREE_TOOLS, PAID_TOOLS, UNMAPPED, FREE_NO_TOOLS],
        task="tools coding task",
        context={"tools_required": True, "language": "python"},
        metadata=store,
        health={FREE_TOOLS: RouteHealth(state=HEALTH_OK), PAID_TOOLS: RouteHealth(state=HEALTH_OK)},
        top_k=2,
    )
    payload = receipt.to_dict()
    assert payload["task_fingerprint"]["digest"].startswith("sha256:")
    assert payload["discovered_count"] == 4
    assert isinstance(payload["hard_drops"], list) and payload["hard_drops"]
    assert "probes" in payload
    assert "shortlist" in payload and len(payload["shortlist"]) <= 2
    assert payload["evidence_digest"].startswith("sha256:")
    assert payload["shortlist_digest"].startswith("sha256:")
    for entry in payload["shortlist"]:
        assert "confidence" in entry
        assert "score_features" in entry
        assert "inclusion_reason" in entry


def test_unmapped_is_hard_drop_not_ranked() -> None:
    store = _base_store()
    receipt = build_candidate_pool(
        [UNMAPPED, FREE_TOOLS],
        task="tools",
        context={"tools_required": True},
        metadata=store,
        health={FREE_TOOLS: RouteHealth(state=HEALTH_OK)},
        top_k=2,
    )
    assert any(d.route_id == UNMAPPED and d.reason == DROP_UNMAPPED for d in receipt.hard_drops)
    assert UNMAPPED not in [e.route_id for e in receipt.shortlist]


def test_missing_metadata_store_with_requirements_fail_closed() -> None:
    with pytest.raises(CandidatePoolError, match="metadata"):
        build_candidate_pool(
            [FREE_TOOLS], task="tools", context={"tools_required": True}, metadata=None, top_k=1
        )


def test_task_profile_is_canonical_candidate_pool_input() -> None:
    from verdict.task_profile import profile_task

    profile = profile_task(
        "repair parser",
        context={"tools_required": True, "task_family": "coding", "spend_policy": "free_only"},
    )
    receipt = build_candidate_pool(
        [FREE_TOOLS],
        task_profile=profile,
        metadata=_base_store(),
        health={FREE_TOOLS: RouteHealth(state=HEALTH_OK)},
        top_k=1,
    )
    assert receipt.task_fingerprint.digest == profile.digest
    assert receipt.task_fingerprint.task_family == profile.task_family
    assert receipt.task_fingerprint.required_capabilities == profile.required_capabilities


def test_material_quality_beats_latency_tie_breaker() -> None:
    strong = "strong/qualified-code"
    fast = "fast/weaker-code"
    store = _store(
        (
            _record(strong, scores=SoftScores(aa_coding=_prov(90.0))),
            _record(fast, scores=SoftScores(aa_coding=_prov(20.0))),
        )
    )
    receipt = build_candidate_pool(
        [fast, strong],
        task="fix code",
        context={"tools_required": True, "task_family": "coding"},
        metadata=store,
        health={strong: RouteHealth(state=HEALTH_OK), fast: RouteHealth(state=HEALTH_OK)},
        evidence={strong: RouteEvidence(latency_ms=5_000.0), fast: RouteEvidence(latency_ms=1.0)},
        top_k=2,
    )
    assert next(item.route_id for item in receipt.shortlist) == strong


def test_active_probes_are_closed_over_bounded_preprobe_topk() -> None:
    routes = tuple(f"provider-{index}/candidate-{index}" for index in range(8))
    store = _store(tuple(_record(route) for route in routes))
    probed: list[str] = []

    def probe(route_id: str, level: str) -> dict[str, Any]:
        probed.append(route_id)
        return {"ok": True, "qualified": True, "level": level}

    receipt = build_candidate_pool(
        routes,
        task="fix code",
        context={"tools_required": True},
        metadata=store,
        health={route: RouteHealth(state=HEALTH_OK) for route in routes},
        top_k=2,
        probe_budget=ProbeBudget(max_probes=20, max_level=PROBE_SMALL_QUAL),
        probe_fn=probe,
        require_min_qualification=True,
    )
    shortlisted = {item.route_id for item in receipt.shortlist}
    assert len(set(probed)) <= 2
    assert set(probed) == shortlisted


def test_unknown_quality_is_not_positive_evidence() -> None:
    known = "known/strong-code"
    unknown = "unknown/mystery-code"
    store = _store(
        (
            _record(known, scores=SoftScores(aa_coding=_prov(60.0))),
            _record(unknown, scores=SoftScores()),
        )
    )
    receipt = build_candidate_pool(
        [unknown, known],
        task="fix code",
        context={"tools_required": True},
        metadata=store,
        health={known: RouteHealth(state=HEALTH_OK), unknown: RouteHealth(state=HEALTH_OK)},
        top_k=2,
    )
    rows = {item.route_id: item for item in receipt.shortlist}
    assert "eval_score" not in rows[unknown].score_features
    assert rows[unknown].confidence < rows[known].confidence
    assert rows[unknown].score < rows[known].score


def test_equivalent_outcome_order_has_same_evidence_digest() -> None:
    store = _base_store()
    first = OutcomeObservation(route_id=FREE_TOOLS, task_family="coding", success=True)
    second = OutcomeObservation(route_id=PAID_TOOLS, task_family="coding", success=False)
    kwargs = dict(
        advertised=[FREE_TOOLS, PAID_TOOLS],
        task="tools required coding",
        context={"tools_required": True},
        metadata=store,
        health={FREE_TOOLS: RouteHealth(state=HEALTH_OK), PAID_TOOLS: RouteHealth(state=HEALTH_OK)},
        top_k=2,
    )
    left = build_candidate_pool(**kwargs, outcomes=[first, second])
    right = build_candidate_pool(**kwargs, outcomes=[second, first])
    assert left.evidence_digest == right.evidence_digest


def test_same_leaf_different_providers_remain_available_for_diversity() -> None:
    first = "provider-a/shared-leaf"
    second = "provider-b/shared-leaf"
    store = _store((_record(first), _record(second)))
    receipt = build_candidate_pool(
        [first, second],
        task="fix code",
        context={"tools_required": True},
        metadata=store,
        health={first: RouteHealth(state=HEALTH_OK), second: RouteHealth(state=HEALTH_OK)},
        top_k=2,
    )
    assert {item.provider for item in receipt.shortlist} == {"provider-a", "provider-b"}
