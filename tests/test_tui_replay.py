"""Tests for TUI replay mode."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from verdict.orchestration.tui import follow_replay


@pytest.mark.parametrize("model_field", ["model", "reported_model"])
def test_follow_replay_shows_replay_header_real_models(tmp_path: Path, model_field: str) -> None:
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
            "at": "2026-01-01T00:00:00.750000Z",
            "seq": 2,
            "type": "terminal",
            "node_id": "node-a",
            "data": {model_field: "kr/claude-haiku-4.5", "executor_kind": "live", "ok": True},
        },
        {
            "at": "2026-01-01T00:00:01.000000Z",
            "seq": 3,
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
    assert "inferred" not in view.goal


def test_follow_replay_shows_fixture_label(tmp_path: Path) -> None:
    """Legacy fixture terminals identify a fixture, not merely selected routes."""
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
            "at": "2026-01-01T00:00:00.750000Z",
            "seq": 2,
            "type": "terminal",
            "node_id": "node-a",
            "data": {"reported_model": "demo-sub/atlas-coder", "ok": True},
        },
        {
            "at": "2026-01-01T00:00:01.000000Z",
            "seq": 3,
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


@pytest.mark.parametrize("route", ["", "alpha/real-provider", "demo-sub/atlas-coder"])
def test_follow_replay_unknown_routes_no_real_models_claim(tmp_path: Path, route: str) -> None:
    """A selected route does not prove a live or scripted executor ran."""
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
            "at": "2026-01-01T00:00:00.500000Z",
            "seq": 1,
            "type": "selection",
            "node_id": "node-a",
            "data": {"route_id": route, "attempt": 1},
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

    # Without terminal evidence, show only the generic REPLAY label.
    assert "REPLAY" in view.goal
    assert "real models" not in view.goal
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
    assert "fixture run" in view.goal  # demo-run reports reserved fixture terminal models


def test_replay_kind_needs_evidence_for_real_models() -> None:
    from types import SimpleNamespace

    from verdict.orchestration.tui import _replay_kind

    def ev(t: str, **data: object) -> SimpleNamespace:
        return SimpleNamespace(type=t, data=data)

    assert _replay_kind([]) == "unknown"
    assert _replay_kind([ev("node_state")]) == "unknown"
    for route in ("alpha/real-provider", "kr/claude-haiku-4.5", "demo-sub/atlas"):
        for kind in ("selection", "dispatch"):
            assert _replay_kind([ev(kind, route_id=route)]) == "unknown"
    assert _replay_kind([ev("terminal", route_id="alpha/x")]) == "unknown"
    assert _replay_kind([ev("terminal", reported_model="alpha/x")]) == "real"
    assert _replay_kind([ev("terminal", reported_model="demo-sub/atlas")]) == "fixture"
    for marker in (
        {"mode": "offline-scenario"},
        {"run_id": "offline-flagship-failover"},
        {"executor": "scripted"},
        {"executor_kind": "scripted"},
    ):
        assert (
            _replay_kind(
                [
                    ev("run_started", **marker),
                    ev("terminal", reported_model="alpha/x", executor_kind="live"),
                ]
            )
            == "fixture"
        )
    # Mode is passed explicitly; inspect only package Python sources, from any cwd.
    package = Path(__file__).resolve().parents[1] / "verdict"
    offenders = [
        str(path.relative_to(package))
        for path in sorted(package.rglob("*.py"))
        if "VERDICT_RUN_MODE" in path.read_text(encoding="utf-8")
    ]
    assert not offenders, f"VERDICT_RUN_MODE found in source; use explicit mode=: {offenders}"


@pytest.mark.parametrize(
    ("terminal", "expected"),
    [
        ({"executor_kind": "live", "reported_model": "alpha/x"}, "real"),
        ({"executor_kind": "live", "model": "alpha/x"}, "real"),
        ({"executor_kind": "live", "reported_model": "fixture/model"}, "real"),
        (
            {
                "executor_kind": "live",
                "reported_model": "alpha/x",
                "session_ref": "fault-injected:not-provenance",
            },
            "real",
        ),
        ({"executor_kind": "live"}, "unknown"),
        ({"executor_kind": "scripted", "reported_model": "alpha/x"}, "fixture"),
        ({"executor_kind": "scripted"}, "fixture"),
        ({"executor_kind": "fault-injected", "reported_model": "alpha/x"}, "unknown"),
        ({"executor_kind": "", "reported_model": "alpha/x"}, "unknown"),
        ({"executor_kind": None, "reported_model": "alpha/x"}, "unknown"),
        ({"executor_kind": "unrecognized", "reported_model": "alpha/x"}, "unknown"),
        ({"reported_model": "alpha/x", "fault_injected": True}, "unknown"),
        ({"reported_model": "alpha/x", "session_ref": "fault-injected:quota"}, "unknown"),
        ({"reported_model": "(mechanical merge)"}, "unknown"),
        ({"reported_model": ""}, "unknown"),
        ({"reported_model": "  "}, "unknown"),
    ],
)
def test_terminal_executor_provenance(terminal: dict[str, object], expected: str) -> None:
    from types import SimpleNamespace

    from verdict.orchestration.tui import _replay_kind

    assert _replay_kind([SimpleNamespace(type="terminal", data=terminal)]) == expected


@pytest.mark.parametrize("marker_on", ["run_started", "terminal"])
def test_explicit_executor_evidence_is_not_legacy_inference(marker_on: str) -> None:
    from types import SimpleNamespace

    from verdict.orchestration.tui import _replay_evidence

    start = SimpleNamespace(type="run_started", data={})
    terminal = SimpleNamespace(type="terminal", data={"reported_model": "alpha/x"})
    (start if marker_on == "run_started" else terminal).data["executor_kind"] = "live"
    assert _replay_evidence([start, terminal]) == ("real", False)


def test_scripted_terminal_overrides_legacy_unmarked_evidence() -> None:
    """Scripted wins over legacy (unmarked) evidence; result is fixture, not mixed."""
    from types import SimpleNamespace

    from verdict.orchestration.tui import _replay_evidence

    # No explicit executor_kind on the first terminal → legacy inference.
    # A scripted terminal in the same run overrides it → fixture.
    events = [
        SimpleNamespace(type="terminal", data={"reported_model": "alpha/x"}),
        SimpleNamespace(type="terminal", data={"executor_kind": "scripted"}),
    ]
    assert _replay_evidence(events) == ("fixture", False)
    assert _replay_evidence(list(reversed(events))) == ("fixture", False)


def test_live_and_scripted_terminals_produce_mixed_label() -> None:
    """A mix of live (with reported model) and scripted terminals → mixed, never fixture."""
    from types import SimpleNamespace

    from verdict.orchestration.tui import _replay_evidence

    events = [
        SimpleNamespace(
            type="terminal", data={"executor_kind": "live", "reported_model": "alpha/x"}
        ),
        SimpleNamespace(type="terminal", data={"executor_kind": "scripted"}),
    ]
    # Both orderings must give the same mixed result.
    assert _replay_evidence(events) == ("mixed: live and scripted workers", False)
    assert _replay_evidence(list(reversed(events))) == ("mixed: live and scripted workers", False)


@pytest.mark.parametrize("legacy_first", [False, True])
@pytest.mark.parametrize("model_field", ["model", "reported_model"])
def test_live_terminal_overrides_legacy_fixture_models(
    legacy_first: bool, model_field: str
) -> None:
    from types import SimpleNamespace

    from verdict.orchestration.tui import _replay_evidence

    events = [
        SimpleNamespace(type="terminal", data={"executor_kind": "live", model_field: "alpha/x"}),
        SimpleNamespace(type="terminal", data={model_field: "fixture/model"}),
    ]
    if legacy_first:
        events.reverse()
    assert _replay_evidence(events) == ("real", False)


@pytest.mark.parametrize("legacy_first", [False, True])
@pytest.mark.parametrize(
    ("executor_kind", "legacy_model", "inferred"),
    [
        ("live", "fixture/model", False),
        ("live", "alpha/legacy", False),
        ("unrecognized", "alpha/legacy", True),
        ("", "alpha/legacy", True),
        (None, "alpha/legacy", True),
        ("fault-injected", "alpha/legacy", True),
    ],
)
def test_mixed_terminal_provenance_does_not_predate_executor_markers(
    tmp_path: Path, legacy_first: bool, executor_kind: str | None, legacy_model: str, inferred: bool
) -> None:
    from io import StringIO

    from rich.console import Console

    from verdict.orchestration.tui import _replay_evidence, read_events

    terminals = [
        {"executor_kind": executor_kind, "reported_model": "alpha/marked"},
        {"reported_model": legacy_model},
    ]
    if legacy_first:
        terminals.reverse()
    event_data = [
        ("run_started", {"goal": "mixed provenance", "run_id": "mixed-run"}),
        *(("terminal", terminal) for terminal in terminals),
        ("run_finished", {"outcome": "COMPLETE"}),
    ]
    events_file = tmp_path / "events.jsonl"
    events_file.write_text(
        "\n".join(
            json.dumps(
                {
                    "at": "2026-01-01T00:00:00.000000Z",
                    "seq": seq,
                    "type": event_type,
                    "node_id": "node-a" if event_type == "terminal" else "",
                    "data": data,
                }
            )
            for seq, (event_type, data) in enumerate(event_data)
        )
    )

    assert _replay_evidence(read_events(events_file)) == ("real", inferred)
    view = follow_replay(
        events_file, console=Console(file=StringIO(), width=110), speed=1000, max_gap=0
    )
    assert "real models" in view.goal
    assert "mixed executor provenance: marked and unmarked terminals" in view.goal
    assert "run predates executor markers" not in view.goal
    assert ("inferred from reported terminal models" in view.goal) is inferred
    assert "no model calls" not in view.goal


@pytest.mark.parametrize("proof", ["live-controller-run", "dogfood-bod-225-live-2026-09-29"])
def test_legacy_live_proofs_disclose_inferred_replay_header(proof: str) -> None:
    from io import StringIO

    from rich.console import Console

    from verdict.orchestration.tui import _replay_evidence, read_events

    events_file = Path(__file__).resolve().parents[1] / "docs" / "proof" / proof / "events.jsonl"
    assert _replay_evidence(read_events(events_file)) == ("real", True)
    view = follow_replay(
        events_file, console=Console(file=StringIO(), width=110), speed=1000, max_gap=0
    )
    # Repeated run_started events in resumed proofs must retain the replay marker.
    assert "REPLAY of recorded run" in view.goal
    assert "real models: inferred from reported terminal models" in view.goal
    assert "run predates executor markers" in view.goal


def test_follow_replay_mixed_live_scripted_shows_mixed_label(tmp_path: Path) -> None:
    """follow_replay labels a live+scripted run as 'mixed: live and scripted workers'."""
    import json
    from io import StringIO

    from rich.console import Console

    from verdict.orchestration.tui import _replay_evidence, follow_replay, read_events

    def _write(path: Path, live_first: bool, run_id: str) -> None:
        terminals = [
            {"executor_kind": "live", "reported_model": "alpha/real", "ok": True},
            {"executor_kind": "scripted", "model": "demo-scripted", "ok": True},
        ]
        if not live_first:
            terminals = list(reversed(terminals))
        rows = [
            ("run_started", {"goal": "mixed live/scripted run", "run_id": run_id}),
            *[("terminal", t) for t in terminals],
            ("run_finished", {"outcome": "COMPLETE"}),
        ]
        path.write_text(
            "\n".join(
                json.dumps(
                    {
                        "at": "2026-01-01T00:00:00.000000Z",
                        "seq": i,
                        "type": et,
                        "node_id": "node-a" if et == "terminal" else "",
                        "data": d,
                    }
                )
                for i, (et, d) in enumerate(rows)
            )
        )

    for live_first, run_id in [(True, "mixed-ls-fwd"), (False, "mixed-ls-rev")]:
        ef = tmp_path / f"ev-{run_id}.jsonl"
        _write(ef, live_first, run_id)
        assert _replay_evidence(read_events(ef)) == ("mixed: live and scripted workers", False), (
            f"live_first={live_first}"
        )

    ef0 = tmp_path / "ev-mixed-ls-fwd.jsonl"
    view = follow_replay(ef0, console=Console(file=StringIO(), width=110), speed=1000, max_gap=0)
    assert "mixed: live and scripted workers" in view.goal
    assert "no model calls" not in view.goal
    assert "REPLAY of recorded run" in view.goal


def test_live_terminal_empty_model_is_not_real_evidence() -> None:
    """A live terminal with empty model (pre-call failure) must not count as 'real'."""
    from types import SimpleNamespace

    from verdict.orchestration.tui import _replay_evidence

    events = [
        SimpleNamespace(
            type="terminal", data={"executor_kind": "live", "reported_model": "", "ok": False}
        )
    ]
    kind, _ = _replay_evidence(events)
    assert kind not in {"real", "mixed: live and scripted workers"}


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
    # CI=true (GitHub Actions) disables animation in presentation_mode, which
    # would skip the synchronized-output path this test must exercise.
    for var in ("CI", "GITHUB_ACTIONS", "REDUCED_MOTION", "SSH_CONNECTION"):
        monkeypatch.delenv(var, raising=False)

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
    # Sync-ON must have been emitted (xterm-kitty supports DEC 2026)
    assert "\x1b[?2026h" in joined, (
        "sync-ON was not written — check TERM=xterm-kitty is not overridden. "
        "This assertion proves the sync path was exercised."
    )
    # Sync-OFF must follow sync-ON unconditionally, even when render raises
    assert "\x1b[?2026l" in joined, "sync-OFF missing after render exception"
