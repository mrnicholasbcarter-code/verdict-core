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


def test_cold_start_selects_coding_metric_without_cross_fallback() -> None:
    """code_edit uses aa_coding only; high intelligence does not substitute."""
    high_code = "model/high-code"
    high_intel = "model/high-intel"
    store = _store(
        (
            _record(
                high_code, scores=SoftScores(aa_coding=_prov(90.0), aa_intelligence=_prov(10.0))
            ),
            _record(
                high_intel, scores=SoftScores(aa_coding=_prov(10.0), aa_intelligence=_prov(90.0))
            ),
        )
    )
    health = {
        high_code: RouteHealth(state=HEALTH_OK, fresh=True),
        high_intel: RouteHealth(state=HEALTH_OK, fresh=True),
    }
    receipt = build_candidate_pool(
        [high_code, high_intel],
        task="implement the feature with tests",
        metadata=store,
        health=health,
        outcomes=(),
        top_k=2,
    )
    assert [e.route_id for e in receipt.shortlist] == [high_code, high_intel]
    winner = receipt.shortlist[0]
    assert winner.eval_prior_kind == "metadata"
    assert winner.eval_prior_metric == "aa_coding"
    assert winner.eval_prior_value == 90.0
    assert winner.eval_prior_provenance is not None
    assert winner.eval_prior_provenance["source"] == SOURCE_MODELS_DEV
    assert winner.eval_prior_metadata_id == high_code


def test_cold_start_docs_selects_intelligence_metric() -> None:
    high_code = "model/high-code"
    high_intel = "model/high-intel"
    store = _store(
        (
            _record(
                high_code, scores=SoftScores(aa_coding=_prov(90.0), aa_intelligence=_prov(10.0))
            ),
            _record(
                high_intel, scores=SoftScores(aa_coding=_prov(10.0), aa_intelligence=_prov(90.0))
            ),
        )
    )
    health = {
        high_code: RouteHealth(state=HEALTH_OK, fresh=True),
        high_intel: RouteHealth(state=HEALTH_OK, fresh=True),
    }
    receipt = build_candidate_pool(
        [high_code, high_intel],
        task="summarize the docs for the operator",
        metadata=store,
        health=health,
        outcomes=(),
        top_k=2,
    )
    assert receipt.shortlist[0].route_id == high_intel
    assert receipt.shortlist[0].eval_prior_metric == "aa_intelligence"
    assert receipt.shortlist[0].eval_prior_value == 90.0


def test_missing_coding_prior_is_absent_even_with_high_intelligence() -> None:
    only_intel = "model/intel-only"
    store = _store((_record(only_intel, scores=SoftScores(aa_intelligence=_prov(95.0))),))
    receipt = build_candidate_pool(
        [only_intel],
        task="implement a bug fix",
        metadata=store,
        health={only_intel: RouteHealth(state=HEALTH_OK, fresh=True)},
        outcomes=(),
        top_k=1,
    )
    entry = receipt.shortlist[0]
    assert entry.eval_prior_kind == "absent"
    assert entry.eval_prior_metric == "aa_coding"
    assert entry.eval_prior_value is None
    assert "eval_score" not in entry.score_features


def test_zero_coding_is_evidence_not_missing() -> None:
    zero = "model/zero-code"
    store = _store((_record(zero, scores=SoftScores(aa_coding=_prov(0.0))),))
    receipt = build_candidate_pool(
        [zero],
        task="refactor the module",
        metadata=store,
        health={zero: RouteHealth(state=HEALTH_OK, fresh=True)},
        outcomes=(),
        top_k=1,
    )
    entry = receipt.shortlist[0]
    assert entry.eval_prior_kind == "metadata"
    assert entry.eval_prior_value == 0.0
    assert entry.score_features["eval_score"] == 0.0


def test_caller_eval_score_wins_and_is_not_attributed_to_metadata() -> None:
    route = "model/with-aa"
    store = _store(
        (_record(route, scores=SoftScores(aa_coding=_prov(80.0), aa_intelligence=_prov(80.0))),)
    )
    receipt = build_candidate_pool(
        [route],
        task="implement tests",
        metadata=store,
        health={route: RouteHealth(state=HEALTH_OK, fresh=True)},
        evidence={route: RouteEvidence(eval_score=15.0)},
        outcomes=(),
        top_k=1,
    )
    entry = receipt.shortlist[0]
    assert entry.eval_prior_kind == "caller_evidence"
    assert entry.eval_prior_metric is None
    assert entry.eval_prior_value == 15.0
    assert entry.eval_prior_provenance is None
    assert abs(entry.score_features["eval_score"] - 0.15) < 1e-9


# ── BOD-432: trusted live-probe receipt (S02/A25) ───────────────────────────

PROBE_NOW = 1_800_000_000.0


def _probed(route_id: str, **overrides: Any) -> dict[str, Any]:
    candidate: dict[str, Any] = {
        "route_id": route_id,
        "eligible": True,
        "probe_observed_at": PROBE_NOW - 10,
        "probe_headers": {"x-omniroute-model": route_id.split("/", 1)[-1]},
    }
    candidate.update(overrides)
    return candidate


def _live_receipt(*candidates: dict[str, Any], now: Any = PROBE_NOW) -> Any:
    from verdict.candidate_pool import build_live_probe_receipt

    return build_live_probe_receipt(objective="fix", candidates=candidates, now=now)


def test_live_probe_receipt_keeps_server_rank_order_for_fresh_probes() -> None:
    receipt = _live_receipt(_probed("free/a"), _probed("free/b"))
    assert [e.route_id for e in receipt.shortlist] == ["free/a", "free/b"]
    assert all(p.passed for p in receipt.probes)


