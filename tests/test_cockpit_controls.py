"""Cockpit action confirmation, recorded deep links and bounded rendering."""

from __future__ import annotations

import argparse
import re
import shutil
import time
from io import StringIO
from pathlib import Path
from typing import Any

import pytest
from rich.console import Console

from verdict.actions import ActionResult
from verdict.design import PresentationMode, tokens_for
from verdict.orchestration import cockpit_controls as cockpit
from verdict.orchestration import cockpit_nav as nav
from verdict.orchestration.cli import _watch, add_parsers
from verdict.orchestration.contracts import RunEvent
from verdict.orchestration.tui import RunView, follow, read_events

FIXTURE = Path(__file__).parent / "fixtures" / "cockpit_controls"
GOLDEN = FIXTURE / "golden"


def _console(width: int = 60, *, color: bool = False) -> Console:
    return Console(
        file=StringIO(),
        width=width,
        height=200,
        force_terminal=color,
        color_system="truecolor" if color else None,
        no_color=not color,
        record=True,
        markup=False,
        highlight=False,
    )


def _state() -> tuple[RunView, cockpit.ControlCockpitState]:
    events = read_events(FIXTURE / "events.jsonl")
    view = RunView.from_events(events)
    state = cockpit.initial_state(view, events, Path("recorded-run"), node_id="alpha")
    return view, state


def _plain_frame(panel_name: cockpit.PanelName, width: int) -> str:
    view, state = _state()
    cockpit.select_panel(state, view, panel_name)
    mode = PresentationMode(
        color=False, unicode=False, animate=False, width=width, color_system=None
    )
    console = _console(width)
    console.print(cockpit.render_cockpit(view, state, mode, dashboard=False))
    return console.export_text()


@pytest.mark.parametrize("panel_name", ["routing", "context", "receipt"])
@pytest.mark.parametrize("width", [60, 100, 200])
def test_plain_goldens(
    panel_name: cockpit.PanelName, width: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "verdict.orchestration.routing_view._now_iso", lambda: "2026-09-29T00:00:09Z"
    )
    output = _plain_frame(panel_name, width)
    assert output == (GOLDEN / f"{panel_name}-{width}.txt").read_text()
    assert "\x1b" not in output
    assert all(len(line) <= width for line in output.splitlines())
    assert "recovery budget" in output and "exhausted" in output


def test_context_key_opens_selected_provenance_and_refreshes() -> None:
    view, state = _state()
    assert cockpit.dispatch_key("c", state, view)
    assert state.context_open
    assert state.context_view is not None
    assert [n.node_id for n in state.context_view.nodes] == ["alpha"]
    assert state.context_view.nodes[0].sources is not None
    assert state.context_view.nodes[0].sources[0].path == "src/alpha.py"
    state.move(1)
    mode = PresentationMode(False, False, False, 60, None)
    console = _console()
    console.print(cockpit.render_cockpit(view, state, mode, dashboard=False))
    output = console.export_text()
    assert "node beta" in output
    assert "node alpha" not in output
    assert cockpit.dispatch_key("ESC", state, view)
    assert not state.context_open


def test_routing_key_filters_to_selected_worker() -> None:
    view, state = _state()
    assert cockpit.dispatch_key("r", state, view)
    routing = cockpit.routing_for_selected(state, view)
    assert len(routing.evaluations) == 1
    assert routing.evaluations[0].node_id == "alpha"
    assert routing.evaluations[0].selected_route == "kr/claude-opus"
    state.move(1)
    routing = cockpit.routing_for_selected(state, view)
    assert routing.evaluations[0].node_id == "beta"
    assert routing.evaluations[0].selected_route == "cc/claude-sonnet"


def test_no_routing_evidence_does_not_fall_back_to_another_worker() -> None:
    view, state = _state()
    state.sync_order(["missing"])
    assert cockpit.routing_for_selected(state, view).evaluations == []


