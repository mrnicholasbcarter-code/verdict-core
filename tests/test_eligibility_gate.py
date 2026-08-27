"""Integration + invariant tests for issue #57 / #72 / #73 eligibility gate.

These tests prove the AC:
- Candidate filtering occurs before ranking in every route path (router, Gate,
  IntelligenceService) and no ranker can reintroduce an excluded candidate.
- Protected work fails closed when runtime truth is absent.
- The explain endpoint surfaces the complete pre-ranking eligible set and
  per-candidate exclusions from the same authority the router uses.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import verdict.intelligence as intel
from verdict.availability import AvailabilityCandidate, AvailabilityReport, AvailabilityState
from verdict.availability_cache import AvailabilityCache
from verdict.eligibility import EligibilityGate, EligibilityVerdict
from verdict.intelligence import IntelligenceService
from verdict.models import ModelInfo, ProviderConfig, RoutingDecision
from verdict.router import select_best_model


def _candidate(model_id: str, state: str, tier: int = 2) -> AvailabilityCandidate:
    return AvailabilityCandidate(
        model=ModelInfo(id=model_id, provider=model_id.split("/", 1)[0], capability_tier=tier),
        state=AvailabilityState(state),
        reasons=(f"probe:{state}",),
        source="verdict:probe",
    )


def _report(*states: tuple[str, str]) -> AvailabilityReport:
    """Build a report keyed by (model_id, availability_state)."""
    candidates = [_candidate(mid, st) for mid, st in states]
    eligible = [
        c for c in candidates if c.state in {AvailabilityState.ELIGIBLE, AvailabilityState.READY}
    ]
    return AvailabilityReport(tuple(candidates), tuple(eligible), "cache", 60)


def _cache(report: AvailabilityReport) -> AvailabilityCache:
    cache = AvailabilityCache(source=lambda: report, ttl_seconds=60, stale_window_seconds=30)
    # Populate eagerly so get() returns a fresh entry.
    for mid, _ in [("a/1", "eligible"), ("b/2", "ready"), ("c/3", "denied"), ("d/4", "unknown")]:
        cache.get(mid)
    return cache


def _service_with_gate(gate: EligibilityGate | None) -> IntelligenceService:
    return IntelligenceService(
        primary_model="anthropic/claude-3-opus-20240229",
        providers={"a": ProviderConfig(base_url="https://a.example/v1", priority=1)},
        profile="development",
        log_path="",
        log_full_task=False,
        discovery_ttl=60,
        eligibility_gate=gate,
    )


def test_gate_excludes_denied_unconditionally() -> None:
    """Denied candidates are excluded in both dev and protected modes."""
    report = _report(("a/1", "eligible"), ("b/2", "denied"))
    cache = _cache(report)
    gate = EligibilityGate(cache.get, protected_fail_closed=True, allow_unverified_in_dev=True)
    candidates = [
        ModelInfo(id="a/1", provider="a", capability_tier=2),
        ModelInfo(id="b/2", provider="b", capability_tier=2),
    ]
    for kwargs in ({"dev_mode": True}, {"protected": True, "dev_mode": False}):
        result = gate.evaluate(candidates, **kwargs)
        admitted_ids = {m.id for m in result.admitted}
        assert admitted_ids == {"a/1"}, kwargs
        assert all(not r.admitted for r in result.exclusions)
        assert {r.model_id for r in result.exclusions} == {"b/2"}


def test_ranker_cannot_reintroduce_excluded_candidate() -> None:
    """The gate result is the ONLY input to ranking, so exclusions stick."""
    report = _report(("a/1", "eligible"), ("b/2", "denied"))
    cache = _cache(report)
    gate = EligibilityGate(cache.get, protected_fail_closed=True)
    candidates = [
        ModelInfo(id="a/1", provider="a", capability_tier=2),
        ModelInfo(id="b/2", provider="b", capability_tier=1),  # higher priority tier
    ]
    filtered = gate.evaluate(candidates, dev_mode=True).eligible
    chosen, _ = select_best_model(
        filtered,
        tier=3,
        configs={
            "a": ProviderConfig(base_url="https://a.example/v1", priority=1),
            "b": ProviderConfig(base_url="https://b.example/v1", priority=2),
        },
    )
    assert chosen is not None
    assert chosen.id == "a/1"  # never b/2 even though it is a "better" tier
    assert chosen.id != "b/2"


def test_protected_work_fails_closed_when_truth_absent() -> None:
    """Protected (tier 0) routing drops candidates with unknown runtime truth."""
    report = _report(("a/1", "unknown"), ("b/2", "unknown"))
    cache = _cache(report)
    gate = EligibilityGate(cache.get, protected_fail_closed=True)
    candidates = [
        ModelInfo(id="a/1", provider="a", capability_tier=2),
        ModelInfo(id="b/2", provider="b", capability_tier=2),
    ]
    result = gate.evaluate(candidates, protected=True, dev_mode=False)
    assert not result.admitted  # nothing verified -> fail closed
    assert all(r.verdict == EligibilityVerdict.RUNTIME_TRUTH_ABSENT for r in result.exclusions)


def test_dev_mode_admits_unverified_when_not_protected() -> None:
    report = _report(("a/1", "unknown"))
    cache = _cache(report)
    gate = EligibilityGate(cache.get, protected_fail_closed=True, allow_unverified_in_dev=True)
    candidates = [ModelInfo(id="a/1", provider="a", capability_tier=2)]
    result = gate.evaluate(candidates, protected=False, dev_mode=True)
    assert result.admitted[0].id == "a/1"
    assert result.records[0].verdict == EligibilityVerdict.NOT_LIVE_ELIGIBLE


def test_intelligence_route_filters_before_ranking(monkeypatch: Any) -> None:
    """End-to-end: IntelligenceService.route excludes denied candidates."""
    report = _report(("a/1", "eligible"), ("b/2", "denied"))
    cache = _cache(report)
    gate = EligibilityGate(cache.get, protected_fail_closed=True, allow_unverified_in_dev=True)

    # Patch names bound inside intelligence.route (it imports them
    # locally, so patching the source modules would not take effect).
    monkeypatch.setattr(intel, "scan", lambda task: (None, ""))
    monkeypatch.setattr(
        intel,
        "fetch_models",
        lambda name, cfg, ttl: [
            ModelInfo(id="a/1", provider="a", capability_tier=0),
            ModelInfo(id="b/2", provider="b", capability_tier=0),
        ],
    )

    # Stub planner "write test" not auto-escalated critical
    # effort (which would force final_tier=0 and route to primary by design).
    class _TaskSpec:
        effort = "low"

    class _Plan:
        task_spec = _TaskSpec()

    svc = _service_with_gate(gate)
    svc.planner.plan = lambda task, context=None, criticality=None: _Plan()

    # low criticality non-critical tier, verified eligible model is
    # actually selected (critical tier always routes to primary by design).
    dec = asyncio.run(svc.route("write test", criticality="low"))
    assert isinstance(dec, RoutingDecision)
    # Both candidates tier-0 (so tier filtering not decide); only the
    # availability gate differentiates them. denied model must win.
    assert dec.model == "a/1"
    assert dec.candidate_states, "candidate_states must carry gate records"


def test_explain_surfaces_eligible_set_and_exclusions(monkeypatch: Any) -> None:
    """Issue #73: explain endpoint exposes the full pre-ranking eligible set."""
    from fastapi.testclient import TestClient

    import verdict.api as api

    report = _report(("a/1", "eligible"), ("b/2", "denied"))
    cache = _cache(report)
    gate = EligibilityGate(cache.get, protected_fail_closed=True, allow_unverified_in_dev=True)
    monkeypatch.setattr(api, "_build_availability_cache", lambda: (cache, gate))
    monkeypatch.setenv("LLMGATE_AUTH_TOKEN", "test-token")

    with TestClient(api.app) as client:
        resp = client.get("/v1/route/explain", headers={"Authorization": "Bearer test-token"})
    assert resp.status_code == 200
    body = resp.json()
    # The denied candidate must never appear in the eligible set; the gate's
    # exclusions list must name it explicitly (issue #73).
    assert "b/2" not in body["eligible_set"]
    assert {e["model_id"] for e in body["exclusions"]} == {"b/2"}


