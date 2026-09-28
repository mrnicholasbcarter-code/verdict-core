"""Per-story state isolation tests for BOD-157 (VERDICT_MULTI_STORY=on).

Real subprocess tests verifying:
- Two supervisors with different --story and disjoint footprints: both admitted,
  state files are separate under <state_dir>/stories/<safe_id>/.
- Same --story twice: second rejected (lock held).
- Flag on without --story: argparse error.
- Flag off: unchanged parity (no --story needed, no stories/ subdir).
"""

from __future__ import annotations

import importlib.util
import json
import multiprocessing
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
_PYTHON = sys.executable


def _load_supervisor() -> Any:
    """Load the supervisor script as a module."""
    path = ROOT / "scripts" / "prime_supervisor.py"
    assert path.exists(), "supervisor script missing"
    spec = importlib.util.spec_from_file_location("prime_supervisor", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _git_init(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=T",
            "-c",
            "user.email=t@e.invalid",
            "commit",
            "--allow-empty",
            "-qm",
            "base",
        ],
        check=True,
    )


# ---------------------------------------------------------------------------
# Helpers: safe dirname / story state dir
# ---------------------------------------------------------------------------


class TestSafeStoryDirname:
    """Unit tests for _safe_story_dirname and _story_state_dir helpers."""

    def test_slashes_replaced(self) -> None:
        m = _load_supervisor()
        assert m._safe_story_dirname("BOD-157/sub") == "BOD-157_sub"

    def test_backslash_replaced(self) -> None:
        m = _load_supervisor()
        assert m._safe_story_dirname("BOD\\157") == "BOD_157"

    def test_null_replaced(self) -> None:
        m = _load_supervisor()
        assert m._safe_story_dirname("BOD\x00157") == "BOD_157"

    def test_plain_id_unchanged(self) -> None:
        m = _load_supervisor()
        assert m._safe_story_dirname("BOD-157") == "BOD-157"

    def test_story_state_dir_created(self, tmp_path: Path) -> None:
        m = _load_supervisor()
        d = m._story_state_dir(tmp_path, "BOD-42")
        assert d == tmp_path / "stories" / "BOD-42"
        assert d.is_dir()


# ---------------------------------------------------------------------------
# _read_story_metadata with story_id_override
# ---------------------------------------------------------------------------


class TestReadStoryMetadataOverride:
    """story_id_override from --story must be authoritative and fail-closed on mismatch."""

    def test_override_sets_story_id(self, tmp_path: Path) -> None:
        m = _load_supervisor()
        meta = m._read_story_metadata(tmp_path, story_id_override="BOD-42")
        assert meta["story_id"] == "BOD-42"

    def test_override_matches_checkpoint(self, tmp_path: Path) -> None:
        m = _load_supervisor()
        (tmp_path / "checkpoint.json").write_text(json.dumps({"issue": "BOD-42"}))
        meta = m._read_story_metadata(tmp_path, story_id_override="BOD-42")
        assert meta["story_id"] == "BOD-42"

    def test_override_mismatch_raises(self, tmp_path: Path) -> None:
        m = _load_supervisor()
        (tmp_path / "checkpoint.json").write_text(json.dumps({"issue": "BOD-99"}))
        with pytest.raises(ValueError, match="does not match checkpoint"):
            m._read_story_metadata(tmp_path, story_id_override="BOD-42")

    def test_no_override_falls_back_to_checkpoint(self, tmp_path: Path) -> None:
        m = _load_supervisor()
        (tmp_path / "checkpoint.json").write_text(json.dumps({"issue": "BOD-99"}))
        meta = m._read_story_metadata(tmp_path)
        assert meta["story_id"] == "BOD-99"

    def test_no_override_no_checkpoint_falls_back_to_dirname(self, tmp_path: Path) -> None:
        m = _load_supervisor()
        state = tmp_path / "my-state"
        state.mkdir()
        meta = m._read_story_metadata(state)
        assert meta["story_id"] == "supervisor-my-state"


# ---------------------------------------------------------------------------
# Flag on without --story -> error
# ---------------------------------------------------------------------------