def test_receipt_key_uses_verified_existing_summary(tmp_path: Path) -> None:
    run_dir = tmp_path / "demo-run"
    shutil.copytree(Path("docs/proof/demo-run"), run_dir)
    events = read_events(run_dir / "events.jsonl")
    view = RunView.from_events(events)
    state = cockpit.initial_state(view, events, run_dir, node_id="parser")
    before = (run_dir / "receipt.json").read_bytes()
    assert cockpit.dispatch_key("p", state, view)
    assert state.receipt_open
    assert state.receipt_result is not None
    assert state.receipt_result.data["problems"] == []
    console = _console()
    console.print(cockpit.render_receipt(state, PresentationMode(False, False, False, 60, None)))
    output = console.export_text()
    assert "COMPLETE" in output
    assert "VALIDATED" in output
    assert "worker: parser" in output
    assert (run_dir / "receipt.json").read_bytes() == before


@pytest.mark.parametrize("key", list(cockpit.CONTROL_KEYS))
def test_controls_require_confirmation_and_escape_is_no_mutation(key: str) -> None:
    view, state = _state()
    assert cockpit.dispatch_key(key, state, view)
    assert state.pending_action == cockpit.CONTROL_KEYS[key]
    assert state.control_result is None
    assert cockpit.dispatch_key("ESC", state, view)
    assert state.pending_action == ""
    assert state.control_result is None


def test_queued_is_not_rendered_as_completed() -> None:
    _view, state = _state()
    state.control_result = ActionResult(data={"request": {"id": "abc"}})
    console = _console()
    console.print(cockpit.render_controls(state, PresentationMode(False, False, False, 60, None)))
    output = console.export_text()
    assert "queued" in output
    assert "pending" in output
    assert "completed" not in output


