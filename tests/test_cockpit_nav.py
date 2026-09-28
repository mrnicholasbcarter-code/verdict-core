"""Tests for the interactive cockpit navigation layer (BOD-276, lane 2)."""

from __future__ import annotations

import json
from io import StringIO
from pathlib import Path
from typing import Any

import pytest
from rich.console import Console

from verdict.orchestration.cockpit_nav import (
    CockpitState,
    ScriptedKeyReader,
    _RealKeyReader,
    dispatch_key,
    identity_for,
    render_detail_panel,
    render_footer,
    render_help,
    render_selected_row,
    run_cockpit,
    transition_chain_for,
)
from verdict.orchestration.contracts import RunEvent
from verdict.orchestration.tui import RunView, follow, render

LIVE = Path("docs/proof/live-controller-run/events.jsonl")


def _load_live_events() -> list[RunEvent]:
    return [
        RunEvent.from_dict(json.loads(line))
        for line in LIVE.read_text().splitlines()
        if line.strip()
    ]


def _event(seq: int, kind: str, node: str = "", **data: object) -> RunEvent:
    return RunEvent(
        seq=seq, at=f"2026-01-01T00:00:{seq:02d}+00:00", type=kind, node_id=node, data=data
    )


def _console(width: int = 100) -> Console:
    return Console(file=StringIO(), force_terminal=False, width=width, color_system=None)


def _render_dashboard(view: Any, width: int, plain: bool) -> Any:
    return render(view, width=width, plain=plain)


# ---------------------------------------------------------------------------
# ScriptedKeyReader safety
# ---------------------------------------------------------------------------


def test_scripted_reader_exhausted_raises_eoferror() -> None:
    reader = ScriptedKeyReader(["j"])
    assert reader.read() == "j"
    with pytest.raises(EOFError):
        reader.read()


def test_scripted_reader_close_marks_eof() -> None:
    reader = ScriptedKeyReader(["a", "b"])
    reader.close()
    with pytest.raises(EOFError):
        reader.read()


# ---------------------------------------------------------------------------
# CockpitState / dispatch_key
# ---------------------------------------------------------------------------


def test_sync_order_preserves_selection_by_identity() -> None:
    state = CockpitState()
    state.sync_order(["a", "b", "c"])
    state.move(1)  # -> b
    assert state.selected_id == "b"
    state.sync_order(["a", "b", "c", "d"])
    assert state.selected_id == "b"


def test_dispatch_move_and_wrap() -> None:
    view = RunView.from_events(
        [
            _event(1, "run_started", goal="g"),
            _event(2, "node_state", "n1", state="RUNNING"),
            _event(3, "node_state", "n2", state="RUNNING"),
        ]
    )
    state = CockpitState()
    state.sync_order(list(view.nodes.keys()))
    dispatch_key("j", state, view)
    assert state.selected_id == "n2"
    dispatch_key("j", state, view)
    assert state.selected_id == "n1"  # wrapped


def test_dispatch_enter_and_esc() -> None:
    state = CockpitState()
    state.sync_order(["n1"])
    dispatch_key("ENTER", state, RunView())
    assert state.detail_open
    dispatch_key("d", state, RunView())
    assert state.expanded
    # Esc collapses details first
    dispatch_key("ESC", state, RunView())
    assert not state.expanded and state.detail_open
    dispatch_key("ESC", state, RunView())
    assert not state.detail_open


def test_dispatch_help_toggle() -> None:
    state = CockpitState()
    dispatch_key("?", state, RunView())
    assert state.help_open
    dispatch_key("?", state, RunView())
    assert not state.help_open


def test_dispatch_quit_marks_state() -> None:
    state = CockpitState()
    dispatch_key("q", state, RunView())
    assert state.quit_requested


# ---------------------------------------------------------------------------
# Transition chain (collapsed) + expanded technical details
# ---------------------------------------------------------------------------


def test_transition_chain_collapsed_on_failure_cooldown_reassign() -> None:
    view = RunView.from_events(
        [
            _event(1, "run_started", goal="g"),
            _event(2, "selection", "n1", route_id="cc/haiku", provider="cc", attempt=1),
            _event(3, "node_state", "n1", state="RUNNING", route_id="cc/haiku"),
            _event(
                4,
                "failure",
                "n1",
                category="quota_exhausted",
                action="REROUTE",
                route_id="cc/haiku",
                evidence="429 quota",
            ),
            _event(
                5, "cooldown", "n1", key="cc", scope="provider", category="quota", until="later"
            ),
            _event(
                6,
                "reassign",
                "n1",
                from_route="cc/haiku",
                to_route="kr/haiku",
                reason="quota",
                attempt=2,
            ),
            _event(7, "node_state", "n1", state="RUNNING", route_id="kr/haiku", attempt=2),
        ]
    )
    chain = transition_chain_for(view, "n1")
    assert "failed(quota_exhausted)" in chain.summary
    assert "cooldown[provider:cc]" in chain.summary
    assert "replaced[cc/haiku -> kr/haiku]" in chain.summary
    assert "now[RUNNING]" in chain.summary
    # Technical is populated but hidden until d expands
    assert chain.technical
    assert any("evidence=429 quota" in t for t in chain.technical)


