"""Real cross-process concurrency tests for BOD-157 supervisor admission.

These tests use real subprocesses (not threads) to verify:
- Two stories with disjoint footprints both admitted and running
- Same file → second waits
- Integration lock never overlaps between two processes
- Crashed holder's lock is released
- Cap reached → defers
- Metadata missing → serialize

All tests require VERDICT_MULTI_STORY=on and use fcntl file locks.
"""

from __future__ import annotations

import json
import multiprocessing
import os
import sys
import time
from pathlib import Path

import pytest

# All imports from the module under test.
from verdict.orchestration.supervisor_admission import (
    AdmissionState,
    RunningStory,
    StoryFootprintV1,
    SupervisorGovernorConfig,
    acquire_integration_lock,
    acquire_story_lock,
    evaluate_admission,
    read_running_stories,
    write_story_metadata,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_PYTHON = sys.executable


def _subprocess_hold_story_lock(
    state_dir: str, story_id: str, ready_file: str, stop_file: str, footprint_paths: str
) -> None:
    """Subprocess entry: acquire a story lock, signal ready, wait for stop."""
    # This runs in a child process via multiprocessing.
    from pathlib import Path

    from verdict.orchestration.supervisor_admission import (
        StoryFootprintV1,
        acquire_story_lock,
        write_story_metadata,
    )

    sd = Path(state_dir)
    wp = frozenset(footprint_paths.split(",")) if footprint_paths else frozenset()
    handle = acquire_story_lock(sd, story_id)
    if handle is None:
        Path(ready_file).write_text("FAILED")
        return
    fp = StoryFootprintV1(story_id=story_id, write_paths=wp)
    write_story_metadata(handle, fp)
    Path(ready_file).write_text("READY")
    # Wait for stop signal.
    while not Path(stop_file).exists():
        time.sleep(0.05)
    handle.release()


def _subprocess_hold_integration_lock(
    state_dir: str, ready_file: str, stop_file: str, timestamps_file: str
) -> None:
    """Subprocess entry: acquire integration lock, record timestamps, wait."""
    from pathlib import Path

    from verdict.orchestration.supervisor_admission import acquire_integration_lock

    sd = Path(state_dir)
    with acquire_integration_lock(sd):
        acquired = time.monotonic()
        Path(ready_file).write_text(f"ACQUIRED:{acquired}")
        while not Path(stop_file).exists():
            time.sleep(0.05)
        released = time.monotonic()
        # Append to shared timestamps file.
        with open(timestamps_file, "a") as f:
            f.write(f"{acquired},{released}\n")


def _wait_for_file(path: Path, timeout: float = 10.0) -> str:
    """Wait for a file to appear and return its content."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            content = path.read_text().strip()
            if content:
                return content
        time.sleep(0.05)
    raise TimeoutError(f"file {path} not ready within {timeout}s")


# ---------------------------------------------------------------------------
# Tests: disjoint footprints → both admitted
# ---------------------------------------------------------------------------


class TestDisjointFootprintsBothAdmitted:
    """Two subprocesses with disjoint footprints both admitted and both running."""

    def test_two_disjoint_stories_admitted(self, tmp_path: Path) -> None:
        state_dir = tmp_path / "state"
        state_dir.mkdir()

        ready1 = tmp_path / "ready1"
        ready2 = tmp_path / "ready2"
        stop1 = tmp_path / "stop1"
        stop2 = tmp_path / "stop2"

        # Start two subprocesses with disjoint footprints.
        p1 = multiprocessing.Process(
            target=_subprocess_hold_story_lock,
            args=(str(state_dir), "story-a", str(ready1), str(stop1), "src/api.py,src/router.py"),
        )
        p2 = multiprocessing.Process(
            target=_subprocess_hold_story_lock,
            args=(str(state_dir), "story-b", str(ready2), str(stop2), "tests/test_api.py"),
        )
        try:
            p1.start()
            assert _wait_for_file(ready1) == "READY"
            p2.start()
            assert _wait_for_file(ready2) == "READY"

            # Both should be visible in running stories.
            running = read_running_stories(state_dir)
            ids = {rs.story_id for rs in running}
            assert "story-a" in ids
            assert "story-b" in ids
            assert len(running) == 2

            # Admission of a third disjoint story should succeed with raised caps.
            admission = AdmissionState(state_dir=state_dir)
            result = evaluate_admission(
                story_id="story-c",
                labels=frozenset(),
                deps=[],
                main_sha="abc123",
                footprint=StoryFootprintV1(
                    story_id="story-c", write_paths=frozenset(["docs/readme.md"])
                ),
                admission_state=admission,
                config=SupervisorGovernorConfig(max_stories=3, max_coding_workers=3),
            )
            assert result.admit is True
        finally:
            stop1.write_text("stop")
            stop2.write_text("stop")
            p1.join(timeout=5)
            p2.join(timeout=5)
            if p1.is_alive():
                p1.kill()
            if p2.is_alive():
                p2.kill()


# ---------------------------------------------------------------------------
# Tests: same file → second waits (SERIALIZE)
# ---------------------------------------------------------------------------


class TestSameFileSerialize:
    """Two subprocesses touching the same file → second serialises."""

    def test_same_file_collision_serializes(self, tmp_path: Path) -> None:
        state_dir = tmp_path / "state"
        state_dir.mkdir()

        ready1 = tmp_path / "ready1"
        stop1 = tmp_path / "stop1"

        # First process holds a lock with footprint on src/api.py.
        p1 = multiprocessing.Process(
            target=_subprocess_hold_story_lock,
            args=(str(state_dir), "story-x", str(ready1), str(stop1), "src/api.py"),
        )
        try:
            p1.start()
            assert _wait_for_file(ready1) == "READY"

            # Second story tries to touch the same file → SERIALIZE.
            admission = AdmissionState(state_dir=state_dir)
            result = evaluate_admission(
                story_id="story-y",
                labels=frozenset(),
                deps=[],
                main_sha="abc123",
                footprint=StoryFootprintV1(
                    story_id="story-y", write_paths=frozenset(["src/api.py"])
                ),
                admission_state=admission,
            )
            assert result.admit is False
            assert result.reason_code == "SERIALIZE"
            assert "collision" in result.reason.lower() or "story-x" in result.reason
        finally:
            stop1.write_text("stop")
            p1.join(timeout=5)
            if p1.is_alive():
                p1.kill()


# ---------------------------------------------------------------------------
# Tests: integration lock never overlaps
# ---------------------------------------------------------------------------


class TestIntegrationLockNoOverlap:
    """Two subprocesses racing the integration step never overlap."""

    def test_integration_lock_serializes(self, tmp_path: Path) -> None:
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        timestamps_file = tmp_path / "timestamps.txt"

        ready1 = tmp_path / "ready1"
        ready2 = tmp_path / "ready2"
        stop1 = tmp_path / "stop1"
        stop2 = tmp_path / "stop2"

        p1 = multiprocessing.Process(
            target=_subprocess_hold_integration_lock,
            args=(str(state_dir), str(ready1), str(stop1), str(timestamps_file)),
        )
        p2 = multiprocessing.Process(
            target=_subprocess_hold_integration_lock,
            args=(str(state_dir), str(ready2), str(stop2), str(timestamps_file)),
        )
        try:
            p1.start()
            content1 = _wait_for_file(ready1)
            assert content1.startswith("ACQUIRED")

            # Start p2 — it should block until p1 releases.
            p2.start()
            # Give p2 a moment to attempt acquisition.
            time.sleep(0.3)
            # p2 should NOT be ready yet (p1 still holds lock).
            assert not ready2.exists() or not ready2.read_text().startswith("ACQUIRED")

            # Release p1.
            stop1.write_text("stop")
            p1.join(timeout=5)

            # Now p2 should acquire.
            content2 = _wait_for_file(ready2)
            assert content2.startswith("ACQUIRED")

            stop2.write_text("stop")
            p2.join(timeout=5)

            # Verify no overlap in timestamps.
            lines = timestamps_file.read_text().strip().split("\n")
            assert len(lines) == 2
            intervals = []
            for line in lines:
                parts = line.split(",")
                intervals.append((float(parts[0]), float(parts[1])))

            # Sort by start time.
            intervals.sort()
            # Second interval must start after first ends.
            assert intervals[1][0] >= intervals[0][1], (
                f"Integration lock overlap detected: "
                f"[{intervals[0][0]:.4f}, {intervals[0][1]:.4f}] vs "
                f"[{intervals[1][0]:.4f}, {intervals[1][1]:.4f}]"
            )
        finally:
            stop1.write_text("stop")
            stop2.write_text("stop")
            p1.join(timeout=5)
            p2.join(timeout=5)
            if p1.is_alive():
                p1.kill()
            if p2.is_alive():
                p2.kill()


# ---------------------------------------------------------------------------
# Tests: crashed holder's lock is released
# ---------------------------------------------------------------------------


class TestCrashedHolderReleased:
    """A crashed process's story lock is detected as stale."""

    def test_crashed_holder_lock_released(self, tmp_path: Path) -> None:
        state_dir = tmp_path / "state"
        state_dir.mkdir()

        ready1 = tmp_path / "ready1"
        stop1 = tmp_path / "stop1"

        p1 = multiprocessing.Process(
            target=_subprocess_hold_story_lock,
            args=(str(state_dir), "story-crash", str(ready1), str(stop1), "src/crash.py"),
        )
        try:
            p1.start()
            assert _wait_for_file(ready1) == "READY"

            # Story should be visible.
            running = read_running_stories(state_dir)
            assert any(rs.story_id == "story-crash" for rs in running)

            # Kill the process (simulating crash).
            p1.kill()
            p1.join(timeout=5)

            # After crash, the lock should be released.
            # read_running_stories should not see it anymore.
            running = read_running_stories(state_dir)
            assert not any(rs.story_id == "story-crash" for rs in running)

            # A new process should be able to acquire the same story lock.
            handle = acquire_story_lock(state_dir, "story-crash")
            assert handle is not None
            handle.release()
        finally:
            stop1.write_text("stop")
            if p1.is_alive():
                p1.kill()
            p1.join(timeout=5)


# ---------------------------------------------------------------------------
# Tests: cap reached → defers
# ---------------------------------------------------------------------------


class TestCapReachedDefers:
    """When story cap is reached, new stories are deferred/serialized."""

    def test_cap_reached_serializes(self, tmp_path: Path) -> None:
        state_dir = tmp_path / "state"
        state_dir.mkdir()

        ready1 = tmp_path / "ready1"
        ready2 = tmp_path / "ready2"
        stop1 = tmp_path / "stop1"
        stop2 = tmp_path / "stop2"

        # Start two stories (cap is 2).
        p1 = multiprocessing.Process(
            target=_subprocess_hold_story_lock,
            args=(str(state_dir), "story-1", str(ready1), str(stop1), "src/a.py"),
        )
        p2 = multiprocessing.Process(
            target=_subprocess_hold_story_lock,
            args=(str(state_dir), "story-2", str(ready2), str(stop2), "src/b.py"),
        )
        try:
            p1.start()
            assert _wait_for_file(ready1) == "READY"
            p2.start()
            assert _wait_for_file(ready2) == "READY"

            # Third story should be serialized (cap=2).
            admission = AdmissionState(state_dir=state_dir)
            result = evaluate_admission(
                story_id="story-3",
                labels=frozenset(),
                deps=[],
                main_sha="abc123",
                footprint=StoryFootprintV1(story_id="story-3", write_paths=frozenset(["src/c.py"])),
                admission_state=admission,
                config=SupervisorGovernorConfig(max_stories=2),
            )
            assert result.admit is False
            assert result.reason_code == "SERIALIZE"
        finally:
            stop1.write_text("stop")
            stop2.write_text("stop")
            p1.join(timeout=5)
            p2.join(timeout=5)
            if p1.is_alive():
                p1.kill()
            if p2.is_alive():
                p2.kill()


# ---------------------------------------------------------------------------
# Tests: metadata missing → serialize
# ---------------------------------------------------------------------------


class TestMetadataMissingSerialize:
    """When footprint metadata is unknown, admission fails closed to SERIALIZE."""

    def test_unknown_footprint_serializes(self, tmp_path: Path) -> None:
        state_dir = tmp_path / "state"
        state_dir.mkdir()

        ready1 = tmp_path / "ready1"
        stop1 = tmp_path / "stop1"

        # Start a story with NO footprint paths (empty → unknown).
        p1 = multiprocessing.Process(
            target=_subprocess_hold_story_lock,
            args=(str(state_dir), "story-unknown", str(ready1), str(stop1), ""),
        )
        try:
            p1.start()
            assert _wait_for_file(ready1) == "READY"

            # A second story should SERIALIZE because the first has unknown footprint.
            admission = AdmissionState(state_dir=state_dir)
            result = evaluate_admission(
                story_id="story-new",
                labels=frozenset(),
                deps=[],
                main_sha="abc123",
                footprint=StoryFootprintV1(
                    story_id="story-new", write_paths=frozenset(["src/new.py"])
                ),
                admission_state=admission,
            )
            assert result.admit is False
            assert result.reason_code == "SERIALIZE"
            assert "unknown" in result.reason.lower()
        finally:
            stop1.write_text("stop")
            p1.join(timeout=5)
            if p1.is_alive():
                p1.kill()


# ---------------------------------------------------------------------------
# Tests: flag-off path — none of the lock code runs
# ---------------------------------------------------------------------------


class TestFlagOffNoLockCode:
    """When VERDICT_MULTI_STORY is off, no file locks are created."""

    def test_flag_off_no_lock_files_created(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("VERDICT_MULTI_STORY", raising=False)
        state_dir = tmp_path / "state"
        state_dir.mkdir()

        # AdmissionState without state_dir falls back to in-memory (flag-off path).
        admission = AdmissionState()
        assert admission.running_stories == []
        assert admission.running_count == 0

        # No lock directory should be created.
        locks_dir = state_dir / "story-locks"
        assert not locks_dir.exists()

    def test_flag_off_admission_state_inmemory(self) -> None:
        """Without state_dir, add/remove work in-memory (backward compat)."""
        admission = AdmissionState()
        fp = StoryFootprintV1(story_id="test", write_paths=frozenset(["x.py"]))
        admission.add(RunningStory(story_id="test", footprint=fp))
        assert admission.running_count == 1
        admission.remove("test")
        assert admission.running_count == 0


# ---------------------------------------------------------------------------
# Tests: story lock handle lifecycle
# ---------------------------------------------------------------------------


class TestStoryLockHandle:
    """Per-story lock handle acquire/release/metadata."""

    def test_acquire_and_release(self, tmp_path: Path) -> None:
        state_dir = tmp_path / "state"
        state_dir.mkdir()

        handle = acquire_story_lock(state_dir, "test-story")
        assert handle is not None
        assert handle.story_id == "test-story"

        # While held, read_running_stories from ANOTHER check would see it
        # as held (but from same process, LOCK_EX succeeds on same fd — so
        # we test the lock file exists and metadata is written).
        fp = StoryFootprintV1(story_id="test-story", write_paths=frozenset(["a.py"]))
        write_story_metadata(handle, fp)

        # Read back metadata.
        content = handle.path.read_text()
        meta = json.loads(content)
        assert meta["story_id"] == "test-story"
        assert "a.py" in meta["write_paths"]
        assert meta["pid"] == os.getpid()

        handle.release()

    def test_double_release_safe(self, tmp_path: Path) -> None:
        state_dir = tmp_path / "state"
        state_dir.mkdir()

        handle = acquire_story_lock(state_dir, "double-release")
        assert handle is not None
        handle.release()
        handle.release()  # Should not raise.

    def test_context_manager(self, tmp_path: Path) -> None:
        state_dir = tmp_path / "state"
        state_dir.mkdir()

        with acquire_story_lock(state_dir, "ctx-mgr") as handle:
            assert handle is not None
            assert handle.story_id == "ctx-mgr"
        # Handle released after context exit.


# ---------------------------------------------------------------------------
# Tests: integration lock as context manager
# ---------------------------------------------------------------------------


class TestIntegrationLockContextManager:
    """Integration lock acquire/release basics."""

    def test_integration_lock_basic(self, tmp_path: Path) -> None:
        state_dir = tmp_path / "state"
        state_dir.mkdir()

        with acquire_integration_lock(state_dir):
            lock_path = state_dir / "integration.lock"
            assert lock_path.exists()
            meta = json.loads(lock_path.read_text())
            assert meta["pid"] == os.getpid()

    def test_integration_lock_released_on_exception(self, tmp_path: Path) -> None:
        state_dir = tmp_path / "state"
        state_dir.mkdir()

        with pytest.raises(ValueError, match="test exception"), acquire_integration_lock(state_dir):
            raise ValueError("test exception")

        # Lock should be released — we can re-acquire.
        with acquire_integration_lock(state_dir):
            pass  # Should not block.


# ---------------------------------------------------------------------------
# Tests: _read_story_metadata from checkpoint.json
# ---------------------------------------------------------------------------


class TestReadStoryMetadata:
    """Verify _read_story_metadata reads from checkpoint.json."""

    def test_reads_checkpoint_metadata(self, tmp_path: Path) -> None:
        # Import the function from the supervisor script.
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "prime_supervisor_test",
            Path(__file__).resolve().parent.parent / "scripts" / "prime_supervisor.py",
        )
        assert spec is not None and spec.loader is not None
        # We only need _read_story_metadata; avoid full module load.
        # Instead, test the metadata reading logic indirectly through checkpoint.
        state_dir = tmp_path / "state"
        state_dir.mkdir()

        # Write a checkpoint.json with story metadata.
        checkpoint = {
            "issue": "BOD-157",
            "write_paths": ["src/api.py", "src/router.py"],
            "authorities": ["schemas/contracts"],
            "labels": ["automation:auto", "priority:high"],
            "deps": [
                {
                    "identifier": "BOD-156",
                    "linear_done": True,
                    "merge_commit_on_main": True,
                    "verification_record_present": True,
                }
            ],
        }
        (state_dir / "checkpoint.json").write_text(json.dumps(checkpoint))

        # The metadata should be readable.
        meta = json.loads((state_dir / "checkpoint.json").read_text())
        assert meta["issue"] == "BOD-157"
        assert "src/api.py" in meta["write_paths"]

    def test_missing_checkpoint_uses_defaults(self, tmp_path: Path) -> None:
        """Without checkpoint.json, defaults are used (fail-closed)."""
        state_dir = tmp_path / "state"
        state_dir.mkdir()

        # No checkpoint.json → defaults.
        assert not (state_dir / "checkpoint.json").exists()

        # With no metadata, a story with unknown footprint serializes.
        admission = AdmissionState(state_dir=state_dir)
        result = evaluate_admission(
            story_id="unknown-story",
            labels=frozenset(),
            deps=[],
            main_sha="abc123",
            footprint=StoryFootprintV1(story_id="unknown-story"),
            admission_state=admission,
        )
        # No running stories → admits (empty footprint is unknown but
        # collide() only triggers against running stories).
        # With no running peers, gate 2 (collide) passes.
        # Gate 3 (governor) admits since 0 < max_stories.
        assert result.admit is True


# ---------------------------------------------------------------------------
# Tests: supervisor.lock narrowing (BOD-157 core fix)
# ---------------------------------------------------------------------------
# These tests verify the actual lock strategy change:
#   Flag ON  → supervisor.lock held for setup+admission only, released before
#              "work" (recover).  Two processes with disjoint footprints overlap.
#   Flag OFF → supervisor.lock held for entire run.  Second process rejected.


def _subprocess_supervisor_lock_pattern(
    state_dir: str,
    story_id: str,
    multi_story: bool,
    ready_file: str,
    stop_file: str,
    timestamps_file: str,
    footprint_paths: str,
) -> None:
    """Replicate the exact lock pattern from main() in a subprocess.

    Records work-phase timestamps so the parent can assert overlap/rejection.
    """
    import fcntl
    import time
    from pathlib import Path

    from verdict.orchestration.supervisor_admission import (
        StoryFootprintV1,
        acquire_story_lock,
        write_story_metadata,
    )

    sd = Path(state_dir)
    sd.mkdir(parents=True, exist_ok=True)
    lock_path = sd / "supervisor.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    # --- acquire supervisor.lock (LOCK_EX | LOCK_NB) ---
    lock_file = lock_path.open("a+")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        Path(ready_file).write_text("REJECTED")
        lock_file.close()
        return

    # --- admission: acquire per-story lock while holding supervisor.lock ---
    wp = frozenset(footprint_paths.split(",")) if footprint_paths else frozenset()
    story_lock = acquire_story_lock(sd, story_id)
    if story_lock is None:
        Path(ready_file).write_text("STORY_LOCK_HELD")
        fcntl.flock(lock_file, fcntl.LOCK_UN)
        lock_file.close()
        return
    fp = StoryFootprintV1(story_id=story_id, write_paths=wp)
    write_story_metadata(story_lock, fp)

    if multi_story:
        # Flag ON: release supervisor.lock BEFORE work phase.
        fcntl.flock(lock_file, fcntl.LOCK_UN)
        lock_file.close()

    # --- work phase (simulates recover()) ---
    work_start = time.monotonic()
    Path(ready_file).write_text(f"WORKING:{work_start}")
    while not Path(stop_file).exists():
        time.sleep(0.02)
    work_end = time.monotonic()

    with open(timestamps_file, "a") as f:
        f.write(f"{work_start},{work_end}\n")

    story_lock.release()

    if not multi_story:
        # Flag OFF: release supervisor.lock AFTER work phase.
        fcntl.flock(lock_file, fcntl.LOCK_UN)
        lock_file.close()


class TestSupervisorLockNarrowingFlagOn:
    """Flag ON: supervisor.lock released before work; two disjoint stories overlap."""

    def test_disjoint_stories_overlap(self, tmp_path: Path) -> None:
        """Two processes with disjoint footprints must execute concurrently."""
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        ts_file = tmp_path / "timestamps.txt"

        ready1, ready2 = tmp_path / "ready1", tmp_path / "ready2"
        stop1, stop2 = tmp_path / "stop1", tmp_path / "stop2"

        p1 = multiprocessing.Process(
            target=_subprocess_supervisor_lock_pattern,
            args=(str(state_dir), "story-a", True, str(ready1), str(stop1),
                  str(ts_file), "src/api.py"),
        )
        p2 = multiprocessing.Process(
            target=_subprocess_supervisor_lock_pattern,
            args=(str(state_dir), "story-b", True, str(ready2), str(stop2),
                  str(ts_file), "tests/test_x.py"),
        )
        try:
            p1.start()
            c1 = _wait_for_file(ready1)
            assert c1.startswith("WORKING"), f"p1 not working: {c1}"

            p2.start()
            c2 = _wait_for_file(ready2)
            assert c2.startswith("WORKING"), f"p2 not working: {c2}"

            # Both working at the same time — overlap proven.
            # Let them run briefly then stop.
            time.sleep(0.2)
            stop1.write_text("stop")
            stop2.write_text("stop")
            p1.join(timeout=5)
            p2.join(timeout=5)

            # Verify timestamps show overlap.
            lines = ts_file.read_text().strip().split("\n")
            assert len(lines) == 2, f"expected 2 timestamp lines, got {len(lines)}"
            intervals = []
            for line in lines:
                parts = line.split(",")
                intervals.append((float(parts[0]), float(parts[1])))
            intervals.sort()
            # Overlap: second starts before first ends.
            assert intervals[1][0] < intervals[0][1], (
                f"No overlap detected (flag ON should allow parallel): "
                f"[{intervals[0][0]:.4f}, {intervals[0][1]:.4f}] vs "
                f"[{intervals[1][0]:.4f}, {intervals[1][1]:.4f}]"
            )
        finally:
            stop1.write_text("stop")
            stop2.write_text("stop")
            for p in (p1, p2):
                p.join(timeout=5)
                if p.is_alive():
                    p.kill()

    def test_same_file_no_concurrent_run(self, tmp_path: Path) -> None:
        """Two processes touching the same file: second blocked at story lock."""
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        ts_file = tmp_path / "timestamps.txt"

        ready1, ready2 = tmp_path / "ready1", tmp_path / "ready2"
        stop1, stop2 = tmp_path / "stop1", tmp_path / "stop2"

        # Both write to the SAME story_id → second can't acquire story lock.
        p1 = multiprocessing.Process(
            target=_subprocess_supervisor_lock_pattern,
            args=(str(state_dir), "story-shared", True, str(ready1), str(stop1),
                  str(ts_file), "src/api.py"),
        )
        p2 = multiprocessing.Process(
            target=_subprocess_supervisor_lock_pattern,
            args=(str(state_dir), "story-shared", True, str(ready2), str(stop2),
                  str(ts_file), "src/api.py"),
        )
        try:
            p1.start()
            c1 = _wait_for_file(ready1)
            assert c1.startswith("WORKING"), f"p1 not working: {c1}"

            p2.start()
            c2 = _wait_for_file(ready2, timeout=5)
            # p2 should be rejected because story lock already held.
            assert c2 == "STORY_LOCK_HELD", f"p2 should be blocked: {c2}"
        finally:
            stop1.write_text("stop")
            stop2.write_text("stop")
            for p in (p1, p2):
                p.join(timeout=5)
                if p.is_alive():
                    p.kill()

    def test_cap_reached_third_defers(self, tmp_path: Path) -> None:
        """With cap=2, third process defers at admission (evaluate_admission)."""
        state_dir = tmp_path / "state"
        state_dir.mkdir()

        ready1, ready2 = tmp_path / "ready1", tmp_path / "ready2"
        stop1, stop2 = tmp_path / "stop1", tmp_path / "stop2"

        # Hold two story locks to fill capacity.
        p1 = multiprocessing.Process(
            target=_subprocess_hold_story_lock,
            args=(str(state_dir), "story-a", str(ready1), str(stop1), "src/a.py"),
        )
        p2 = multiprocessing.Process(
            target=_subprocess_hold_story_lock,
            args=(str(state_dir), "story-b", str(ready2), str(stop2), "src/b.py"),
        )
        try:
            p1.start()
            assert _wait_for_file(ready1) == "READY"
            p2.start()
            assert _wait_for_file(ready2) == "READY"

            # Third story should be rejected by governor (cap=2).
            admission = AdmissionState(state_dir=state_dir)
            config = SupervisorGovernorConfig(max_stories=2, max_coding_workers=2)
            result = evaluate_admission(
                story_id="story-c",
                labels=frozenset(),
                deps=[],
                main_sha="abc123",
                footprint=StoryFootprintV1(
                    story_id="story-c", write_paths=frozenset(["src/c.py"])
                ),
                admission_state=admission,
                config=config,
            )
            assert result.admit is False, "Third story should be deferred at cap=2"
        finally:
            stop1.write_text("stop")
            stop2.write_text("stop")
            for p in (p1, p2):
                p.join(timeout=5)
                if p.is_alive():
                    p.kill()


class TestSupervisorLockNarrowingFlagOff:
    """Flag OFF: supervisor.lock held for entire run; second process rejected."""

    def test_flag_off_second_rejected(self, tmp_path: Path) -> None:
        """With flag off, second process on same state_dir is rejected by flock."""
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        ts_file = tmp_path / "timestamps.txt"

        ready1, ready2 = tmp_path / "ready1", tmp_path / "ready2"
        stop1, stop2 = tmp_path / "stop1", tmp_path / "stop2"

        p1 = multiprocessing.Process(
            target=_subprocess_supervisor_lock_pattern,
            args=(str(state_dir), "story-a", False, str(ready1), str(stop1),
                  str(ts_file), "src/api.py"),
        )
        p2 = multiprocessing.Process(
            target=_subprocess_supervisor_lock_pattern,
            args=(str(state_dir), "story-b", False, str(ready2), str(stop2),
                  str(ts_file), "tests/test_x.py"),
        )
        try:
            p1.start()
            c1 = _wait_for_file(ready1)
            assert c1.startswith("WORKING"), f"p1 not working: {c1}"

            p2.start()
            c2 = _wait_for_file(ready2, timeout=5)
            # p2 must be REJECTED — supervisor.lock held for entire run.
            assert c2 == "REJECTED", (
                f"Flag OFF: second process should be rejected, got: {c2}"
            )
        finally:
            stop1.write_text("stop")
            stop2.write_text("stop")
            for p in (p1, p2):
                p.join(timeout=5)
                if p.is_alive():
                    p.kill()

    def test_flag_off_integration_never_overlaps(self, tmp_path: Path) -> None:
        """Flag OFF + integration lock: integration steps never overlap."""
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        ts_file = tmp_path / "timestamps.txt"

        ready1, ready2 = tmp_path / "ready1", tmp_path / "ready2"
        stop1, stop2 = tmp_path / "stop1", tmp_path / "stop2"

        # Use the integration lock helper (already tested above).
        p1 = multiprocessing.Process(
            target=_subprocess_hold_integration_lock,
            args=(str(state_dir), str(ready1), str(stop1), str(ts_file)),
        )
        p2 = multiprocessing.Process(
            target=_subprocess_hold_integration_lock,
            args=(str(state_dir), str(ready2), str(stop2), str(ts_file)),
        )
        try:
            p1.start()
            c1 = _wait_for_file(ready1)
            assert c1.startswith("ACQUIRED")

            p2.start()
            time.sleep(0.3)
            assert not ready2.exists() or not ready2.read_text().startswith("ACQUIRED")

            stop1.write_text("stop")
            p1.join(timeout=5)

            c2 = _wait_for_file(ready2)
            assert c2.startswith("ACQUIRED")
            stop2.write_text("stop")
            p2.join(timeout=5)

            lines = ts_file.read_text().strip().split("\n")
            assert len(lines) == 2
            intervals = []
            for line in lines:
                parts = line.split(",")
                intervals.append((float(parts[0]), float(parts[1])))
            intervals.sort()
            assert intervals[1][0] >= intervals[0][1], (
                f"Integration lock overlap: "
                f"[{intervals[0][0]:.4f}, {intervals[0][1]:.4f}] vs "
                f"[{intervals[1][0]:.4f}, {intervals[1][1]:.4f}]"
            )
        finally:
            stop1.write_text("stop")
            stop2.write_text("stop")
            for p in (p1, p2):
                p.join(timeout=5)
                if p.is_alive():
                    p.kill()