def test_explain_per_model_carries_eligibility(monkeypatch: Any) -> None:
    from fastapi.testclient import TestClient

    import verdict.api as api

    report = _report(("a/1", "eligible"), ("b/2", "denied"))
    cache = _cache(report)
    gate = EligibilityGate(cache.get, protected_fail_closed=True, allow_unverified_in_dev=True)
    monkeypatch.setattr(api, "_build_availability_cache", lambda: (cache, gate))
    monkeypatch.setenv("LLMGATE_AUTH_TOKEN", "test-token")

    with TestClient(api.app) as client:
        resp = client.get(
            "/v1/route/explain?model_id=a/1", headers={"Authorization": "Bearer test-token"}
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["eligibility"]["model_id"] == "a/1"
    assert body["eligible"] is True


def test_degraded_is_not_auto_admitted_unless_adapter_eligible_set_includes_it() -> None:
    degraded = _candidate("aa/degraded", "degraded")
    report = AvailabilityReport((degraded,), (), "cache", 60)
    cache = AvailabilityCache(source=lambda: report, ttl_seconds=60, stale_window_seconds=30)
    cache.get("aa/degraded")
    gate = EligibilityGate(cache.get, protected_fail_closed=True, allow_unverified_in_dev=False)
    candidates = [ModelInfo(id="aa/degraded", provider="aa", capability_tier=2)]

    excluded = gate.evaluate(candidates, protected=True, dev_mode=False)
    assert [model.id for model in excluded.admitted] == []
    assert excluded.records[0].admitted is False

    admitted_report = AvailabilityReport((degraded,), (degraded,), "cache", 60)
    admitted_cache = AvailabilityCache(
        source=lambda: admitted_report, ttl_seconds=60, stale_window_seconds=30
    )
    admitted_cache.get("aa/degraded")
    admitted_gate = EligibilityGate(
        admitted_cache.get, protected_fail_closed=True, allow_unverified_in_dev=False
    )
    included = admitted_gate.evaluate(candidates, protected=True, dev_mode=False)
    assert [model.id for model in included.admitted] == ["aa/degraded"]
    assert included.records[0].reason == "adapter eligibility verdict"


def test_eligibility_matches_exact_model_id_not_provider_suffix() -> None:
    sibling = _candidate("alt/model", "eligible")
    report = AvailabilityReport((sibling,), (sibling,), "cache", 60)
    cache = AvailabilityCache(source=lambda: report, ttl_seconds=60, stale_window_seconds=30)
    cache.get("other/model")
    gate = EligibilityGate(cache.get, protected_fail_closed=True, allow_unverified_in_dev=False)

    result = gate.evaluate(
        [ModelInfo(id="other/model", provider="other", capability_tier=2)],
        protected=True,
        dev_mode=False,
    )

    assert result.admitted == []
    assert result.records[0].state == "unknown"
    assert result.records[0].admitted is False


def test_shadow_learning_uses_trusted_labels_and_leaves_gate_unchanged() -> None:
    from verdict.autodev_run import shadow_learning_report

    digest = "sha256:" + "11" * 32
    episodes = [
        {
            "packet_integrity_digest": digest,
            "actual_identity": "cheap/a",
            "worker_self_report": {"outcome": "applied", "role": "advisory"},
            "trusted_verification": {"decided": False, "role": "deciding"},
        },
        {
            "packet_integrity_digest": digest,
            "actual_identity": "cheap/a",
            "worker_self_report": {"outcome": "error", "role": "advisory"},
            "trusted_verification": {"decided": True, "role": "deciding"},
        },
    ]
    shadow = shadow_learning_report(episodes)
    assert shadow["labeled_from"] == "trusted_verification"
    assert shadow["advisory_ranking"][0]["wins"] == 1
    assert shadow["advisory_ranking"][0]["losses"] == 1
    assert shadow["admission_unchanged"] is True

    report = _report(("a/1", "eligible"), ("b/2", "denied"))
    cache = _cache(report)
    gate = EligibilityGate(cache.get, protected_fail_closed=True, allow_unverified_in_dev=True)
    candidates = [
        ModelInfo(id="a/1", provider="a", capability_tier=2),
        ModelInfo(id="b/2", provider="b", capability_tier=2),
    ]
    before = {m.id for m in gate.evaluate(candidates, dev_mode=True).admitted}
    _ = shadow_learning_report(episodes)
    after = {m.id for m in gate.evaluate(candidates, dev_mode=True).admitted}
    assert before == after == {"a/1"}
    import verdict.eligibility as eligibility_mod

    assert "shadow_learning_report" not in Path(eligibility_mod.__file__).read_text(
        encoding="utf-8"
    )


def test_shadow_report_binds_packet_digest_and_ignores_self_report_only() -> None:
    from verdict.autodev_run import shadow_learning_report

    digest = "sha256:" + "ab" * 32
    labeled = shadow_learning_report(
        [
            {
                "packet_integrity_digest": digest,
                "actual_identity": "a/1",
                "worker_self_report": {"outcome": "error", "role": "advisory"},
                "trusted_verification": {"decided": True, "role": "deciding"},
            }
        ]
    )
    assert labeled["source_binding"] == digest
    assert labeled["episode_count"] == 1
    ignored = shadow_learning_report(
        [
            {
                "packet_integrity_digest": digest,
                "actual_identity": "a/1",
                "worker_self_report": {"outcome": "applied", "role": "advisory"},
                "trusted_verification": {"decided": True, "role": "advisory"},
            },
            {
                "packet_integrity_digest": digest,
                "actual_identity": "a/1",
                "worker_self_report": {"outcome": "applied", "role": "advisory"},
            },
        ]
    )
    assert ignored["episode_count"] == 0
    assert ignored["advisory_ranking"] == []
    assert ignored["source_binding"] == digest


def test_shadow_canary_is_bounded_and_rollback_restores_baseline_choice() -> None:
    from verdict.autodev_run import (
        apply_shadow_canary,
        rollback_shadow_canary,
        shadow_learning_report,
    )

    admitted = ("a/1", "b/2")
    digest = "sha256:" + "22" * 32
    report = shadow_learning_report(
        [
            {
                "packet_integrity_digest": digest,
                "actual_identity": "b/2",
                "trusted_verification": {"decided": True, "role": "deciding"},
            },
            {
                "packet_integrity_digest": digest,
                "actual_identity": "b/2",
                "trusted_verification": {"decided": True, "role": "deciding"},
            },
            {
                "packet_integrity_digest": digest,
                "actual_identity": "a/1",
                "trusted_verification": {"decided": False, "role": "deciding"},
            },
        ]
    )
    canary = apply_shadow_canary(admitted, report)
    assert canary["chosen"] == "b/2"
    assert canary["baseline"] == "a/1"
    assert canary["active"] is True
    assert canary["improvement"] is True
    rolled = rollback_shadow_canary(canary)
    assert rolled["chosen"] == "a/1"
    assert rolled["active"] is False
    assert rolled.get("improvement") is False
    gate = EligibilityGate(
        _cache(_report(("a/1", "eligible"), ("b/2", "eligible"))).get,
        protected_fail_closed=True,
        allow_unverified_in_dev=True,
    )
    candidates = [
        ModelInfo(id="a/1", provider="a", capability_tier=2),
        ModelInfo(id="b/2", provider="b", capability_tier=2),
    ]
    before = {m.id for m in gate.evaluate(candidates, dev_mode=True).admitted}
    _ = apply_shadow_canary(admitted, report)
    after = {m.id for m in gate.evaluate(candidates, dev_mode=True).admitted}
    assert before == after == {"a/1", "b/2"}


def test_canary_reports_truthful_no_improvement_when_chosen_is_baseline() -> None:
    from verdict.autodev_run import apply_shadow_canary, shadow_learning_report

    digest = "sha256:" + "44" * 32
    report = shadow_learning_report(
        [
            {
                "packet_integrity_digest": digest,
                "actual_identity": "a/1",
                "trusted_verification": {"decided": True, "role": "deciding"},
            },
            {
                "packet_integrity_digest": digest,
                "actual_identity": "b/2",
                "trusted_verification": {"decided": False, "role": "deciding"},
            },
        ]
    )
    canary = apply_shadow_canary(("a/1", "b/2"), report)
    assert canary["chosen"] == "a/1"
    assert canary["baseline"] == "a/1"
    assert canary["improvement"] is False


def test_shadow_canary_does_not_require_a_vendor_brand() -> None:
    from verdict.autodev_run import apply_shadow_canary, shadow_learning_report

    admitted = ("oc/hy3-free", "local/llama")
    report = shadow_learning_report(
        [
            {
                "packet_integrity_digest": "sha256:" + "ef" * 32,
                "actual_identity": "local/llama",
                "trusted_verification": {"decided": True, "role": "deciding"},
            }
        ]
    )
    canary = apply_shadow_canary(admitted, report)
    assert canary["chosen"] == "local/llama"
    assert canary["baseline"] == "oc/hy3-free"


def test_shadow_does_not_rank_episodes_without_a_single_packet_digest() -> None:
    from verdict.autodev_run import shadow_learning_report

    unbound = shadow_learning_report(
        [{"actual_identity": "a/1", "trusted_verification": {"decided": True, "role": "deciding"}}]
    )
    assert unbound["episode_count"] == 0
    assert unbound["advisory_ranking"] == []
    assert unbound["source_binding"] is None
    mixed = shadow_learning_report(
        [
            {
                "packet_integrity_digest": "sha256:" + "aa" * 32,
                "actual_identity": "a/1",
                "trusted_verification": {"decided": True, "role": "deciding"},
            },
            {
                "packet_integrity_digest": "sha256:" + "bb" * 32,
                "actual_identity": "b/2",
                "trusted_verification": {"decided": True, "role": "deciding"},
            },
        ]
    )
    assert mixed["episode_count"] == 0
    assert mixed["advisory_ranking"] == []
    assert mixed["source_binding"] is None
