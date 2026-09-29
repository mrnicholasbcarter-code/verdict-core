"""Tests for TUI replay mode."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from verdict.orchestration.tui import follow_replay


def test_follow_replay_shows_replay_header_real_models(tmp_path: Path) -> None:
    """Replay header identifies real-model run and shows speed."""
    events_file = tmp_path / "events.jsonl"
    events = [
        {
            "at": "2026-01-01T00:00:00.000000Z",
            "seq": 0,
            "type": "run_started",
            "node_id": "",
            "data": {"goal": "test goal", "run_id": "test-run-123"},
        },
        {
            "at": "2026-01-01T00:00:00.500000Z",
            "seq": 1,
            "type": "selection",
            "node_id": "node-a",
            "data": {"route_id": "kr/claude-haiku-4.5", "attempt": 1},
        },
        {
            "at": "2026-01-01T00:00:01.000000Z",
            "seq": 2,
            "type": "run_finished",
            "node_id": "",
            "data": {"outcome": "COMPLETE"},
        },
    ]
    events_file.write_text("\n".join(json.dumps(e) for e in events))

    view = follow_replay(events_file, speed=2.0, max_gap=0.1)

    # Header should show REPLAY marker with speed and "real models"
    assert "REPLAY" in view.goal
    assert "test-run-123" in view.goal or tmp_path.name in view.goal
    assert "x2.0" in view.goal
    assert "real models" in view.goal


def test_follow_replay_shows_fixture_label(tmp_path: Path) -> None:
    """Replay header identifies fixture run when routes start with demo-/fixture."""
    events_file = tmp_path / "events.jsonl"
    events = [
        {
            "at": "2026-01-01T00:00:00.000000Z",
            "seq": 0,
            "type": "run_started",
            "node_id": "",
            "data": {"goal": "test goal", "run_id": "fixture-run-456"},
        },
        {
            "at": "2026-01-01T00:00:00.500000Z",
            "seq": 1,
            "type": "selection",
            "node_id": "node-a",
            "data": {"route_id": "demo-sub/atlas-coder", "attempt": 1},
        },
        {
            "at": "2026-01-01T00:00:01.000000Z",
            "seq": 2,
            "type": "run_finished",
            "node_id": "",
            "data": {"outcome": "COMPLETE"},
        },
    ]
    events_file.write_text("\n".join(json.dumps(e) for e in events))

    view = follow_replay(events_file, speed=1.0, max_gap=0.1)

    # Header should show REPLAY marker with "fixture run" and "no model calls"
    assert "REPLAY" in view.goal
    assert "fixture run" in view.goal
    assert "no model calls" in view.goal
    assert "real models" not in view.goal


def test_follow_replay_unknown_routes_no_real_models_claim(tmp_path: Path) -> None:
    """Replay header does not claim 'real models' when routes are unknown."""
    events_file = tmp_path / "events.jsonl"
    events = [
        {
            "at": "2026-01-01T00:00:00.000000Z",
            "seq": 0,
            "type": "run_started",
            "node_id": "",
            "data": {"goal": "test goal"},
        },
        {
            "at": "2026-01-01T00:00:01.000000Z",
            "seq": 1,
            "type": "run_finished",
            "node_id": "",
            "data": {"outcome": "COMPLETE"},
        },
    ]
    events_file.write_text("\n".join(json.dumps(e) for e in events))

    view = follow_replay(events_file, speed=1.0, max_gap=0.1)

    # Without selection events, should not claim "real models"
    # Should show generic REPLAY label
    assert "REPLAY" in view.goal
    assert "real models" not in view.goal  # no executed routes -> no claim
    assert "fixture" not in view.goal


def test_follow_replay_deterministic(tmp_path: Path) -> None:
    """Replay produces deterministic view from events."""
    events_file = tmp_path / "events.jsonl"
    events = [
        {
            "at": "2026-01-01T00:00:00Z",
            "seq": 0,
            "type": "run_started",
            "node_id": "",
            "data": {"goal": "build feature X"},
        },
        {
            "at": "2026-01-01T00:00:01Z",
            "seq": 1,
            "type": "topology",
            "node_id": "",
            "data": {
                "topology": "sequential",
                "rationale": ["simple"],
                "max_parallel": 1,
                "layers": [["node-a"]],
            },
        },
        {
            "at": "2026-01-01T00:00:02Z",
            "seq": 2,
            "type": "run_finished",
            "node_id": "",
            "data": {"outcome": "COMPLETE"},
        },
    ]
    events_file.write_text("\n".join(json.dumps(e) for e in events))

    # Run twice
    view1 = follow_replay(events_file, speed=10.0, max_gap=0.1)
    view2 = follow_replay(events_file, speed=10.0, max_gap=0.1)

    # Both should have same outcome
    assert view1.outcome == view2.outcome == "COMPLETE"
    assert view1.topology == view2.topology == "sequential"
    assert view1.event_count == view2.event_count == 3


def test_follow_replay_speed_scaling(tmp_path: Path) -> None:
    """Speed parameter scales timing."""
    import time

    events_file = tmp_path / "events.jsonl"
    events = [
        {
            "at": "2026-01-01T00:00:00.000000Z",
            "seq": 0,
            "type": "run_started",
            "node_id": "",
            "data": {"goal": "test"},
        },
        {
            "at": "2026-01-01T00:00:00.500000Z",  # 0.5s gap
            "seq": 1,
            "type": "run_finished",
            "node_id": "",
            "data": {"outcome": "COMPLETE"},
        },
    ]
    events_file.write_text("\n".join(json.dumps(e) for e in events))

    # With speed=10.0, 0.5s gap should become ~0.05s
    start = time.time()
    view = follow_replay(events_file, speed=10.0, max_gap=2.0)
    elapsed = time.time() - start

    # Should complete in well under 0.5s (adding some margin for processing)
    assert elapsed < 0.4  # 0.05s gap + margins
    assert view.outcome == "COMPLETE"


def test_follow_replay_max_gap_caps_delays(tmp_path: Path) -> None:
    """max_gap caps individual event delays."""
    import time

    events_file = tmp_path / "events.jsonl"
    events = [
        {
            "at": "2026-01-01T00:00:00.000000Z",
            "seq": 0,
            "type": "run_started",
            "node_id": "",
            "data": {"goal": "test"},
        },
        {
            "at": "2026-01-01T00:01:00.000000Z",  # 60s gap
            "seq": 1,
            "type": "run_finished",
            "node_id": "",
            "data": {"outcome": "COMPLETE"},
        },
    ]
    events_file.write_text("\n".join(json.dumps(e) for e in events))

    # With max_gap=0.2, the 60s gap should be capped to 0.2s
    start = time.time()
    view = follow_replay(events_file, speed=1.0, max_gap=0.2)
    elapsed = time.time() - start

    # Should complete in ~0.2s + processing + final pause (0.5s)
    assert elapsed < 1.2  # 0.2 gap + 0.5 pause + margins
    assert view.outcome == "COMPLETE"


def test_replay_from_fixture_events(tmp_path: Path) -> None:
    """Replay renders from a fixture events file."""
    # Use one of the committed proof runs if available
    fixture = Path(__file__).parent.parent / "docs" / "proof" / "demo-run" / "events.jsonl"
    if not fixture.exists():
        pytest.skip("Fixture events.jsonl not found")

    # Should not crash and should produce a view
    view = follow_replay(fixture, speed=100.0, max_gap=0.05)

    # Basic sanity checks
    assert view.event_count > 0
    assert view.goal  # Should have a goal
    assert "REPLAY" in view.goal
    assert "fixture run" in view.goal  # demo-run uses demo-sub/* routes


def test_replay_kind_needs_evidence_for_real_models() -> None:
    from types import SimpleNamespace

    from verdict.orchestration.tui import _replay_kind

    def ev(t: str, route: str) -> SimpleNamespace:
        return SimpleNamespace(type=t, data={"route_id": route})

    def run_started(run_id: str = "", mode: str = "") -> SimpleNamespace:
        return SimpleNamespace(type="run_started", data={"run_id": run_id, "mode": mode})

    assert _replay_kind([]) == "unknown"
    assert _replay_kind([ev("node_state", "")]) == "unknown"
    # Real provider route with a generic name → still 'real'
    assert _replay_kind([ev("selection", "alpha/real-provider")]) == "real"
    assert _replay_kind([ev("selection", "kr/claude-haiku-4.5")]) == "real"
    assert _replay_kind([ev("selection", "kr/x"), ev("dispatch", "demo-sub/atlas")]) == "fixture"
    # mode marker from demo_scenario.py → 'fixture' regardless of route name
    assert (
        _replay_kind([run_started(mode="offline-scenario"), ev("selection", "alpha/real-looking")])
        == "fixture"
    )
    # offline- run_id prefix → 'fixture'
    assert (
        _replay_kind([run_started(run_id="offline-flagship-failover"), ev("selection", "alpha/x")])
        == "fixture"
    )
    # Real run with alpha/ route and no offline marker → 'real'
    assert _replay_kind([run_started(run_id="live-run-001"), ev("selection", "alpha/x")]) == "real"
    # Env var MUST NOT be used: mode is passed explicitly, never via os.environ
    import subprocess

    result = subprocess.run(
        ["grep", "-r", "VERDICT_RUN_MODE", "verdict/", "scripts/"], capture_output=True, text=True
    )
    assert result.returncode != 0, (
        "VERDICT_RUN_MODE env var found in source - use explicit mode= parameter instead:\n"
        + result.stdout
    )
