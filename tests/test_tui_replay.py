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

    # Should complete in ~0.2s + processing (no final pause: removed by BOD-280)
    assert elapsed < 1.0  # 0.2 gap + margins
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

    assert _replay_kind([]) == "unknown"
    assert _replay_kind([ev("node_state", "")]) == "unknown"
    assert _replay_kind([ev("selection", "kr/claude-haiku-4.5")]) == "real"
    assert _replay_kind([ev("selection", "kr/x"), ev("dispatch", "demo-sub/atlas")]) == "fixture"


# ---------------------------------------------------------------------------
# BOD-280: New tests for reduced-motion / no-sleep / sync-output / tmux
# ---------------------------------------------------------------------------


def _make_events(tmp_path: Path, *, gap_s: float = 0.5) -> Path:
    """Write two-event jsonl with the given inter-event gap."""
    import datetime

    t0 = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)
    t1 = t0 + datetime.timedelta(seconds=gap_s)

    events = [
        {
            "at": t0.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            "seq": 0,
            "type": "run_started",
            "node_id": "",
            "data": {"goal": "test goal", "run_id": "test-run-abc"},
        },
        {
            "at": t1.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            "seq": 1,
            "type": "run_finished",
            "node_id": "",
            "data": {"outcome": "COMPLETE"},
        },
    ]
    f = tmp_path / "events.jsonl"
    import json

    f.write_text("\n".join(json.dumps(e) for e in events))
    return f


def test_replay_with_animation_disabled_renders_statically(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """VERDICT_NO_ANIMATION: replay must NOT start a Live context."""
    import io

    from rich.console import Console

    from verdict.orchestration.tui import follow_replay

    monkeypatch.setenv("VERDICT_NO_ANIMATION", "1")
    monkeypatch.delenv("NO_COLOR", raising=False)

    stream = io.StringIO()
    console = Console(file=stream, force_terminal=True, width=80, color_system=None)

    live_started: list[bool] = []

    import rich.live as _rl

    original_start = _rl.Live.start

    def _patched_start(self: _rl.Live, *args: object, **kwargs: object) -> None:
        live_started.append(True)
        original_start(self, *args, **kwargs)

    monkeypatch.setattr(_rl.Live, "start", _patched_start)

    events_file = _make_events(tmp_path, gap_s=0.1)
    follow_replay(events_file, console=console, speed=100.0, max_gap=0.05)

    assert not live_started, "Live must not be started when animation is disabled"


def test_replay_no_decorative_sleep_after_final_event(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No time.sleep() call after the last event (the 0.5-s 'see final state' pause)."""
    import io

    from rich.console import Console

    from verdict.orchestration.tui import follow_replay

    monkeypatch.setenv("VERDICT_NO_ANIMATION", "1")

    stream = io.StringIO()
    console = Console(file=stream, force_terminal=False, width=80)

    sleep_calls: list[float] = []

    import time as _time_mod

    original_sleep = _time_mod.sleep

    def _patched_sleep(s: float) -> None:
        sleep_calls.append(s)
        original_sleep(0.0)  # don't actually wait

    monkeypatch.setattr(_time_mod, "sleep", _patched_sleep)

    events_file = _make_events(tmp_path, gap_s=0.0)
    follow_replay(events_file, console=console, speed=1.0, max_gap=0.0)

    # Any sleep should be timestamp-driven inter-event delay, not a post-final pause.
    # With gap_s=0.0 there should be zero sleeps at all.
    assert sleep_calls == [], f"Unexpected sleep calls: {sleep_calls}"


def test_replay_tmux_no_dec2026_markers(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """TMUX env: replay must not write DEC-2026 synchronized-output markers."""
    import io

    from rich.console import Console

    from verdict.orchestration.tui import follow_replay

    monkeypatch.setenv("TMUX", "/private/tmp/tmux-500/default,1234,0")
    monkeypatch.delenv("VERDICT_NO_ANIMATION", raising=False)
    monkeypatch.delenv("NO_COLOR", raising=False)

    stream = io.StringIO()
    console = Console(file=stream, force_terminal=True, width=80, color_system=None)

    events_file = _make_events(tmp_path, gap_s=0.0)
    follow_replay(events_file, console=console, speed=100.0, max_gap=0.0)

    output = stream.getvalue()
    assert "\x1b[?2026h" not in output, "DEC-2026 sync-ON must not appear for TMUX"
    assert "\x1b[?2026l" not in output, "DEC-2026 sync-OFF must not appear for TMUX"


def test_replay_render_exception_still_writes_sync_off(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A render error must not leave the synchronized-output block open."""
    import io

    from rich.console import Console

    from verdict.orchestration import tui as _tui

    monkeypatch.delenv("VERDICT_NO_ANIMATION", raising=False)
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("TMUX", raising=False)

    # Force a terminal that supports synchronized output (kitty-like)
    monkeypatch.setenv("TERM", "xterm-kitty")

    written: list[str] = []

    class _FakeFile(io.StringIO):
        def write(self, s: str) -> int:  # type: ignore[override]
            written.append(s)
            return super().write(s)

        def isatty(self) -> bool:
            return True

    fake_file = _FakeFile()
    console = Console(file=fake_file, force_terminal=True, width=80, color_system=None)

    # Patch render to raise on the second call (after live started)
    call_count = [0]
    original_render = _tui.render

    def _bad_render(*args: object, **kwargs: object) -> object:
        call_count[0] += 1
        if call_count[0] >= 2:
            raise RuntimeError("injected render failure")
        return original_render(*args, **kwargs)

    monkeypatch.setattr(_tui, "render", _bad_render)

    events_file = _make_events(tmp_path, gap_s=0.0)
    import contextlib

    with contextlib.suppress(RuntimeError):
        _tui.follow_replay(events_file, console=console, speed=100.0, max_gap=0.0)

    joined = "".join(written)
    # If a sync-ON was written, a sync-OFF must follow
    if "\x1b[?2026h" in joined:
        assert "\x1b[?2026l" in joined, "sync-OFF missing after render exception"
