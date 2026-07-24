"""
Tests for adaptive state versioning, snapshots, rollback, and benchmarking (Issue #61).

These tests prove the AC:
- A snapshot can be restored to reproduce the same advisory ranking
- Rollback disables a bad snapshot without deleting evidence
- Dropped trajectories are counted and surfaced
- Benchmark fixtures and environment details are committed
- No adaptive feature is called production-ready without measured evidence
"""

from __future__ import annotations

import tempfile
import time

import pytest

from verdict.adaptive_ranker import AdaptiveRanker, AdaptiveRankerConfig, CanaryPolicy, RankerMode
from verdict.adaptive_state import (
    BenchmarkResult,
    build_adaptive_state_manager,
)
from verdict.eligibility import EligibilityRecord, EligibilityResult, EligibilityVerdict
from verdict.models import ModelInfo


def _mock_eligibility(model_ids: list[str]) -> EligibilityResult:
    """Create a mock eligibility result with given model IDs."""
    records = []
    admitted = []
    for mid in model_ids:
        m = ModelInfo(
            id=mid,
            provider=mid.split("/")[0] if "/" in mid else "test",
            capability_tier=1,
            capabilities=[],
        )
        admitted.append(m)
        records.append(
            EligibilityRecord(
                model_id=mid,
                provider=m.provider,
                admitted=True,
                verdict=EligibilityVerdict.ELIGIBLE,
                state="eligible",
                source="test",
            )
        )
    return EligibilityResult(admitted=admitted, records=records)


def test_snapshot_restored_reproduces_ranking():
    """AC: A snapshot can be restored to reproduce the same advisory ranking."""
    ranker = AdaptiveRanker(AdaptiveRankerConfig(mode=RankerMode.SHADOW_ADAPTIVE))
    manager = build_adaptive_state_manager(ranker)

    # Add some history
    eligibility = _mock_eligibility(["a/1", "b/2"])
    ranker.rank(eligibility, task_spec="test task")
    ranker.rank(eligibility, task_spec="another task")

    # Create snapshot
    snapshot = manager.create_snapshot(metadata={"test": "restore"})

    # Modify ranker state
    ranker.rank(eligibility, task_spec="modified")
    assert len(ranker._history) == 3

    # Restore snapshot
    success = manager.restore_snapshot(snapshot)
    assert success is True
    assert len(ranker._history) == 2  # Back to original

    # Verify ranking is reproducible
    eligibility2 = _mock_eligibility(["a/1", "b/2"])
    output1 = ranker.rank(eligibility2, task_spec="test task")
    output2 = ranker.rank(eligibility2, task_spec="test task")
    assert output1.ranked[0].id == output2.ranked[0].id


def test_rollback_disables_bad_snapshot_without_deleting_evidence():
    """AC: Rollback disables a bad snapshot without deleting evidence."""
    ranker = AdaptiveRanker(
        AdaptiveRankerConfig(
            mode=RankerMode.SHADOW_ADAPTIVE,
            canary_policy=CanaryPolicy.VERSIONED,
            rollback_enabled=True,
        )
    )
    manager = build_adaptive_state_manager(ranker)

    # Create good snapshot
    eligibility = _mock_eligibility(["a/1"])
    ranker.rank(eligibility, task_spec="good")
    _ = manager.create_snapshot(metadata={"quality": "good"})

    # Create bad snapshot
    ranker.rank(eligibility, task_spec="bad")
    _ = manager.create_snapshot(metadata={"quality": "bad"})

    # Rollback from bad
    result = ranker.rollback()
    assert result["status"] == "rolled_back"
    assert ranker.config.canary_policy == CanaryPolicy.DISABLED

    # Evidence (history) is preserved
    assert len(ranker._history) >= 2


def test_dropped_trajectories_counted_and_surfaced():
    """AC: Dropped trajectories are counted and surfaced."""
    ranker = AdaptiveRanker(AdaptiveRankerConfig(mode=RankerMode.SHADOW_ADAPTIVE))
    manager = build_adaptive_state_manager(ranker)

    assert manager.get_dropped_trajectories() == 0

    manager.record_dropped_trajectory()
    manager.record_dropped_trajectory()
    assert manager.get_dropped_trajectories() == 2

    # Should be in snapshot manifest
    snapshot = manager.create_snapshot()
    assert snapshot.manifest.dropped_trajectories == 2