@pytest.mark.parametrize("panel_name", ["routing", "context", "receipt"])
def test_watch_deep_link_plain_60_columns(
    panel_name: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("VERDICT_PLAIN", "1")
    monkeypatch.setenv("COLUMNS", "60")
    args = argparse.Namespace(
        run=str(FIXTURE),
        runs_dir=".verdict/runs",
        node="alpha",
        panel=panel_name,
        once=True,
        replay=False,
        speed=1.0,
    )
    assert _watch(args) == 0
    output = capsys.readouterr().out
    assert "selected: alpha" in output
    assert "\x1b" not in output
    assert all(len(line) <= 60 for line in output.splitlines())
    assert {
        "routing": "routing explorer",
        "context": "node alpha",
        "receipt": "Receipt not available",
    }[panel_name] in output


def test_watch_parser_accepts_node_and_panel() -> None:
    parser = argparse.ArgumentParser()
    add_parsers(parser.add_subparsers(dest="command"))
    args = parser.parse_args(["watch", "run-123", "--node", "alpha", "--panel", "context"])
    assert args.node == "alpha"
    assert args.panel == "context"


def test_unknown_node_refused_by_deep_link(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("VERDICT_PLAIN", "1")
    args = argparse.Namespace(
        run=str(FIXTURE),
        runs_dir=".verdict/runs",
        node="missing",
        panel="context",
        once=True,
        replay=False,
        speed=1.0,
    )
    assert _watch(args) == 2
    assert "unknown node: missing" in capsys.readouterr().err


@pytest.mark.parametrize(
    "env",
    [{"VERDICT_PLAIN": "1"}, {"NO_COLOR": "1"}, {"TERM": "dumb"}, {"VERDICT_NO_ANIMATION": "1"}],
)
def test_disabled_motion_uses_static_frames(
    env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    for key in ("VERDICT_PLAIN", "NO_COLOR", "TERM", "CI", "VERDICT_NO_ANIMATION"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(
        cockpit, "Live", lambda *args, **kwargs: pytest.fail("static mode must not start Live")
    )
    target = _console(color=True)
    view = cockpit.run_cockpit(
        FIXTURE,
        console=target,
        key_reader=nav.ScriptedKeyReader(["q"]),
        stop_when_final=False,
        max_iterations=3,
        poll_seconds=0,
    )
    assert view.event_count == 9
    output = target.export_text(styles=True)
    if "VERDICT_NO_ANIMATION" not in env:
        assert "\x1b" not in output


def test_non_tty_is_static_and_plain() -> None:
    target = _console()
    view = cockpit.run_cockpit(
        FIXTURE,
        console=target,
        key_reader=nav.ScriptedKeyReader([]),
        stop_when_final=False,
        max_iterations=3,
        poll_seconds=0,
    )
    assert view.event_count == 9
    assert "\x1b" not in target.export_text(styles=True)


def test_color_uses_shared_tokens_and_no_hardcoded_palette() -> None:
    view, state = _state()
    console = _console(100, color=True)
    mode = PresentationMode(True, True, False, 100, "truecolor")
    controls = cockpit.render_controls(state, mode)
    assert controls.border_style == tokens_for()["BORDER"]
    console.print(cockpit.render_cockpit(view, state, mode, dashboard=False))
    output = console.export_text(styles=True)
    assert "\x1b" in output
    source = Path(cockpit.__file__).read_text()
    assert not re.search(r"#[0-9a-fA-F]{6}", source)
    assert 'style="purple"' not in source


def test_burst_1000_events_applies_once_and_coalesces(monkeypatch: pytest.MonkeyPatch) -> None:
    events = [
        RunEvent(
            seq=i,
            at="2026-09-29T00:00:00Z",
            type="node_state",
            node_id="alpha",
            data={"state": "RUNNING", "attempt": i},
        )
        for i in range(1, 1001)
    ]
    calls = 0
    renders = 0
    real_render = cockpit.render_cockpit

    def source() -> list[RunEvent]:
        nonlocal calls
        calls += 1
        return [] if calls == 1 else events

    def render_spy(*args: Any, **kwargs: Any) -> Any:
        nonlocal renders
        renders += 1
        return real_render(*args, **kwargs)

    monkeypatch.setattr(cockpit, "render_cockpit", render_spy)
    started = time.monotonic()
    view = cockpit.run_cockpit(
        FIXTURE,
        console=_console(),
        key_reader=nav.ScriptedKeyReader(["q"]),
        events_source=source,
        max_iterations=3,
        stop_when_final=False,
        poll_seconds=0,
    )
    assert view.event_count == 1000
    assert view.nodes["alpha"].attempt == 1000
    assert renders == 1
    assert time.monotonic() - started < 2


def test_follow_deep_link_receives_keys_with_hard_cap(tmp_path: Path) -> None:
    events_path = tmp_path / "events.jsonl"
    events_path.write_bytes((FIXTURE / "events.jsonl").read_bytes())
    target = _console()
    view = follow(
        events_path,
        console=target,
        interactive=True,
        node_id="alpha",
        panel_name="context",
        key_reader=nav.ScriptedKeyReader(["r", "q"]),
        stop_when_final=False,
        max_iterations=4,
        poll_seconds=0,
    )
    assert view.event_count == 9
    output = target.export_text()
    assert "node alpha" in output
    assert "routing explorer" in output


def test_single_x_is_confirmation_only(monkeypatch: pytest.MonkeyPatch) -> None:
    view, state = _state()
    calls: list[str] = []
    monkeypatch.setattr(nav, "submit_control", lambda s, a: calls.append(a))
    assert cockpit.dispatch_key("x", state, view)
    assert state.pending_action == "run.cancel"
    assert not calls
    assert not cockpit.dispatch_key("ENTER", state, view)
    assert not calls
    assert cockpit.dispatch_key("x", state, view)
    assert calls == ["run.cancel"]


def test_empty_route_failures_not_leaked_across_nodes() -> None:
    """Fix 2: route and f.route_id == route guard prevents cross-node failure leak.

    Two nodes, each with an empty route_id failure.
    When the selected role row also has no route, neither failure should appear
    because `route` is empty and the route-match branch requires `route` to be truthy.
    """
    from pathlib import Path
    from verdict.orchestration import cockpit_controls as cockpit
    from verdict.orchestration import cockpit_nav as nav
    from verdict.orchestration.tui import Failure, RunView

    view = RunView()
    view.failures = [
        Failure(node_id="N1", category="hard", action="generate", route_id=""),
        Failure(node_id="N2", category="hard", action="generate", route_id=""),
    ]

    state = cockpit.ControlCockpitState(run_dir=Path("."), events=[])
    state.sync_order([nav.ROLE_CONTROLLER])
    # Select the controller role row; its route is "" (no plan_started event)
    state.selected_index = 0

    _, failures, _ = cockpit._health_for_selected(state, view)
    # Neither node's failure must appear: selected is a role row so node_id branch is
    # also skipped; route is empty so the route-match branch is also skipped.
    assert failures == [], f"expected no failures, got {failures}"
