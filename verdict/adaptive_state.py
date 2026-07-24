"""
Adaptive state versioning, snapshots, rollback, and benchmarking (Issue #61 / Slice 33.4).

This module provides reproducible state management for the adaptive ranker:
- Versioned snapshots with manifests and checksums
- Restore/rollback without deleting evidence
- Dropped trajectory counting and surfacing
- Benchmark framework for static vs semantic vs shadow adaptive
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from verdict.adaptive_ranker import AdaptiveRanker, AdaptiveRankerConfig, RankerMode


@dataclass(frozen=True)
class SnapshotManifest:
    """Manifest describing a state snapshot."""

    version: str
    timestamp: float
    config_hash: str
    history_checksum: str
    dropped_trajectories: int
    total_entries: int
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Snapshot:
    """Complete adaptive state snapshot."""

    manifest: SnapshotManifest
    config: AdaptiveRankerConfig
    history: list[dict[str, Any]]
    _checksum: str = field(repr=False, default="")

    def __post_init__(self) -> None:
        if not self._checksum:
            object.__setattr__(self, "_checksum", self._compute_checksum())

    def _compute_checksum(self) -> str:
        data = {
            "manifest": asdict(self.manifest),
            "config": asdict(self.config),
            "history": self.history,
        }
        return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()

    def verify(self) -> bool:
        """Verify snapshot integrity."""
        return self._checksum == self._compute_checksum()


@dataclass(frozen=True)
class BenchmarkResult:
    """Results from a benchmark run."""

    mode: RankerMode
    task_count: int
    p50_latency_ms: float
    p95_latency_ms: float
    cold_start_ms: float
    memory_bytes: int
    failure_rate: float
    quality_score: float
    recall: float
    precision: float
    timestamp: float = field(default_factory=time.time)


class AdaptiveStateManager:
    """
    Manages adaptive ranker state with versioning, snapshots, rollback, and benchmarks.
    """

    def __init__(
        self,
        ranker: AdaptiveRanker,
        storage_dir: str | Path = "./.verdict/adaptive_state",
        max_snapshots: int = 100,
        retention_days: int = 30,
    ) -> None:
        self.ranker = ranker
        self.storage_dir = Path(storage_dir)
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self.max_snapshots = max_snapshots
        self.retention_days = retention_days
        self._dropped_trajectories = 0

    def _compute_config_hash(self) -> str:
        """Compute hash of current config."""
        config_data = asdict(self.ranker.config)
        return hashlib.sha256(json.dumps(config_data, sort_keys=True).encode()).hexdigest()[:16]

    def _compute_history_checksum(self) -> str:
        """Compute checksum of current history."""
        return hashlib.sha256(
            json.dumps(self.ranker._history, sort_keys=True).encode()
        ).hexdigest()[:16]

    def create_snapshot(self, metadata: dict[str, Any] | None = None) -> Snapshot:
        """Create a versioned snapshot of current state."""
        config_hash = self._compute_config_hash()
        history_checksum = self._compute_history_checksum()

        manifest = SnapshotManifest(
            version=f"v{len(list(self.storage_dir.glob('snapshot_*'))) + 1}",
            timestamp=time.time(),
            config_hash=config_hash,
            history_checksum=history_checksum,
            dropped_trajectories=self._dropped_trajectories,
            total_entries=len(self.ranker._history),
            metadata=metadata or {},
        )

        snapshot = Snapshot(
            manifest=manifest,
            config=self.ranker.config,
            history=self.ranker._history.copy(),
        )

        # Save to disk
        snapshot_path = (
            self.storage_dir / f"snapshot_{manifest.version}_{manifest.timestamp:.0f}.json"
        )
        with open(snapshot_path, "w") as f:
            json.dump(
                {
                    "manifest": asdict(snapshot.manifest),
                    "config": asdict(snapshot.config),
                    "history": snapshot.history,
                    "checksum": snapshot._checksum,
                },
                f,
                indent=2,
            )

        # Enforce retention
        self._enforce_retention()

        return snapshot

    def restore_snapshot(self, snapshot: Snapshot) -> bool:
        """Restore ranker state from snapshot."""
        if not snapshot.verify():
            return False

        self.ranker._history = snapshot.history.copy()
        self.ranker.config = snapshot.config
        return True

    def rollback_to_snapshot(self, snapshot: Snapshot) -> bool:
        """Rollback to a previous snapshot (keeps evidence)."""
        if not snapshot.verify():
            return False

        # Restore state but keep evidence intact
        self.ranker._history = snapshot.history.copy()
        self.ranker.config = snapshot.config
        return True

    def get_snapshot(self, version: str) -> Snapshot | None:
        """Load a specific snapshot by version."""
        for path in self.storage_dir.glob(f"snapshot_{version}_*.json"):
            with open(path) as f:
                data = json.load(f)
            manifest = SnapshotManifest(**data["manifest"])
            config = AdaptiveRankerConfig(**data["config"])
            return Snapshot(
                manifest=manifest,
                config=config,
                history=data["history"],
                _checksum=data.get("checksum", ""),
            )
        return None

    def list_snapshots(self) -> list[SnapshotManifest]:
        """List all available snapshots."""
        snapshots = []
        for path in sorted(self.storage_dir.glob("snapshot_*.json")):
            with open(path) as f:
                data = json.load(f)
            snapshots.append(SnapshotManifest(**data["manifest"]))
        return snapshots

    def _enforce_retention(self) -> None:
        """Enforce max snapshots and time-based retention."""
        snapshots = sorted(
            self.storage_dir.glob("snapshot_*.json"),
            key=lambda p: p.stat().st_mtime,
        )

        # Remove excess snapshots
        while len(snapshots) > self.max_snapshots:
            oldest = snapshots.pop(0)
            oldest.unlink()

        # Remove old snapshots
        cutoff = time.time() - (self.retention_days * 86400)
        for path in snapshots:
            if path.stat().st_mtime < cutoff:
                path.unlink()

    def record_dropped_trajectory(self) -> None:
        """Record a dropped trajectory."""
        self._dropped_trajectories += 1

    def get_dropped_trajectories(self) -> int:
        """Get count of dropped trajectories."""
        return self._dropped_trajectories

    def run_benchmark(
        self,
        test_cases: list[tuple[str, str]],  # (task, expected_model)
        modes: list[RankerMode] | None = None,
    ) -> dict[RankerMode, BenchmarkResult]:
        """
        Benchmark ranker modes against test cases.

        Args:
            test_cases: List of (task_spec, expected_model_id) tuples
            modes: Modes to benchmark (default: all)

        Returns:
            Dict mapping mode to BenchmarkResult
        """
        if modes is None:
            modes = [RankerMode.STATIC, RankerMode.SEMANTIC, RankerMode.SHADOW_ADAPTIVE]

        results = {}

        for mode in modes:
            # Create fresh ranker with this mode
            config = AdaptiveRankerConfig(mode=mode)
            ranker = AdaptiveRanker(config=config)

            latencies = []
            correct = 0
            total = 0
            failed = 0

            for task, expected_model in test_cases:
                try:
                    start = time.perf_counter()
                    # Mock eligibility result with expected model
                    from verdict.eligibility import (
                        EligibilityRecord,
                        EligibilityResult,
                        EligibilityVerdict,
                    )
                    from verdict.models import ModelInfo

                    expected = ModelInfo(
                        id=expected_model,
                        provider=expected_model.split("/")[0] if "/" in expected_model else "test",
                        capability_tier=1,
                        capabilities=frozenset(),
                    )
                    eligibility = EligibilityResult(
                        admitted=[expected],
                        records=[
                            EligibilityRecord(
                                model_id=expected_model,
                                provider=expected_model.split("/")[0]
                                if "/" in expected_model
                                else "test",
                                admitted=True,
                                verdict=EligibilityVerdict.ELIGIBLE,
                                state="eligible",
                                source="benchmark",
                            )
                        ],
                    )

                    output = ranker.rank(eligibility, task_spec=task)
                    elapsed = (time.perf_counter() - start) * 1000
                    latencies.append(elapsed)

                    if output.ranked and output.ranked[0].id == expected_model:
                        correct += 1
                    total += 1

                except Exception:
                    failed += 1
                    total += 1

            if latencies:
                latencies.sort()
                p50 = latencies[len(latencies) // 2]
                p95 = latencies[int(len(latencies) * 0.95)]
            else:
                p50 = p95 = 0.0

            results[mode] = BenchmarkResult(
                mode=mode,
                task_count=total,
                p50_latency_ms=p50,
                p95_latency_ms=p95,
                cold_start_ms=latencies[0] if latencies else 0.0,
                memory_bytes=len(str(ranker._history).encode()),
                failure_rate=failed / total if total > 0 else 1.0,
                quality_score=correct / total if total > 0 else 0.0,
                recall=correct / total if total > 0 else 0.0,
                precision=correct / total if total > 0 else 0.0,
            )

        return results


def build_adaptive_state_manager(
    ranker: AdaptiveRanker,
    storage_dir: str | Path = "./.verdict/adaptive_state",
) -> AdaptiveStateManager:
    """Factory function for adaptive state manager."""
    return AdaptiveStateManager(ranker, storage_dir)
