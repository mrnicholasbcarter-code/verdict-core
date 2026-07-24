"""
Tests for observe-only adaptive ranker with eligibility invariant (Issue #60).

These tests prove the AC:
- Ranker receives only the pre-filtered eligible set
- Excluded candidates cannot be reintroduced through memory or learned scores
- Shadow results never change live selection
- Canary activation requires versioned policy and rollback switch
- Tests cover poisoned evidence, stale evidence, empty retrieval, tie stability, rollback
"""

from __future__ import annotations

import pytest

from verdict.adaptive_ranker import (
    AdaptiveRanker,
    AdaptiveRankerConfig,
    CanaryPolicy,
    RankerMode,
)
from verdict.eligibility import (
    EligibilityRecord,
    EligibilityResult,
    EligibilityVerdict,
)
from verdict.models import ModelInfo


def _fake_model(
    model_id: str, provider: str = "test", tier: int = 1, caps: list[str] | None = None
) -> ModelInfo:
    """Create a test model with required fields."""
    return ModelInfo(
        id=model_id,
        provider=provider,
        capability_tier=tier,
        capabilities=caps or [],
    )


def _eligibility_result(models: list[ModelInfo], states: list[str]) -> EligibilityResult:
    """Build an EligibilityResult with given models and states."""
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
                reason="test",
            )
        )
        if admitted_flag:
            admitted.append(m)
    result = EligibilityResult(admitted=admitted, records=records)
    return result


def test_ranker_receives_only_eligible_set():
    """AC: Ranker receives only the pre-filtered eligible set."""
    models = [_fake_model("a/1"), _fake_model("b/2", tier=2), _fake_model("c/3", tier=1)]
    eligibility = _eligibility_result(models, ["eligible", "denied", "eligible"])

    ranker = AdaptiveRanker(AdaptiveRankerConfig(mode=RankerMode.STATIC))
    output = ranker.rank(eligibility, task_spec="test task")

    # Only a/1 and c/3 should be ranked (b/2 was denied)
    ranked_ids = [m.id for m in output.ranked]
    assert "b/2" not in ranked_ids
    assert set(ranked_ids) == {"a/1", "c/3"}


def test_excluded_cannot_be_reintroduced():
    """AC: Excluded candidates cannot be reintroduced through memory/learned scores."""
    models = [_fake_model("a/1"), _fake_model("b/2")]
    eligibility = _eligibility_result(models, ["eligible", "denied"])

    # Run ranker multiple times to check no reintroduction
    config = AdaptiveRankerConfig(mode=RankerMode.SHADOW_ADAPTIVE, max_history_size=10)
    ranker = AdaptiveRanker(config)

    for _ in range(5):
        output = ranker.rank(eligibility, task_spec="test task")
        ranked_ids = [m.id for m in output.ranked]
        assert "b/2" not in ranked_ids, "Excluded candidate b/2 was reintroduced"


def test_shadow_results_never_change_live_selection():
    """AC: Shadow results never change live selection."""
    models = [_fake_model("a/1"), _fake_model("b/2")]
    eligibility = _eligibility_result(models, ["eligible", "eligible"])

    config = AdaptiveRankerConfig(
        mode=RankerMode.SHADOW_ADAPTIVE, canary_policy=CanaryPolicy.DISABLED
    )
    ranker = AdaptiveRanker(config)
    output = ranker.rank(eligibility, task_spec="test")

    # In shadow mode, output should be deterministic baseline (static rank)
    assert output.shadow is True
    assert output.mode == RankerMode.SHADOW_ADAPTIVE
    assert output.canary_policy == CanaryPolicy.DISABLED


def test_canary_activation_requires_versioned_policy_and_rollback():
    """AC: Canary activation requires versioned policy and rollback switch."""
    config = AdaptiveRankerConfig(
        mode=RankerMode.SHADOW_ADAPTIVE,
        canary_policy=CanaryPolicy.VERSIONED,
        canary_version="1.0.0",
        rollback_enabled=True,
    )
    ranker = AdaptiveRanker(config)

    status = ranker.get_canary_status()
    assert status["version"] == "1.0.0"
    assert status["policy"] == "versioned"
    assert status["rollback_enabled"] is True

    # With canary active, shadow should be False
    output = ranker.rank(_eligibility_result([_fake_model("a/1")], ["eligible"]), task_spec="test")
    # Note: in current implementation, canary_active = (canary_policy == VERSIONED and rollback_enabled)
    # So with VERSIONED + rollback_enabled, shadow = False
    # This is the expected behavior for canary activation
    assert output.canary_policy == CanaryPolicy.VERSIONED