def test_transition_chain_empty_when_no_failures() -> None:
    view = RunView.from_events(
        [
            _event(1, "run_started", goal="g"),
            _event(2, "selection", "n1", route_id="cc/haiku"),
            _event(3, "node_state", "n1", state="VALIDATED", route_id="cc/haiku"),
        ]
    )
    assert transition_chain_for(view, "n1").is_empty


# ---------------------------------------------------------------------------
# Identity: selected vs observed reported_model
# ---------------------------------------------------------------------------


def test_identity_reports_mismatch_from_terminal_events() -> None:
    events = [
        _event(1, "run_started", goal="g"),
        _event(2, "selection", "n1", route_id="cc/haiku", provider="cc", attempt=1),
        _event(3, "node_state", "n1", state="RUNNING", route_id="cc/haiku"),
        _event(
            4,
            "terminal",
            "n1",
            route_id="cc/haiku",
            reported_model="cc/haiku-4.5",
            session_ref="s123",
            ok=True,
            duration_seconds=1.0,
        ),
    ]
    view = RunView.from_events(events)
    ident = identity_for(view, events, "n1")
    assert ident.selected_route == "cc/haiku"
    assert ident.observed_route == "cc/haiku-4.5"
    assert ident.session_ref == "s123"
    assert ident.mismatch


def test_identity_no_mismatch_when_equal() -> None:
    events = [
        _event(1, "selection", "n1", route_id="cc/haiku", attempt=1),
        _event(2, "terminal", "n1", route_id="cc/haiku", reported_model="cc/haiku", ok=True),
    ]
    view = RunView.from_events(events)
    ident = identity_for(view, events, "n1")
    assert not ident.mismatch


# ---------------------------------------------------------------------------
# Rendering: detail panel collapsed vs expanded; NO_COLOR has no ANSI
# ---------------------------------------------------------------------------


def _rich_to_text(console: Console, renderable: Any) -> str:
    console.print(renderable)
    text = console.file.getvalue()
    console.file.truncate(0)
    console.file.seek(0)
    return text


def test_detail_panel_collapsed_hides_technical() -> None:
    events = [
        _event(1, "run_started", goal="g"),
        _event(2, "selection", "n1", route_id="cc/haiku", attempt=1),
        _event(3, "node_state", "n1", state="RUNNING", route_id="cc/haiku"),
        _event(
            4,
            "failure",
            "n1",
            category="quota_exhausted",
            action="REROUTE",
            route_id="cc/haiku",
            evidence="429 quota-hard",
        ),
    ]
    view = RunView.from_events(events)
    state = CockpitState()
    state.sync_order(list(view.nodes.keys()))
    console = _console(100)
    rendered = render_detail_panel(view, events, state, plain=True, width=100)
    text = _rich_to_text(console, rendered)
    assert "failed(quota_exhausted)" in text
    assert "429 quota-hard" not in text  # collapsed
    assert "press d for technical details" in text


def test_detail_panel_expanded_shows_technical() -> None:
    events = [
        _event(1, "selection", "n1", route_id="cc/haiku", attempt=1),
        _event(
            2,
            "failure",
            "n1",
            category="quota_exhausted",
            action="REROUTE",
            route_id="cc/haiku",
            evidence="429 quota-hard",
        ),
    ]
    view = RunView.from_events(events)
    state = CockpitState()
    state.sync_order(list(view.nodes.keys()))
    state.expanded = True
    console = _console(100)
    rendered = render_detail_panel(view, events, state, plain=True, width=100)
    text = _rich_to_text(console, rendered)
    assert "evidence=429 quota-hard" in text


def test_detail_panel_flags_mismatch() -> None:
    events = [
        _event(1, "selection", "n1", route_id="cc/haiku"),
        _event(2, "terminal", "n1", route_id="cc/haiku", reported_model="cc/haiku-4.5", ok=True),
    ]
    view = RunView.from_events(events)
    state = CockpitState()
    state.sync_order(["n1"])
    console = _console(100)
    text = _rich_to_text(console, render_detail_panel(view, events, state, plain=True))
    assert "MISMATCH" in text