class TestFlagOnWithoutStoryErrors:
    """VERDICT_MULTI_STORY=on without --story must fail at argparse level."""

    def test_flag_on_no_story_exits(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("VERDICT_MULTI_STORY", "on")
        monkeypatch.setenv("VERDICT_TEST_MODE", "1")
        result = subprocess.run(
            [_PYTHON, str(ROOT / "scripts" / "prime_supervisor.py"), "--skip-identity-verify"],
            capture_output=True,
            text=True,
            timeout=10,
            env={**os.environ, "VERDICT_MULTI_STORY": "on", "VERDICT_TEST_MODE": "1"},
        )
        assert result.returncode != 0
        assert "--story is required" in result.stderr


# ---------------------------------------------------------------------------
# Flag off -> unchanged parity (no stories/ subdir)
# ---------------------------------------------------------------------------


class TestFlagOffParity:
    """Flag off must not create per-story subdirs and must ignore --story."""

    def test_check_admission_returns_none(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        m = _load_supervisor()
        monkeypatch.delenv("VERDICT_MULTI_STORY", raising=False)
        _git_init(tmp_path)
        result, handle = m._check_admission(tmp_path, tmp_path / "state")
        assert result is None
        assert handle is None

    def test_no_stories_subdir_when_flag_off(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        m = _load_supervisor()
        monkeypatch.delenv("VERDICT_MULTI_STORY", raising=False)
        state = tmp_path / "state"
        state.mkdir()
        # _check_admission with flag off does nothing.
        result, _ = m._check_admission(tmp_path, state)
        assert result is None
        assert not (state / "stories").exists()


# ---------------------------------------------------------------------------
# Two supervisors, different --story, disjoint footprints -> both admitted,
# state files are separate
# ---------------------------------------------------------------------------


def _hold_story_and_write_state(
    state_dir: str, story_id: str, ready_file: str, stop_file: str, footprint_paths: str
) -> None:
    """Subprocess entry: acquire story lock, write per-story state, signal ready."""
    from pathlib import Path as _Path

    from verdict.orchestration.supervisor_admission import (
        StoryFootprintV1,
        acquire_story_lock,
        write_story_metadata,
    )

    sd = _Path(state_dir)
    wp = frozenset(footprint_paths.split(",")) if footprint_paths else frozenset()
    handle = acquire_story_lock(sd, story_id)
    if handle is None:
        _Path(ready_file).write_text("FAILED")
        return
    fp = StoryFootprintV1(story_id=story_id, write_paths=wp)
    write_story_metadata(handle, fp)

    # Simulate per-story state files under stories/<safe_id>/
    import importlib.util as ilu

    spec = ilu.spec_from_file_location(
        "ps", str(_Path(__file__).resolve().parents[1] / "scripts" / "prime_supervisor.py")
    )
    assert spec is not None and spec.loader is not None
    mod = ilu.module_from_spec(spec)
    spec.loader.exec_module(mod)
    story_state = mod._story_state_dir(sd, story_id)
    (story_state / "checkpoint.json").write_text(json.dumps({"issue": story_id}))
    (story_state / "supervisor.json").write_text(json.dumps({"status": "RUNNING"}))

    _Path(ready_file).write_text("READY")
    while not _Path(stop_file).exists():
        time.sleep(0.05)
    handle.release()


def _wait_file(path: Path, timeout: float = 10.0) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            content = path.read_text().strip()
            if content:
                return content
        time.sleep(0.05)
    raise TimeoutError(f"timed out waiting for {path}")


class TestTwoStoriesSeparateState:
    """Two supervisors with different --story: both admitted, state files separate."""

    @pytest.fixture()
    def env(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path, Any]:
        monkeypatch.setenv("VERDICT_MULTI_STORY", "on")
        state = tmp_path / "state"
        state.mkdir()
        repo = tmp_path / "repo"
        _git_init(repo)
        m = _load_supervisor()
        return state, repo, m

    def test_disjoint_stories_both_admitted(
        self, env: tuple[Path, Path, Any], tmp_path: Path
    ) -> None:
        state, _repo, _m = env
        # Launch story A in a subprocess
        ready_a = tmp_path / "ready_a"
        stop_a = tmp_path / "stop_a"
        proc_a = multiprocessing.Process(
            target=_hold_story_and_write_state,
            args=(str(state), "BOD-A", str(ready_a), str(stop_a), "docs/a.md"),
        )
        proc_a.start()
        assert _wait_file(ready_a) == "READY"

        # Launch story B in a subprocess
        ready_b = tmp_path / "ready_b"
        stop_b = tmp_path / "stop_b"
        proc_b = multiprocessing.Process(
            target=_hold_story_and_write_state,
            args=(str(state), "BOD-B", str(ready_b), str(stop_b), "tests/b.py"),
        )
        proc_b.start()
        assert _wait_file(ready_b) == "READY"

        try:
            # Both should be admitted (disjoint footprints)
            # Verify separate state directories
            story_a_dir = state / "stories" / "BOD-A"
            story_b_dir = state / "stories" / "BOD-B"
            assert story_a_dir.is_dir(), "story A state dir should exist"
            assert story_b_dir.is_dir(), "story B state dir should exist"

            # Each story has its own checkpoint.json
            cp_a = json.loads((story_a_dir / "checkpoint.json").read_text())
            cp_b = json.loads((story_b_dir / "checkpoint.json").read_text())
            assert cp_a["issue"] == "BOD-A"
            assert cp_b["issue"] == "BOD-B"

            # Each story has its own supervisor.json
            sup_a = json.loads((story_a_dir / "supervisor.json").read_text())
            sup_b = json.loads((story_b_dir / "supervisor.json").read_text())
            assert sup_a["status"] == "RUNNING"
            assert sup_b["status"] == "RUNNING"

            # Shared locks dir is at root, not per-story
            assert (state / "story-locks").is_dir()
            assert not (story_a_dir / "story-locks").exists()
            assert not (story_b_dir / "story-locks").exists()
        finally:
            stop_a.write_text("stop")
            stop_b.write_text("stop")
            proc_a.join(timeout=5)
            proc_b.join(timeout=5)


# ---------------------------------------------------------------------------
# Same --story twice -> second rejected (lock held)
# ---------------------------------------------------------------------------


class TestSameStoryRejected:
    """Same --story launched twice: second must be rejected by story lock."""

    def test_same_story_lock_blocks(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("VERDICT_MULTI_STORY", "on")
        state = tmp_path / "state"
        state.mkdir()
        repo = tmp_path / "repo"
        _git_init(repo)

        # First process holds the lock
        ready_1 = tmp_path / "ready_1"
        stop_1 = tmp_path / "stop_1"
        proc_1 = multiprocessing.Process(
            target=_hold_story_and_write_state,
            args=(str(state), "BOD-SAME", str(ready_1), str(stop_1), "src/app.py"),
        )
        proc_1.start()
        assert _wait_file(ready_1) == "READY"

        try:
            # Second process tries the same story lock -> should fail
            from verdict.orchestration.supervisor_admission import acquire_story_lock

            handle = acquire_story_lock(state, "BOD-SAME")
            assert handle is None, "second lock acquisition should fail"
        finally:
            stop_1.write_text("stop")
            proc_1.join(timeout=5)

    def test_same_story_admission_rejected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("VERDICT_MULTI_STORY", "on")
        m = _load_supervisor()
        state = tmp_path / "state"
        state.mkdir()
        repo = tmp_path / "repo"
        _git_init(repo)

        # Write checkpoint in per-story dir for the first story
        story_state = m._story_state_dir(state, "BOD-SAME")
        (story_state / "checkpoint.json").write_text(
            json.dumps({"issue": "BOD-SAME", "write_paths": ["src/app.py"]})
        )

        # First process holds the lock
        ready_1 = tmp_path / "ready_1"
        stop_1 = tmp_path / "stop_1"
        proc_1 = multiprocessing.Process(
            target=_hold_story_and_write_state,
            args=(str(state), "BOD-SAME", str(ready_1), str(stop_1), "src/app.py"),
        )
        proc_1.start()
        assert _wait_file(ready_1) == "READY"

        try:
            # _check_admission for the same story: the running story has
            # the same write_paths, so the collision gate rejects first
            # (SERIALIZE).  If footprints were disjoint the governor would
            # still reject with ALREADY_RUNNING / LOCK_HELD.  Either way,
            # the second supervisor is not admitted.
            result, handle = m._check_admission(repo, state, story_id_override="BOD-SAME")
            assert result is not None
            assert result["admit"] is False
            assert result["reason_code"] in {"SERIALIZE", "LOCK_HELD"}
            assert handle is None
        finally:
            stop_1.write_text("stop")
            proc_1.join(timeout=5)


# ---------------------------------------------------------------------------
# Prompt includes story id when --story is provided
# ---------------------------------------------------------------------------


class TestPromptIncludesStoryId:
    """The controller prompt must include the story id for targeted work."""

    def test_instruction_unit_includes_story(self) -> None:
        m = _load_supervisor()
        unit = m._supervisor_instruction_unit(
            token="test-1",
            state_dir=Path("/tmp/state"),
            session_dir=Path("/tmp/state/sessions/test-1"),
            max_issues=5,
            timeout=3600.0,
            story_id="BOD-42",
        )
        assert "Story=BOD-42" in unit.content
        assert "work ONLY on this story" in unit.content

    def test_instruction_unit_no_story_when_none(self) -> None:
        m = _load_supervisor()
        unit = m._supervisor_instruction_unit(
            token="test-1",
            state_dir=Path("/tmp/state"),
            session_dir=Path("/tmp/state/sessions/test-1"),
            max_issues=5,
            timeout=3600.0,
        )
        assert "Story=" not in unit.content
        assert "work ONLY on this story" not in unit.content
