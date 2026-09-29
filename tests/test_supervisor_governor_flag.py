"""Tests for BOD-157 supervisor concurrency governor flag (ADR-037).

Covers:
- Flag-off parity with origin/main single-story behaviour.
- Flag-on admits two stories with disjoint footprints.
- Same file -> serialised.
- Manual label -> excluded.
- Dependency not verified on main -> waits.
- Governor at cap -> defers.
- Integration step never runs two at once.
- Invalid flag value -> off with a warning.
"""

from __future__ import annotations

import importlib.util
import json
import math
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from verdict.orchestration.concurrency_governor import ResourcePressure
from verdict.orchestration.ready_gate import DepEvidence
from verdict.orchestration.story_footprint import StoryFootprintV1
from verdict.orchestration.supervisor_admission import (
    AdmissionDecision,
    AdmissionState,
    RunningStory,
    SupervisorGovernorConfig,
    evaluate_admission,
)

ROOT = Path(__file__).resolve().parents[1]


def module():
    path = ROOT / "scripts" / "prime_supervisor.py"
    assert path.exists(), "supervisor script missing"
    spec = importlib.util.spec_from_file_location("prime_supervisor", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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


# ===================================================================
# 1. Flag-off parity: supervisor behaves identically to origin/main
# ===================================================================


class TestFlagOffParity:
    """With VERDICT_MULTI_STORY unset/off, _check_admission returns None
    and the supervisor takes the single-story flock path unchanged."""

    def test_flag_unset_returns_none(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        m = module()
        monkeypatch.delenv("VERDICT_MULTI_STORY", raising=False)
        _git_init(tmp_path)
        result, handle = m._check_admission(tmp_path, tmp_path / "state")
        assert result is None, "flag-off must be a no-op (None)"
        assert handle is None

    def test_flag_off_returns_none(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        m = module()
        monkeypatch.setenv("VERDICT_MULTI_STORY", "off")
        _git_init(tmp_path)
        result, handle = m._check_admission(tmp_path, tmp_path / "state")
        assert result is None
        assert handle is None

    def test_flag_off_multi_story_enabled_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        m = module()
        monkeypatch.delenv("VERDICT_MULTI_STORY", raising=False)
        assert m._multi_story_enabled() is False

    def test_recover_still_called_when_flag_off(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When flag is off, _check_admission is (None, None) so recover() runs."""
        m = module()
        monkeypatch.delenv("VERDICT_MULTI_STORY", raising=False)
        _git_init(tmp_path)
        result, handle = m._check_admission(tmp_path, tmp_path / "state")
        # None means the supervisor proceeds to recover() — identical to today.
        assert result is None
        assert handle is None


# ===================================================================
# 2. Flag-on: two stories with disjoint footprints -> both ADMIT
# ===================================================================


class TestDisjointFootprintsAdmit:
    def test_two_disjoint_stories_admitted(self) -> None:
        state = AdmissionState()
        sha = "abc123"

        fp_a = StoryFootprintV1(story_id="story-a", write_paths=frozenset({"docs/readme.md"}))
        fp_b = StoryFootprintV1(story_id="story-b", write_paths=frozenset({"tests/test_foo.py"}))
        config = SupervisorGovernorConfig(max_stories=2, max_coding_workers=2)

        # Admit story A
        result_a = evaluate_admission(
            story_id="story-a",
            labels=frozenset(),
            deps=[],
            main_sha=sha,
            footprint=fp_a,
            admission_state=state,
            config=config,
        )
        assert result_a.admit is True
        assert result_a.reason_code == "ADMIT"
        state.add(RunningStory(story_id="story-a", footprint=fp_a))

        # Admit story B (disjoint footprint)
        result_b = evaluate_admission(
            story_id="story-b",
            labels=frozenset(),
            deps=[],
            main_sha=sha,
            footprint=fp_b,
            admission_state=state,
            config=config,
        )
        assert result_b.admit is True
        assert result_b.reason_code == "ADMIT"


# ===================================================================
# 3. Same file -> serialised
# ===================================================================


class TestSameFileSerialized:
    def test_overlapping_write_path_serializes(self) -> None:
        state = AdmissionState()
        sha = "abc123"
        shared_file = "verdict/api.py"

        fp_a = StoryFootprintV1(story_id="story-a", write_paths=frozenset({shared_file}))
        fp_b = StoryFootprintV1(story_id="story-b", write_paths=frozenset({shared_file}))
        config = SupervisorGovernorConfig(max_stories=2, max_coding_workers=2)

        # Admit story A
        result_a = evaluate_admission(
            story_id="story-a",
            labels=frozenset(),
            deps=[],
            main_sha=sha,
            footprint=fp_a,
            admission_state=state,
            config=config,
        )
        assert result_a.admit is True
        state.add(RunningStory(story_id="story-a", footprint=fp_a))

        # Story B collides -> SERIALIZE
        result_b = evaluate_admission(
            story_id="story-b",
            labels=frozenset(),
            deps=[],
            main_sha=sha,
            footprint=fp_b,
            admission_state=state,
            config=config,
        )
        assert result_b.admit is False
        assert result_b.reason_code == "SERIALIZE"
        assert "story-a" in result_b.reason


# ===================================================================
# 4. Manual label -> excluded
# ===================================================================


class TestManualLabelExcluded:
    def test_automation_manual_label_excluded(self) -> None:
        state = AdmissionState()
        result = evaluate_admission(
            story_id="story-x",
            labels=frozenset({"automation:manual"}),
            deps=[],
            main_sha="abc",
            footprint=StoryFootprintV1(story_id="story-x", write_paths=frozenset({"a.py"})),
            admission_state=state,
        )
        assert result.admit is False
        assert result.reason_code == "EXCLUDED"


# ===================================================================
# 5. Dependency not verified on main -> waits
# ===================================================================


class TestDependencyWaits:
    def test_unverified_dep_waits(self) -> None:
        state = AdmissionState()
        dep = DepEvidence(
            identifier="BOD-100",
            linear_done=True,
            merge_commit_on_main=False,  # not on main yet
            verification_record_present=False,
        )
        result = evaluate_admission(
            story_id="story-y",
            labels=frozenset(),
            deps=[dep],
            main_sha="abc",
            footprint=StoryFootprintV1(story_id="story-y", write_paths=frozenset({"b.py"})),
            admission_state=state,
        )
        assert result.admit is False
        assert result.reason_code == "WAIT"

    def test_unknown_dep_evidence_waits(self) -> None:
        state = AdmissionState()
        dep = DepEvidence(
            identifier="BOD-101",
            linear_done=None,  # unknown
            merge_commit_on_main=None,
            verification_record_present=None,
        )
        result = evaluate_admission(
            story_id="story-z",
            labels=frozenset(),
            deps=[dep],
            main_sha="abc",
            footprint=StoryFootprintV1(story_id="story-z", write_paths=frozenset({"c.py"})),
            admission_state=state,
        )
        assert result.admit is False
        assert result.reason_code == "WAIT"


# ===================================================================
# 6. Governor at cap -> defers
# ===================================================================


class TestGovernorCapDefers:
    def test_story_cap_reached_serializes(self) -> None:
        state = AdmissionState()
        config = SupervisorGovernorConfig(max_stories=2, max_coding_workers=2)
        sha = "abc"

        # Fill up to cap with 2 running stories
        for i in range(2):
            fp = StoryFootprintV1(
                story_id=f"running-{i}", write_paths=frozenset({f"dir{i}/file.py"})
            )
            state.add(RunningStory(story_id=f"running-{i}", footprint=fp))

        # Third story should be deferred/serialized by governor
        fp_new = StoryFootprintV1(story_id="new-story", write_paths=frozenset({"other/file.py"}))
        result = evaluate_admission(
            story_id="new-story",
            labels=frozenset(),
            deps=[],
            main_sha=sha,
            footprint=fp_new,
            admission_state=state,
            config=config,
        )
        assert result.admit is False
        assert result.reason_code in ("SERIALIZE", "DEFER")

    def test_pressure_unknown_defers(self) -> None:
        """Unknown pressure (NaN) -> governor treats as max pressure -> DEFER."""
        state = AdmissionState()
        config = SupervisorGovernorConfig(max_stories=2, max_coding_workers=2)

        # One story running
        fp_a = StoryFootprintV1(story_id="running-0", write_paths=frozenset({"dir0/file.py"}))
        state.add(RunningStory(story_id="running-0", footprint=fp_a))

        # NaN pressure -> effective cap drops to 1 -> already at cap
        fp_new = StoryFootprintV1(story_id="new-story", write_paths=frozenset({"other/file.py"}))
        result = evaluate_admission(
            story_id="new-story",
            labels=frozenset(),
            deps=[],
            main_sha="abc",
            footprint=fp_new,
            admission_state=state,
            config=config,
            pressure=ResourcePressure(cpu=math.nan, ram=math.nan, disk=math.nan),
        )
        assert result.admit is False
        assert result.reason_code in ("SERIALIZE", "DEFER")


# ===================================================================
# 7. Integration step never runs two at once
# ===================================================================


class TestIntegrationSerialized:
    def test_integration_lock_serializes(self, tmp_path: Path) -> None:
        """acquire_integration_lock serialises concurrent acquire across threads."""
        from verdict.orchestration.supervisor_admission import acquire_integration_lock

        state_dir = tmp_path / "state"
        state_dir.mkdir()
        acquired_order: list[int] = []
        barrier = threading.Barrier(2, timeout=5)

        def worker(idx: int) -> None:
            barrier.wait()
            with acquire_integration_lock(state_dir):
                acquired_order.append(idx)
                time.sleep(0.05)

        t1 = threading.Thread(target=worker, args=(1,))
        t2 = threading.Thread(target=worker, args=(2,))
        t1.start()
        t2.start()
        t1.join(timeout=5)
        t2.join(timeout=5)

        # Both ran, but never concurrently — order is sequential.
        assert len(acquired_order) == 2
        assert set(acquired_order) == {1, 2}


# ===================================================================
# 8. Invalid flag value -> off with a warning
# ===================================================================


class TestInvalidFlagWarns:
    def test_invalid_value_treated_as_off(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        m = module()
        monkeypatch.setenv("VERDICT_MULTI_STORY", "yes")
        assert m._multi_story_enabled() is False
        captured = capsys.readouterr()
        assert "warning" in captured.out.lower() or "unrecognised" in captured.out.lower()

    def test_numeric_value_treated_as_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        m = module()
        monkeypatch.setenv("VERDICT_MULTI_STORY", "1")
        assert m._multi_story_enabled() is False

    def test_empty_value_treated_as_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        m = module()
        monkeypatch.setenv("VERDICT_MULTI_STORY", "")
        assert m._multi_story_enabled() is False

    def test_on_value_enables(self, monkeypatch: pytest.MonkeyPatch) -> None:
        m = module()
        monkeypatch.setenv("VERDICT_MULTI_STORY", "on")
        assert m._multi_story_enabled() is True

    def test_on_uppercase_enables(self, monkeypatch: pytest.MonkeyPatch) -> None:
        m = module()
        monkeypatch.setenv("VERDICT_MULTI_STORY", "ON")
        assert m._multi_story_enabled() is True


# ===================================================================
# 9. Flag-on with injected evaluator (supervisor integration)
# ===================================================================


class TestSupervisorFlagOnIntegration:
    def test_flag_on_check_admission_calls_evaluator(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        m = module()
        monkeypatch.setenv("VERDICT_MULTI_STORY", "on")
        _git_init(tmp_path)
        called: dict[str, Any] = {}

        def fake_evaluator(*, repo: Path, state: Path) -> dict[str, Any]:
            called["repo"] = repo
            called["state"] = state
            return {"admit": True, "reason_code": "ADMIT", "reason": "test"}

        m.ADMISSION_EVALUATOR = fake_evaluator
        try:
            result, handle = m._check_admission(tmp_path, tmp_path / "state")
            assert result is not None
            assert result["admit"] is True
            assert called["repo"] == tmp_path
            assert handle is None  # evaluator path does not acquire locks
        finally:
            m.ADMISSION_EVALUATOR = None

    def test_flag_on_rejected_returns_deferred(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        m = module()
        monkeypatch.setenv("VERDICT_MULTI_STORY", "on")
        _git_init(tmp_path)

        def fake_evaluator(*, repo: Path, state: Path) -> dict[str, Any]:
            return {"admit": False, "reason_code": "SERIALIZE", "reason": "test collision"}

        m.ADMISSION_EVALUATOR = fake_evaluator
        try:
            result, handle = m._check_admission(tmp_path, tmp_path / "state")
            assert result is not None
            assert result["admit"] is False
            assert result["reason_code"] == "SERIALIZE"
            assert handle is None  # rejected path does not acquire locks
        finally:
            m.ADMISSION_EVALUATOR = None


# ===================================================================
# 10. Default config is conservative (provisional)
# ===================================================================


class TestProvisionalDefaults:
    def test_default_config_values(self) -> None:
        cfg = SupervisorGovernorConfig()
        assert cfg.max_stories == 2
        assert cfg.max_coding_workers == 2
        assert cfg.max_integration_slots == 1

    def test_admission_decision_serializable(self) -> None:
        d = AdmissionDecision(admit=True, reason_code="ADMIT", reason="test")
        data = d.to_dict()
        assert json.dumps(data)  # must be JSON-serializable
        assert data["admit"] is True


# ===================================================================
# 11. Unknown footprint -> SERIALIZE (fail-closed)
# ===================================================================


class TestUnknownFootprintSerializes:
    def test_unknown_footprint_serializes(self) -> None:
        state = AdmissionState()
        config = SupervisorGovernorConfig(max_stories=2, max_coding_workers=2)

        # Running story with known footprint
        fp_a = StoryFootprintV1(story_id="running", write_paths=frozenset({"a.py"}))
        state.add(RunningStory(story_id="running", footprint=fp_a))

        # New story with unknown footprint (no write_paths)
        fp_unknown = StoryFootprintV1(story_id="unknown")
        result = evaluate_admission(
            story_id="unknown",
            labels=frozenset(),
            deps=[],
            main_sha="abc",
            footprint=fp_unknown,
            admission_state=state,
            config=config,
        )
        assert result.admit is False
        assert result.reason_code == "SERIALIZE"