def test_live_probe_receipt_accepts_aware_datetime_now() -> None:
    now = datetime.fromtimestamp(PROBE_NOW, tz=timezone.utc)
    assert _live_receipt(_probed("free/a"), now=now).shortlist


@pytest.mark.parametrize("age", [-1.0, 3600.0 + 1, 86_400.0])
def test_live_probe_receipt_rejects_future_and_stale_probes(age: float) -> None:
    with pytest.raises(CandidatePoolError, match="probe_observed_at"):
        _live_receipt(_probed("free/a", probe_observed_at=PROBE_NOW - age))


def test_live_probe_receipt_accepts_probe_exactly_at_the_age_bound() -> None:
    assert _live_receipt(_probed("free/a", probe_observed_at=PROBE_NOW - 3600.0)).shortlist


@pytest.mark.parametrize("headers", [{}, {"x-omniroute-response-cost": "0"}])
def test_live_probe_receipt_requires_the_served_model_header(headers: dict[str, str]) -> None:
    with pytest.raises(CandidatePoolError, match="x-omniroute-model"):
        _live_receipt(_probed("free/a", probe_headers=headers))


def test_live_probe_receipt_never_records_unpassed_candidates_as_passed() -> None:
    bad = _probed("free/bad", eligible=False, certification_state="stale", probe_headers={})
    receipt = _live_receipt(bad, _probed("free/ok"))
    assert [e.route_id for e in receipt.shortlist] == ["free/ok"]
    by_route = {p.route_id: p for p in receipt.probes}
    assert by_route["free/ok"].passed is True
    assert by_route["free/bad"].passed is False
    assert by_route["free/bad"].detail


def test_live_probe_receipt_without_a_passed_candidate_raises() -> None:
    with pytest.raises(CandidatePoolError, match="passed"):
        _live_receipt(_probed("free/bad", eligible=False))


def test_demoted_routes_sort_after_non_demoted_admitted() -> None:
    """BOD-438 S10 slice 10.3: demotion only reorders the admitted set."""
    from datetime import datetime, timedelta, timezone

    weak = "model/weak-history"
    mid = "model/no-history"
    store = _store((_record(weak, scores=SoftScores()), _record(mid, scores=SoftScores())))
    fp = fingerprint_task("implement the billing export")
    as_of = datetime(2026, 10, 19, 6, 0, 0, tzinfo=timezone.utc)
    recent = as_of - timedelta(hours=1)

    # Three independently verified, all-failed observations: score < 0.4 with
    # observed unique n=3 -> demoted. Each has a distinct observation_id so
    # they are not deduplicated away.
    weak_outcomes = [
        OutcomeObservation(
            route_id=weak,
            task_family=fp.task_family,
            success=False,
            observed_at=recent,
            quality_credit=0.0,
            observation_id=f"run-weak-{i}:a:1",
        )
        for i in range(3)
    ]

    receipt = build_candidate_pool(
        [weak, mid],
        task_fingerprint=fp,
        metadata=store,
        health={
            weak: RouteHealth(state=HEALTH_OK, fresh=True),
            mid: RouteHealth(state=HEALTH_OK, fresh=True),
        },
        evidence={weak: RouteEvidence(eval_score=100.0), mid: RouteEvidence(eval_score=60.0)},
        outcomes=weak_outcomes,
        as_of=as_of,
        top_k=2,
    )

    ids = [e.route_id for e in receipt.shortlist]
    # Both routes stay admitted — demotion never drops a member.
    assert set(ids) == {weak, mid}
    # weak's fused score (0.40*0.2 task_similarity + 0.25*1.0 eval + 0.15 health
    # = 0.48) is higher than mid's (0.25*0.6 eval + 0.15 health = 0.30), but
    # demotion puts it after mid in the admitted order.
    weak_entry = next(e for e in receipt.shortlist if e.route_id == weak)
    mid_entry = next(e for e in receipt.shortlist if e.route_id == mid)
    assert weak_entry.score > mid_entry.score
    assert ids == [mid, weak]


def test_price_only_change_does_not_affect_shortlist_order_after_demotion() -> None:
    """A25: price/cost changes never affect rank, demoted or not."""
    from datetime import datetime, timedelta, timezone

    weak = "model/weak-history-2"
    mid = "model/no-history-2"
    store = _store((_record(weak, scores=SoftScores()), _record(mid, scores=SoftScores())))
    fp = fingerprint_task("implement the billing export v2")
    as_of = datetime(2026, 10, 19, 6, 0, 0, tzinfo=timezone.utc)
    recent = as_of - timedelta(hours=1)
    weak_outcomes = [
        OutcomeObservation(
            route_id=weak,
            task_family=fp.task_family,
            success=False,
            observed_at=recent,
            quality_credit=0.0,
            observation_id=f"run-weak2-{i}:a:1",
        )
        for i in range(3)
    ]

    def _build(cost: float | None) -> list[str]:
        receipt = build_candidate_pool(
            [weak, mid],
            task_fingerprint=fp,
            metadata=store,
            health={
                weak: RouteHealth(state=HEALTH_OK, fresh=True),
                mid: RouteHealth(state=HEALTH_OK, fresh=True),
            },
            evidence={
                weak: RouteEvidence(eval_score=100.0, cost_per_million=cost),
                mid: RouteEvidence(eval_score=60.0, cost_per_million=cost),
            },
            outcomes=weak_outcomes,
            as_of=as_of,
            top_k=2,
        )
        return [e.route_id for e in receipt.shortlist]

    cheap = _build(1.0)
    expensive = _build(1000.0)
    assert cheap == expensive == [mid, weak]