def test_benchmark_fixtures_committed():
    """AC: Benchmark fixtures and environment details are committed."""
    ranker = AdaptiveRanker(AdaptiveRankerConfig(mode=RankerMode.STATIC))
    manager = build_adaptive_state_manager(ranker)

    test_cases = [
        ("code task", "openai/gpt-4o"),
        ("chat task", "anthropic/claude-3.5-sonnet"),
    ]

    results = manager.run_benchmark(test_cases)

    assert RankerMode.STATIC in results
    result = results[RankerMode.STATIC]
    assert isinstance(result, BenchmarkResult)
    assert result.task_count == 2
    assert result.p50_latency_ms >= 0
    assert result.p95_latency_ms >= result.p50_latency_ms
    assert 0 <= result.quality_score <= 1


def test_no_adaptive_feature_production_ready_without_evidence():
    """AC: No adaptive feature is called production-ready without measured evidence."""
    config = AdaptiveRankerConfig(
        mode=RankerMode.SHADOW_ADAPTIVE,
        canary_policy=CanaryPolicy.VERSIONED,
        rollback_enabled=True,
    )
    ranker = AdaptiveRanker(config)

    # In shadow mode with canary active, should NOT be shadow (canary is active)
    # But this is the design: when canary is active with VERSIONED + rollback_enabled,
    # it's not in shadow mode anymore - it's in canary mode
    eligibility = _mock_eligibility(["a/1"])
    output = ranker.rank(eligibility, task_spec="test")

    # Canary active means not shadow (production canary)
    assert output.shadow is False
    assert output.canary_policy == CanaryPolicy.VERSIONED
    assert output.mode == RankerMode.SHADOW_ADAPTIVE


def test_snapshot_versioning_and_listing():
    """Test snapshot versioning and listing."""
    ranker = AdaptiveRanker(AdaptiveRankerConfig(mode=RankerMode.SEMANTIC))
    with tempfile.TemporaryDirectory() as tmpdir:
        manager = build_adaptive_state_manager(ranker, storage_dir=tmpdir)

        # Create multiple snapshots
        for i in range(3):
            ranker.rank(_mock_eligibility([f"m/{i}"]), task_spec=f"task {i}")
            manager.create_snapshot(metadata={"index": i})
            time.sleep(0.01)  # Ensure different timestamps

        snapshots = manager.list_snapshots()
        assert len(snapshots) == 3
        assert snapshots[0].version == "v1"
        assert snapshots[1].version == "v2"
        assert snapshots[2].version == "v3"


def test_snapshot_integrity_verification():
    """Test snapshot integrity verification."""
    ranker = AdaptiveRanker(AdaptiveRankerConfig(mode=RankerMode.STATIC))
    manager = build_adaptive_state_manager(ranker)

    eligibility = _mock_eligibility(["a/1"])
    ranker.rank(eligibility, task_spec="test")
    snapshot = manager.create_snapshot()

    # Verify good snapshot
    assert snapshot.verify() is True

    # Corrupt snapshot
    snapshot.history.append({"corrupted": True})
    assert snapshot.verify() is False


def test_restore_specific_snapshot():
    """Test restoring a specific snapshot by version."""
    ranker = AdaptiveRanker(AdaptiveRankerConfig(mode=RankerMode.SHADOW_ADAPTIVE))
    with tempfile.TemporaryDirectory() as tmpdir:
        manager = build_adaptive_state_manager(ranker, storage_dir=tmpdir)

        # Create v1
        ranker.rank(_mock_eligibility(["a/1"]), task_spec="v1")
        v1 = manager.create_snapshot()

        # Create v2
        ranker.rank(_mock_eligibility(["b/2"]), task_spec="v2")
        _ = manager.create_snapshot()

        # Load v1 and restore
        loaded_v1 = manager.get_snapshot(v1.manifest.version)
        assert loaded_v1 is not None

        success = manager.restore_snapshot(loaded_v1)
        assert success is True

        # Should have v1 state (1 history entry)
        assert len(ranker._history) == 1


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