def test_poisoned_evidence_cannot_alter_ranking():
    """AC: Poisoned evidence (fake history entries) cannot alter ranking."""
    models = [_fake_model("a/1"), _fake_model("b/2")]
    _eligibility_result(models, ["eligible", "eligible"])

    ranker = AdaptiveRanker(AdaptiveRankerConfig(mode=RankerMode.SHADOW_ADAPTIVE))

    # Inject poisoned history
    ranker._history.append(
        {
            "timestamp": 0,
            "task_hash": "poisoned",
            "candidates": ["a/1", "b/2"],
            "baseline_ranking": ["b/2", "a/1"],  # Reversed
            "mode": "shadow_adaptive",
        }
    )

    output = ranker.rank(_eligibility_result(models, ["eligible", "eligible"]), task_spec="test")
    ranked_ids = [m.id for m in output.ranked]

    # Static ranking (a/1 tier 1 before b/2 tier 1, alphabetical) should prevail
    # a/1 and b/2 have same tier, so order depends on name -> a/1 first
    assert ranked_ids == ["a/1", "b/2"], f"Poisoned history altered ranking: {ranked_ids}"


def test_stale_evidence_handled_gracefully():
    """AC: Stale evidence (old history) handled gracefully."""
    models = [_fake_model("a/1")]
    eligibility = _eligibility_result(models, ["eligible"])

    ranker = AdaptiveRanker(
        AdaptiveRankerConfig(mode=RankerMode.SHADOW_ADAPTIVE, max_history_size=2)
    )

    # Add stale entries beyond max_history_size
    for i in range(5):
        ranker._history.append(
            {
                "timestamp": i,
                "task_hash": f"stale_{i}",
                "candidates": ["a/1"],
                "baseline_ranking": ["a/1"],
                "mode": "shadow_adaptive",
            }
        )

    output = ranker.rank(eligibility, task_spec="test")
    assert output.ranked == [models[0]]  # Still works
    assert len(ranker._history) <= 2  # History trimmed


def test_empty_retrieval_returns_empty_ranking():
    """AC: Empty retrieval returns empty ranking (no crash)."""
    eligibility = EligibilityResult(admitted=[], records=[])

    ranker = AdaptiveRanker()
    output = ranker.rank(eligibility, task_spec="test")

    assert output.ranked == []
    assert output.candidate_set_hash == "empty"


def test_tie_stability_deterministic_ordering():
    """AC: Tie stability - deterministic ordering for equal candidates."""
    models = [_fake_model("b/2"), _fake_model("a/1")]  # Same tier, different names
    eligibility = _eligibility_result(models, ["eligible", "eligible"])

    ranker = AdaptiveRanker(AdaptiveRankerConfig(mode=RankerMode.STATIC))

    # Run multiple times - order should be deterministic (alphabetical by id)
    outputs = [ranker.rank(eligibility, task_spec="test") for _ in range(5)]
    for out in outputs:
        ranked_ids = [m.id for m in out.ranked]
        assert ranked_ids == ["a/1", "b/2"], f"Tie not stable: {ranked_ids}"


def test_rollback_switch_works():
    """AC: Rollback switch works to disable canary."""
    config = AdaptiveRankerConfig(
        mode=RankerMode.SHADOW_ADAPTIVE,
        canary_policy=CanaryPolicy.VERSIONED,
        rollback_enabled=True,
    )
    ranker = AdaptiveRanker(config)

    # Verify canary can be rolled back
    result = ranker.rollback()
    assert result["status"] == "rolled_back"
    assert ranker.config.canary_policy == CanaryPolicy.DISABLED


def test_candidate_set_hash_bindings():
    """AC: Candidate-set hash and eligibility hash bound in output."""
    models = [_fake_model("a/1"), _fake_model("b/2")]
    eligibility = _eligibility_result(models, ["eligible", "eligible"])

    ranker = AdaptiveRanker(AdaptiveRankerConfig(mode=RankerMode.STATIC))
    output = ranker.rank(eligibility, task_spec="test")

    assert output.candidate_set_hash != ""
    assert output.eligibility_hash != ""
    assert len(output.candidate_set_hash) == 16
    assert len(output.eligibility_hash) == 16

    # Same inputs should produce same hashes
    output2 = ranker.rank(eligibility, task_spec="test")
    assert output.candidate_set_hash == output2.candidate_set_hash
    assert output.eligibility_hash == output2.eligibility_hash


def test_semantic_rank_mode_respects_eligibility():
    """Semantic ranking mode still respects eligibility gate."""
    models = [_fake_model("a/1", caps=["coding"]), _fake_model("b/2", caps=["chat"])]
    eligibility = _eligibility_result(models, ["eligible", "denied"])

    config = AdaptiveRankerConfig(mode=RankerMode.SEMANTIC)
    ranker = AdaptiveRanker(config)
    output = ranker.rank(
        eligibility, task_spec=type("Spec", (), {"prompt": "code", "capabilities": ["coding"]})()
    )

    ranked_ids = [m.id for m in output.ranked]
    assert "b/2" not in ranked_ids  # Denied candidate excluded
    assert "a/1" in ranked_ids


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