def test_no_color_renders_without_ansi_escapes() -> None:
    """Plain=True rendering must not emit any \x1b escape sequences."""
    events = [
        _event(1, "run_started", goal="g"),
        _event(2, "selection", "n1", route_id="cc/haiku", attempt=1),
        _event(
            3, "failure", "n1", category="quota_exhausted", action="REROUTE", route_id="cc/haiku"
        ),
    ]
    view = RunView.from_events(events)
    state = CockpitState()
    state.sync_order(["n1"])
    console = _console(100)
    for renderable in (
        render_selected_row(view, state, plain=True),
        render_detail_panel(view, events, state, plain=True, width=100),
        render_help(plain=True),
        render_footer(plain=True),
    ):
        text = _rich_to_text(console, renderable)
        assert "\x1b" not in text


def test_narrow_terminal_does_not_crash() -> None:
    events = [
        _event(1, "run_started", goal="g"),
        _event(2, "selection", "n1", route_id="cc/haiku-4.5-super-long-name", attempt=1),
        _event(
            3, "failure", "n1", category="quota_exhausted", route_id="cc/haiku-4.5-super-long-name"
        ),
    ]
    view = RunView.from_events(events)
    state = CockpitState()
    state.sync_order(["n1"])
    console = _console(60)
    for renderable in (
        render_selected_row(view, state, plain=True),
        render_detail_panel(view, events, state, plain=True, width=60),
    ):
        _rich_to_text(console, renderable)  # must not raise


# ---------------------------------------------------------------------------
# run_cockpit: scripted keys drive selection / detail / exit
# ---------------------------------------------------------------------------


def test_run_cockpit_selection_and_quit(tmp_path: Path) -> None:
    events_path = tmp_path / "events.jsonl"
    rows = [
        {
            "seq": 1,
            "at": "2026-01-01T00:00:01Z",
            "type": "run_started",
            "node_id": "",
            "data": {"goal": "g"},
        },
        {
            "seq": 2,
            "at": "2026-01-01T00:00:02Z",
            "type": "node_state",
            "node_id": "n1",
            "data": {"state": "RUNNING", "route_id": "cc/haiku"},
        },
        {
            "seq": 3,
            "at": "2026-01-01T00:00:03Z",
            "type": "node_state",
            "node_id": "n2",
            "data": {"state": "RUNNING", "route_id": "kr/haiku"},
        },
    ]
    events_path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    view = RunView()
    reader = ScriptedKeyReader(["j", "ENTER", "d", "q"])
    console = _console(100)

    def _source() -> list:
        from verdict.orchestration.tui import read_events

        return list(read_events(events_path))

    state = run_cockpit(
        view=view,
        events_source=_source,
        render_dashboard=_render_dashboard,
        console=console,
        key_reader=reader,
        plain=True,
        poll_seconds=0.0,
        max_iterations=50,
        stop_when_final=False,
    )
    assert state.selected_id == "n2"
    assert state.detail_open
    assert state.expanded
    assert state.quit_requested


def test_run_cockpit_exhausted_reader_exits_cleanly(tmp_path: Path) -> None:
    events_path = tmp_path / "events.jsonl"
    events_path.write_text(
        '{"seq":1,"at":"2026-01-01T00:00:01Z","type":"run_started","node_id":"","data":{"goal":"g"}}\n'
    )
    view = RunView()
    reader = ScriptedKeyReader([])  # empty -> EOFError on first read
    console = _console(100)

    def _source() -> list:
        from verdict.orchestration.tui import read_events

        return list(read_events(events_path))

    state = run_cockpit(
        view=view,
        events_source=_source,
        render_dashboard=_render_dashboard,
        console=console,
        key_reader=reader,
        plain=True,
        poll_seconds=0.0,
        max_iterations=5,
        stop_when_final=False,
    )
    # Loop should exit before quit_requested is set.
    assert not state.quit_requested
    assert view.goal == "g"


def test_run_cockpit_terminal_restore_on_exception(tmp_path: Path) -> None:
    """When render raises, the key reader is still closed (terminal mode restored)."""
    events_path = tmp_path / "events.jsonl"
    events_path.write_text(
        '{"seq":1,"at":"2026-01-01T00:00:01Z","type":"run_started","node_id":"","data":{"goal":"g"}}\n'
    )
    view = RunView()
    reader = ScriptedKeyReader(["q"])
    console = _console(100)
    closed: dict[str, bool] = {"v": False}

    original_close = reader.close

    def _tracking_close() -> None:
        closed["v"] = True
        original_close()

    reader.close = _tracking_close  # type: ignore[assignment]

    def _boom(view: Any, width: int, plain: bool) -> Any:
        raise RuntimeError("render exploded")

    def _source() -> list:
        from verdict.orchestration.tui import read_events

        return list(read_events(events_path))

    with pytest.raises(RuntimeError):
        run_cockpit(
            view=view,
            events_source=_source,
            render_dashboard=_boom,
            console=console,
            key_reader=reader,
            plain=False,
            poll_seconds=0.0,
            max_iterations=5,
            stop_when_final=False,
        )
    assert closed["v"] is True


