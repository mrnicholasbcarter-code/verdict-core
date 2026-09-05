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
from typing import Any

import verdict.intelligence as intel
from verdict.availability import AvailabilityCandidate, AvailabilityReport, AvailabilityState
from verdict.availability_cache import AvailabilityCache
from verdict.contracts import TaskSpec
from verdict.decision_kernel import decide
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


def test_confidence_mapping_by_state() -> None:
    """FR-003: confidence score derived from live-truth state (spec 001)."""
    report = _report(
        ("a/1", "eligible"), ("c/3", "degraded"), ("d/4", "unknown"), ("e/5", "denied")
    )
    cache = _cache(report)
    gate = EligibilityGate(cache.get, protected_fail_closed=True, allow_unverified_in_dev=True)
    candidates = [
        ModelInfo(id=mid, provider=mid.split("/", 1)[0], capability_tier=2)
        for mid in ("a/1", "c/3", "d/4", "e/5")
    ]
    result = gate.evaluate(candidates, dev_mode=True)
    confidence_by_id = {r.model_id: r.confidence for r in result.records}
    assert confidence_by_id["a/1"] == 1.0
    assert confidence_by_id["c/3"] == 0.5
    assert confidence_by_id["d/4"] == 0.0
    assert confidence_by_id["e/5"] == 0.0


def test_protected_fail_closed_driven_by_zero_confidence() -> None:
    """FR-003: protected work excludes on confidence == 0.0, not raw state text."""
    report = _report(("a/1", "unknown"))
    cache = _cache(report)
    gate = EligibilityGate(cache.get, protected_fail_closed=True)
    candidates = [ModelInfo(id="a/1", provider="a", capability_tier=2)]
    result = gate.evaluate(candidates, protected=True, dev_mode=False)
    assert not result.admitted
    assert result.records[0].confidence == 0.0


def test_late_eligibility_transition_is_authoritative_at_reevaluation() -> None:
    """FR-006: a later evaluate() call reflects the candidate's current state,
    proving eligibility is re-checked (not cached) immediately before final
    selection -- a state that flips from eligible to denied between an
    earlier check and the authoritative one is excluded."""
    state = {"value": "eligible"}
    report_holder: dict[str, AvailabilityReport] = {}

    def _dynamic_report() -> AvailabilityReport:
        report_holder["last"] = _report(("a/1", state["value"]))
        return report_holder["last"]

    cache = AvailabilityCache(source=_dynamic_report, ttl_seconds=60, stale_window_seconds=30)
    gate = EligibilityGate(cache.get, protected_fail_closed=True)
    candidates = [ModelInfo(id="a/1", provider="a", capability_tier=2)]

    first = gate.evaluate(candidates, protected=True, dev_mode=False)
    assert first.admitted and first.admitted[0].id == "a/1"

    # Candidate becomes ineligible before the final (authoritative) selection.
    state["value"] = "denied"
    cache.invalidate("a/1")
    final = gate.evaluate(candidates, protected=True, dev_mode=False)
    assert not final.admitted
    assert final.records[0].state == "denied"


def test_dev_mode_reason_surfaces_confidence_and_posture() -> None:
    """FR-007: the reason text makes the best-available admission posture and
    confidence score visible, not just the internal state string."""
    report = _report(("a/1", "unknown"))
    cache = _cache(report)
    gate = EligibilityGate(cache.get, protected_fail_closed=True, allow_unverified_in_dev=True)
    candidates = [ModelInfo(id="a/1", provider="a", capability_tier=2)]
    result = gate.evaluate(candidates, protected=False, dev_mode=True)
    record = result.records[0]
    assert record.confidence == 0.0
    assert "best-available" in record.reason
    assert "confidence=0.0" in record.reason


def test_eligibility_consistent_across_entry_points(monkeypatch: Any) -> None:
    """FR-005/US3: distinct routing entry points produce the same gate result."""
    report = _report(("a/1", "eligible"), ("b/2", "denied"))
    cache = _cache(report)
    gate = EligibilityGate(cache.get, protected_fail_closed=True, allow_unverified_in_dev=True)
    candidates = [
        ModelInfo(id=mid, provider=mid.split("/", 1)[0], capability_tier=2)
        for mid in ("a/1", "b/2")
    ]

    # Entry point one: the deterministic decision-kernel facade.
    kernel_result = decide(
        task_spec=TaskSpec(objective="same task", task_type="review"),
        policy_version="test",
        candidates=candidates,
        availability_truth={mid: report for mid in ("a/1", "b/2")},
        protected=False,
        dev_mode=True,
    )

    # Entry point two: the public IntelligenceService routing path. Its
    # candidate discovery is stubbed, but it still exercises the service's own
    # EligibilityGate evaluation before model selection.
    monkeypatch.setattr(intel, "scan", lambda task: (None, ""))
    monkeypatch.setattr(intel, "fetch_models", lambda name, cfg, ttl: candidates)

    class _TaskSpec:
        effort = "low"

    class _Plan:
        task_spec = _TaskSpec()

    svc = _service_with_gate(gate)
    svc.planner.plan = lambda task, context=None, criticality=None: _Plan()
    service_result = asyncio.run(svc.route("same task", criticality="low"))

    service_records = service_result.candidate_states
    service_admitted = {r["model_id"] for r in service_records if r["admitted"]}
    service_excluded = {
        r["model_id"]: (r["verdict"], r["state"]) for r in service_records if not r["admitted"]
    }
    kernel_excluded = {r["model_id"]: (r["verdict"], r["state"]) for r in kernel_result.exclusions}

    assert service_admitted == {m.id for m in kernel_result.admitted}
    assert service_excluded == kernel_excluded
    assert {r["model_id"]: r["confidence"] for r in service_records} == {"a/1": 1.0, "b/2": 0.0}


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
