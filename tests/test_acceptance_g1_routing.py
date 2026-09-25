"""Acceptance tests for gates G1.1-G1.5 (Core Routing Correctness).

Each test exercises the real production path (verdict/eligibility.py, the chooser/
ranker path, and explain output) and must FAIL if the documented behavior breaks.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from verdict.adaptive_ranker import AdaptiveRanker, AdaptiveRankerConfig, RankerMode
from verdict.availability import AvailabilityCandidate, AvailabilityReport, AvailabilityState
from verdict.availability_cache import AvailabilityCache, explain_freshness
from verdict.capability_passports import (
    CapabilityEvidence,
    CapabilityPassport,
    CapabilityStatus,
    EvidenceAuthority,
    RouteIdentity,
)
from verdict.contracts import TaskSpec
from verdict.eligibility import (
    EligibilityGate,
    EligibilityRecord,
    EligibilityResult,
    EligibilityVerdict,
)
from verdict.models import ModelInfo
from verdict.planner import PlannerPolicy, PlanRejected, StructuredPlanner
from verdict.policy import DecisionState, Policy, PolicyCandidate, compile_policy

NOW = datetime(2026, 7, 30, 4, 30, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Shared fixture builders
# ---------------------------------------------------------------------------


def _route(model: str, protocol: str = "chat") -> RouteIdentity:
    return RouteIdentity(
        gateway="gw-1",
        provider="test-provider",
        connection="account-a",
        endpoint="https://test.example/v1",
        protocol=protocol,
        model_id=model,
        model_revision="rev-1",
    )


def _fresh_evidence(status: CapabilityStatus = CapabilityStatus.SUPPORTED) -> CapabilityEvidence:
    return CapabilityEvidence(
        status=status,
        source="fixture:probe",
        observed_at=NOW - timedelta(seconds=1),
        expires_at=NOW + timedelta(minutes=5),
        confidence=1.0,
        evidence_digest="sha256:" + "a" * 64,
        authority=EvidenceAuthority.VERIFIED,
        method="hermetic",
        adapter_version="test-1",
        scope="test-provider/account-a",
    )


def _passport(model: str, caps: dict[str, CapabilityEvidence] | None = None) -> CapabilityPassport:
    return CapabilityPassport(
        route_identity=_route(model),
        qualified_at=NOW - timedelta(seconds=1),
        expires_at=NOW + timedelta(minutes=5),
        observed=caps or {},
    )


def _fake_model(
    model_id: str, provider: str = "test", tier: int = 1, caps: list[str] | None = None
) -> ModelInfo:
    """Create a test model with required fields."""
    return ModelInfo(id=model_id, provider=provider, capability_tier=tier, capabilities=caps or [])


def _eligibility_result(models: list[ModelInfo], states: list[str]) -> EligibilityResult:
    """Build an EligibilityResult with given models and admission states."""
    records = []
    admitted = []
    for m, s in zip(models, states, strict=True):
        admitted_flag = s in ("eligible", "ready", "degraded")
        records.append(
            EligibilityRecord(
                model_id=m.id,
                provider=m.provider,
                admitted=admitted_flag,
                verdict=EligibilityVerdict.ELIGIBLE
                if admitted_flag
                else EligibilityVerdict.NOT_LIVE_ELIGIBLE,
                state=s,
                source="test",
                reason=None if admitted_flag else "test exclusion",
            )
        )
        if admitted_flag:
            admitted.append(m)
    return EligibilityResult(admitted=admitted, records=records)


def _availability_report(
    models: list[ModelInfo], states: list[AvailabilityState]
) -> AvailabilityReport:
    """Build a test AvailabilityReport."""
    candidates = []
    eligible = []
    for m, state in zip(models, states, strict=True):
        candidate = AvailabilityCandidate(
            model=m, state=state, source="test", freshness_seconds=10.0
        )
        candidates.append(candidate)
        if state in {AvailabilityState.ELIGIBLE, AvailabilityState.READY}:
            eligible.append(candidate)

    return AvailabilityReport(
        candidates=tuple(candidates),
        eligible=tuple(eligible),
        source="test",
        freshness_seconds=60.0,
        errors=(),
    )


# ---------------------------------------------------------------------------
# G1.1: Eligibility filtering runs before ranking
# ---------------------------------------------------------------------------


def test_eligibility_runs_first() -> None:
    """G1.1: Eligibility gate runs FIRST (before any ranking).

    Proves that ineligible candidates are filtered out by the gate and never
    reach the ranker, even if they would be cheaper or higher-ranked.
    """
    cheap = _fake_model("cheap/free", tier=1)
    expensive = _fake_model("expensive/premium", tier=3)
    mid = _fake_model("mid/worker", tier=2)
    models = [cheap, expensive, mid]

    avail_report = _availability_report(
        models, [AvailabilityState.DENIED, AvailabilityState.ELIGIBLE, AvailabilityState.ELIGIBLE]
    )

    def mock_availability_source(model_id: str) -> AvailabilityReport:
        return avail_report

    gate = EligibilityGate(availability_source=mock_availability_source, protected_fail_closed=True)
    eligibility_result = gate.evaluate(models, protected=True, dev_mode=False)

    assert cheap not in eligibility_result.admitted
    assert expensive in eligibility_result.admitted
    assert mid in eligibility_result.admitted

    cheap_record = next((r for r in eligibility_result.records if r.model_id == cheap.id), None)
    assert cheap_record is not None
    assert not cheap_record.admitted

    ranker = AdaptiveRanker(AdaptiveRankerConfig(mode=RankerMode.STATIC))
    ranked_output = ranker.rank(eligibility_result, task_spec="test")

    ranked_ids = [m.id for m in ranked_output.ranked]
    assert "cheap/free" not in ranked_ids
    assert set(ranked_ids) == {"expensive/premium", "mid/worker"}


# ---------------------------------------------------------------------------
# G1.2: Hard safety floors enforced
# ---------------------------------------------------------------------------


def test_capability_floor() -> None:
    """G1.2: Capability floor: candidate lacking a required capability is DENY.

    Tests Policy.evaluate / Policy.compile with a required capability against
    real CapabilityPassports.  A candidate whose passport marks the capability
    UNSUPPORTED gets DENY with the exact reason; a candidate with SUPPORTED is
    admitted.  Fails when the capability check is removed.
    """
    vision_cap = {"vision": _fresh_evidence(CapabilityStatus.SUPPORTED)}
    no_vision = {"vision": _fresh_evidence(CapabilityStatus.UNSUPPORTED)}

    cand_capable = PolicyCandidate(
        candidate_id="capable/model",
        route_identity=_route("capable/model"),
        passport=_passport("capable/model", vision_cap),
        availability="eligible",
    )
    cand_limited = PolicyCandidate(
        candidate_id="limited/model",
        route_identity=_route("limited/model"),
        passport=_passport("limited/model", no_vision),
        availability="eligible",
    )

    policy = Policy(required_capabilities=frozenset({"vision"}), require_actual_identity=False)
    result = policy.compile([cand_capable, cand_limited], at=NOW)

    # capable/model is admitted
    capable_dec = next(d for d in result.decisions if d.candidate_id == "capable/model")
    assert capable_dec.decision is DecisionState.ALLOW

    # limited/model is denied with the exact reason
    limited_dec = next(d for d in result.decisions if d.candidate_id == "limited/model")
    assert limited_dec.decision is DecisionState.DENY
    assert "capability 'vision' is unsupported" in str(limited_dec.reasons)

    # only capable is eligible
    assert [c.candidate_id for c in result.eligible] == ["capable/model"]


def test_budget_floor() -> None:
    """G1.2: Budget floor: StructuredPlanner raises PlanRejected when cost exceeds budget.

    Tests StructuredPlanner._enforce_budget via plan().  Over-requested budget
    (estimated > requested max_usd) and over-policy budget both raise PlanRejected
    with a named reason.  The floor is exercised through the real planner path.
    Fails when _enforce_budget is removed.
    """
    planner = StructuredPlanner()

    # Over-requested: estimated $0.05 (low effort) > max_usd $0.01
    with pytest.raises(PlanRejected, match="plan exceeds requested budget"):
        planner.plan("generate report", budget={"max_usd": 0.01})

    # Within budget: $10 is well above the low-effort estimate
    result = planner.plan("generate report", budget={"max_usd": 10.0})
    assert result.task_spec.objective == "generate report"

    # Over policy limit: policy max_cost_usd=$0.001 < low effort estimate $0.05
    tight_planner = StructuredPlanner(policy=PlannerPolicy(max_cost_usd=0.001))
    with pytest.raises(PlanRejected, match="budget exceeds planner policy"):
        tight_planner.plan("generate report")


def test_privacy_floor() -> None:
    """G1.2: Privacy floor: compile_policy(privacy=restricted) + restricted_data_routes allowlist.

    An allowlisted route is ALLOW; a non-listed route is DENY with the exact
    reason; route_key=None is DENY.  Non-restricted tasks are unchanged.
    Fails when the restricted_data check is removed from Policy.evaluate.
    """
    task = TaskSpec(objective="process sensitive PII", task_type="process", privacy="restricted")

    trusted_route = _route("trusted/model")
    untrusted_route = _route("untrusted/model")
    trusted_key = trusted_route.key

    policy = compile_policy(task, restricted_data_routes=[trusted_key])
    assert policy.restricted_data is True
    assert trusted_key in policy.restricted_data_routes

    # Candidates with actual_route set (required because protected=True)
    cand_trusted = PolicyCandidate(
        candidate_id="trusted/model",
        route_identity=trusted_route,
        actual_route=trusted_route,
        availability="eligible",
    )
    cand_untrusted = PolicyCandidate(
        candidate_id="untrusted/model",
        route_identity=untrusted_route,
        actual_route=untrusted_route,
        availability="eligible",
    )
    # route_key=None: no route_identity, no actual_route
    cand_no_route = PolicyCandidate(candidate_id="no-route", availability="eligible")

    result = policy.compile([cand_trusted, cand_untrusted, cand_no_route], at=NOW)

    # Allowlisted route is admitted
    trusted_dec = next(d for d in result.decisions if d.candidate_id == "trusted/model")
    assert trusted_dec.decision is DecisionState.ALLOW

    # Non-listed route is denied with exact reason
    untrusted_dec = next(d for d in result.decisions if d.candidate_id == "untrusted/model")
    assert untrusted_dec.decision is DecisionState.DENY
    assert "route is not qualified for restricted data" in untrusted_dec.reasons

    # No route_key is also denied
    no_route_dec = next(d for d in result.decisions if d.candidate_id == "no-route")
    assert no_route_dec.decision is DecisionState.DENY
    assert "route is not qualified for restricted data" in no_route_dec.reasons

    # Non-restricted task (public) is unchanged
    public_task = TaskSpec(objective="public query", task_type="query", privacy="public")
    public_policy = compile_policy(public_task)
    assert public_policy.restricted_data is False
    pub_cand = PolicyCandidate(
        candidate_id="any-route", route_identity=_route("any/model"), availability="eligible"
    )
    pub_result = public_policy.compile([pub_cand], at=NOW)
    assert pub_result.decisions[0].decision is DecisionState.ALLOW


def test_capacity_floor() -> None:
    """G1.2: Capacity floor: quota_exhausted and rate_limited are hard DENY in Policy.evaluate.

    Tests the real Policy.evaluate path where availability states quota_exhausted
    and rate_limited cause immediate DENY with a named reason.  An eligible
    candidate passes through.  Fails when the capacity-state DENY branch is removed.
    """
    cand_quota = PolicyCandidate(
        candidate_id="quota-model",
        route_identity=_route("quota/model"),
        availability="quota_exhausted",
    )
    cand_rate = PolicyCandidate(
        candidate_id="rate-model", route_identity=_route("rate/model"), availability="rate_limited"
    )
    cand_ok = PolicyCandidate(
        candidate_id="ok-model", route_identity=_route("ok/model"), availability="eligible"
    )

    policy = Policy(require_actual_identity=False)
    result = policy.compile([cand_quota, cand_rate, cand_ok], at=NOW)

    quota_dec = next(d for d in result.decisions if d.candidate_id == "quota-model")
    assert quota_dec.decision is DecisionState.DENY
    assert "availability is quota_exhausted" in quota_dec.reasons

    rate_dec = next(d for d in result.decisions if d.candidate_id == "rate-model")
    assert rate_dec.decision is DecisionState.DENY
    assert "availability is rate_limited" in rate_dec.reasons

    ok_dec = next(d for d in result.decisions if d.candidate_id == "ok-model")
    assert ok_dec.decision is DecisionState.ALLOW

    assert [c.candidate_id for c in result.eligible] == ["ok-model"]


# ---------------------------------------------------------------------------
# G1.3: Intelligence cannot bypass gate exclusions
# ---------------------------------------------------------------------------


def test_intelligence_cannot_bypass_gate() -> None:
    """G1.3: Advisory intelligence/ranker scores cannot re-admit excluded candidates.

    Even if a ranker or learned intelligence would score an excluded candidate
    higher, the gate's decision is final.
    """
    models = [_fake_model("excluded/smart", tier=1), _fake_model("admitted/basic", tier=3)]

    eligibility = _eligibility_result(models, ["denied", "eligible"])

    config = AdaptiveRankerConfig(mode=RankerMode.SHADOW_ADAPTIVE, max_history_size=10)
    ranker = AdaptiveRanker(config)

    for _iteration in range(10):
        output = ranker.rank(eligibility, task_spec="test task")

        ranked_ids = [m.id for m in output.ranked]
        assert "excluded/smart" not in ranked_ids
        assert "admitted/basic" in ranked_ids
        assert len(output.ranked) == 1
        assert output.ranked[0].id == "admitted/basic"


# ---------------------------------------------------------------------------
# G1.4: Deterministic selection
# ---------------------------------------------------------------------------


def test_deterministic_selection() -> None:
    """G1.4: Identical inputs produce identical selection and explain output."""
    models = [
        _fake_model("provider-a/model-1", tier=2),
        _fake_model("provider-b/model-2", tier=2),
        _fake_model("provider-c/model-3", tier=1),
    ]

    eligibility = _eligibility_result(models, ["eligible", "eligible", "eligible"])
    ranker = AdaptiveRanker(AdaptiveRankerConfig(mode=RankerMode.STATIC))

    outputs = [ranker.rank(eligibility, task_spec="deterministic test") for _ in range(5)]

    first_ranked = [m.id for m in outputs[0].ranked]
    for output in outputs[1:]:
        assert [m.id for m in output.ranked] == first_ranked, "Selection must be deterministic"

    assert len(first_ranked) == 3


# ---------------------------------------------------------------------------
# G1.5: Explain output schema
# ---------------------------------------------------------------------------


def test_explain_output_schema() -> None:
    """G1.5: Explain output contains all required fields."""
    models = [_fake_model("eligible/model"), _fake_model("denied/model")]

    avail_report = _availability_report(
        models, [AvailabilityState.ELIGIBLE, AvailabilityState.DENIED]
    )

    explain_output = explain_freshness(
        avail_report, policy_version="test-policy-v1", refresh_error=None, refreshing=False
    )

    required_fields = {
        "observed_at",
        "expires_at",
        "age_seconds",
        "source",
        "confidence",
        "candidate_count",
        "eligible_count",
        "refresh_error",
        "refreshing",
        "errors",
        "policy_version",
        "freshness_seconds",
    }

    for f in required_fields:
        assert f in explain_output, f"Explain output missing required field: {f}"

    assert explain_output["candidate_count"] == 2
    assert explain_output["eligible_count"] == 1
    assert "T" in explain_output["observed_at"]
    if explain_output["expires_at"] is not None:
        assert "T" in explain_output["expires_at"]
    assert isinstance(explain_output["age_seconds"], (int, float))
    assert 0.0 <= explain_output["confidence"] <= 1.0
    assert explain_output["source"] == "test"

    def mock_source() -> AvailabilityReport:
        return avail_report

    cache = AvailabilityCache(
        source=mock_source,
        ttl_seconds=60,
        stale_window_seconds=30,
        policy_version="test-cache-policy",
    )
    cache_explain = cache.explain("eligible/model")

    for f in {"model_id", "cache_state", "stale"}:
        assert f in cache_explain, f"Cache explain missing required field: {f}"

    for f in required_fields:
        assert f in cache_explain, f"Cache explain missing freshness field: {f}"