# ---------------------------------------------------------------------------
# Burst coalescing: many events applied without dropping state
# ---------------------------------------------------------------------------


def test_burst_events_apply_without_dropping_state(tmp_path: Path) -> None:
    events_path = tmp_path / "events.jsonl"
    burst = [
        {
            "seq": i,
            "at": f"2026-01-01T00:00:{i:02d}Z",
            "type": "node_state",
            "node_id": f"n{i}",
            "data": {"state": "RUNNING"},
        }
        for i in range(1, 21)
    ]
    events_path.write_text("\n".join(json.dumps(r) for r in burst) + "\n")
    view = RunView()
    reader = ScriptedKeyReader(["q"])
    console = _console(100)

    def _source() -> list:
        from verdict.orchestration.tui import read_events

        return list(read_events(events_path))

    run_cockpit(
        view=view,
        events_source=_source,
        render_dashboard=_render_dashboard,
        console=console,
        key_reader=reader,
        plain=True,
        poll_seconds=0.0,
        max_iterations=5,
        stop_when_final=False,
    )
    # All 20 nodes reached the view; no state was silently dropped.
    assert set(view.nodes.keys()) == {f"n{i}" for i in range(1, 21)}


# ---------------------------------------------------------------------------
# Recorded run: docs/proof/live-controller-run/events.jsonl replays cleanly
# ---------------------------------------------------------------------------


def test_live_controller_events_replay_through_cockpit(tmp_path: Path) -> None:
    events = _load_live_events()
    events_path = tmp_path / "events.jsonl"
    events_path.write_text("\n".join(json.dumps(_event_to_row(e)) for e in events) + "\n")
    view = RunView()
    reader = ScriptedKeyReader(["ENTER", "d", "?", "ESC", "ESC", "ESC", "q"])
    console = _console(100)

    def _source() -> list:
        from verdict.orchestration.tui import read_events

        return list(read_events(events_path))

    state = run_cockpit(
        view=view,
        events_source=_source,
        render_dashboard=_render_dashboard,
        console=console,
        key_reader=reader,
        plain=True,
        poll_seconds=0.0,
        max_iterations=50,
        stop_when_final=False,
    )
    assert state.quit_requested
    # Live-run recording finishes COMPLETE.
    assert view.outcome == "COMPLETE"


def _event_to_row(e: RunEvent) -> dict[str, Any]:
    return {"seq": e.seq, "at": e.at, "type": e.type, "node_id": e.node_id, "data": e.data}


# ---------------------------------------------------------------------------
# follow() dispatches to the cockpit when interactive=True
# ---------------------------------------------------------------------------


def test_follow_interactive_uses_cockpit(tmp_path: Path) -> None:
    events_path = tmp_path / "events.jsonl"
    events_path.write_text(
        '{"seq":1,"at":"2026-01-01T00:00:01Z","type":"run_started","node_id":"","data":{"goal":"hi"}}\n'
    )
    reader = ScriptedKeyReader(["q"])
    console = _console(100)
    view = follow(
        events_path,
        console=console,
        interactive=True,
        key_reader=reader,
        max_iterations=10,
        poll_seconds=0.0,
        stop_when_final=False,
    )
    assert view.goal == "hi"


def test_follow_non_interactive_is_unchanged(tmp_path: Path) -> None:
    """Existing default behaviour (no TTY) is preserved."""
    events_path = tmp_path / "events.jsonl"
    events_path.write_text(
        '{"seq":1,"at":"2026-01-01T00:00:01Z","type":"run_started","node_id":"","data":{"goal":"legacy"}}\n'
    )
    console = _console(100)
    view = follow(events_path, console=console, stop_when_final=False, max_polls=1)
    assert view.goal == "legacy"


# ---------------------------------------------------------------------------
# _RealKeyReader construction on a non-TTY should not crash and read yields None
# ---------------------------------------------------------------------------


def test_real_key_reader_non_tty_stream_yields_none() -> None:
    class _FakeStream:
        def fileno(self) -> int:
            raise OSError("no fd")

    reader = _RealKeyReader(_FakeStream())
    assert reader.read(timeout=0.0) is None
    reader.close()


def test_supervise_background_follow_is_never_interactive(monkeypatch) -> None:
    """The supervise path runs follow() in a daemon thread; it must never read keys
    or change terminal mode, even when stdout is a TTY (review of PR #718)."""
    import inspect

    from verdict.orchestration import cli as orch_cli

    src = inspect.getsource(orch_cli)
    i = src.find("target=lambda: follow(")
    assert i != -1, "background follow call not found"
    call = src[i : src.find(")", src.find("interactive=", i)) + 1]
    assert "interactive=False" in call
