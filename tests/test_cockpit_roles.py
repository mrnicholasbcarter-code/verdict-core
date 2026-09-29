"""Tests for cockpit roles (controller/reviewer identity) and health panel (BOD-276 AC2/AC7, BOD-277 AC5)."""

from __future__ import annotations

from io import StringIO
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from rich.console import Console

from verdict.orchestration import cockpit_controls as cockpit
from verdict.orchestration import cockpit_nav as nav
from verdict.orchestration.cockpit_nav import (
    ROLE_CONTROLLER,
    ROLE_REVIEWER,
    CockpitState,
    identity_for_controller,
    identity_for_reviewer,
    is_role_row,
    render_detail_panel,
    render_selected_row,
    selectable_order_with_roles,
)
from verdict.orchestration.contracts import RunEvent
from verdict.orchestration.tui import RunView, render


def _event(seq: int, kind: str, node: str = "", **data: object) -> RunEvent:
    return RunEvent(
        seq=seq, at=f"2026-01-01T00:00:{seq:02d}+00:00", type=kind, node_id=node, data=data
    )


def _console(width: int = 100) -> Console:
    return Console(file=StringIO(), force_terminal=False, width=width, color_system=None)


def _rich_to_text(console: Console, renderable: Any) -> str:
    console.print(renderable)
    text = console.file.getvalue()
    console.file.truncate(0)
    console.file.seek(0)
    return text


# ---------------------------------------------------------------------------
# Selectable order includes controller/reviewer
# ---------------------------------------------------------------------------


def test_selectable_order_with_roles_empty() -> None:
    view = RunView()
    order = selectable_order_with_roles(view)
    assert order == []


def test_selectable_order_with_roles_workers_only() -> None:
    events = [_event(1, "node_state", "n1", state="RUNNING")]
    view = RunView.from_events(events)
    order = selectable_order_with_roles(view)
    assert ROLE_CONTROLLER not in order
    assert ROLE_REVIEWER not in order
    assert "n1" in order


def test_selectable_order_with_controller() -> None:
    events = [
        _event(1, "run_started", goal="g"),
        _event(2, "plan_started", route_id="cc/opus"),
        _event(3, "node_state", "n1", state="RUNNING"),
    ]
    view = RunView.from_events(events)
    order = selectable_order_with_roles(view)
    assert order[0] == ROLE_CONTROLLER
    assert "n1" in order
    assert ROLE_REVIEWER not in order


def test_selectable_order_with_controller_and_reviewer() -> None:
    events = [
        _event(1, "run_started", goal="g"),
        _event(2, "plan_started", route_id="cc/opus"),
        _event(3, "node_state", "n1", state="RUNNING"),
        _event(
            4, "review", status="PASS", reviewer="ocr", route_id="kr/haiku", blocking=0, findings=0
        ),
    ]
    view = RunView.from_events(events)
    order = selectable_order_with_roles(view)
    assert order[0] == ROLE_CONTROLLER
    assert order[-1] == ROLE_REVIEWER
    assert "n1" in order


# ---------------------------------------------------------------------------
# is_role_row helper
# ---------------------------------------------------------------------------


def test_is_role_row() -> None:
    assert is_role_row(ROLE_CONTROLLER)
    assert is_role_row(ROLE_REVIEWER)
    assert not is_role_row("n1")
    assert not is_role_row("")


# ---------------------------------------------------------------------------
# Controller identity: known and unknown
# ---------------------------------------------------------------------------


def test_controller_identity_known() -> None:
    events = [
        _event(1, "run_started", goal="g"),
        _event(2, "plan_started", route_id="cc/opus"),
        _event(
            3,
            "controller",
            state="HEALTHY",
            route_id="cc/opus",
            observed_model="cc/opus-4",
            session_ref="s-plan-1",
        ),
    ]
    view = RunView.from_events(events)
    ident = identity_for_controller(view, events)
    assert ident.selected_route == "cc/opus"
    assert ident.observed_route == "cc/opus-4"
    assert ident.session_ref == "s-plan-1"
    assert ident.mismatch  # different


def test_controller_identity_unknown() -> None:
    """Before the planner completes, observed identity is unknown."""
    events = [_event(1, "run_started", goal="g"), _event(2, "plan_started", route_id="cc/opus")]
    view = RunView.from_events(events)
    ident = identity_for_controller(view, events)
    assert ident.selected_route == "cc/opus"
    assert ident.observed_route == ""
    assert not ident.mismatch  # can't mismatch when unknown


def test_controller_identity_matching() -> None:
    """When selected == observed, no mismatch."""
    events = [
        _event(1, "plan_started", route_id="cc/opus"),
        _event(
            2,
            "controller",
            state="HEALTHY",
            route_id="cc/opus",
            observed_model="cc/opus",
            session_ref="s1",
        ),
    ]
    view = RunView.from_events(events)
    ident = identity_for_controller(view, events)
    assert not ident.mismatch


# ---------------------------------------------------------------------------
# Reviewer identity: known and unknown
# ---------------------------------------------------------------------------


def test_reviewer_identity_known_with_observed() -> None:
    """When the OCR payload reports a model, it is the observed identity."""
    events = [
        _event(
            1,
            "review",
            status="PASS",
            reviewer="ocr",
            route_id="kr/haiku",
            observed_model="kr/haiku",
            blocking=0,
            findings=0,
        )
    ]
    view = RunView.from_events(events)
    ident = identity_for_reviewer(view, events)
    assert ident.selected_route == "kr/haiku"
    assert ident.observed_route == "kr/haiku"
    assert not ident.mismatch


def test_reviewer_identity_no_observed_model() -> None:
    """When the OCR payload has no model, observed is empty, not the selected route."""
    events = [
        _event(
            1, "review", status="PASS", reviewer="ocr", route_id="kr/haiku", blocking=0, findings=0
        )
    ]
    view = RunView.from_events(events)
    ident = identity_for_reviewer(view, events)
    assert ident.selected_route == "kr/haiku"
    assert ident.observed_route == ""  # NOT kr/haiku: no fabrication
    assert not ident.mismatch  # cannot mismatch when observed is unknown


def test_reviewer_identity_unknown() -> None:
    """Before review completes, identity is unknown."""
    view = RunView()
    ident = identity_for_reviewer(view, [])
    assert ident.selected_route == ""
    assert ident.observed_route == ""


# ---------------------------------------------------------------------------
# Detail panel for controller and reviewer rows
# ---------------------------------------------------------------------------


def test_detail_panel_controller_shows_identity() -> None:
    events = [
        _event(1, "plan_started", route_id="cc/opus"),
        _event(
            2,
            "controller",
            state="HEALTHY",
            route_id="cc/opus",
            observed_model="cc/opus-4",
            session_ref="s1",
        ),
        _event(3, "node_state", "n1", state="RUNNING"),
    ]
    view = RunView.from_events(events)
    state = CockpitState()
    state.sync_order(selectable_order_with_roles(view))
    # Controller should be first
    assert state.selected_id == ROLE_CONTROLLER
    state.detail_open = True
    console = _console(100)
    rendered = render_detail_panel(view, events, state, plain=True, width=100)
    text = _rich_to_text(console, rendered)
    assert "role: controller" in text
    assert "selected route: cc/opus" in text
    assert "observed model: cc/opus-4" in text
    assert "session: s1" in text
    assert "MISMATCH" in text


def test_detail_panel_reviewer_shows_identity() -> None:
    events = [
        _event(1, "node_state", "n1", state="RUNNING"),
        _event(
            2,
            "review",
            status="PASS",
            reviewer="ocr",
            route_id="kr/haiku",
            observed_model="kr/haiku",
            blocking=0,
            findings=0,
        ),
    ]
    view = RunView.from_events(events)
    state = CockpitState()
    state.sync_order(selectable_order_with_roles(view))
    # Navigate to reviewer (last row)
    state.selected_index = len(state.node_order) - 1
    assert state.selected_id == ROLE_REVIEWER
    state.detail_open = True
    console = _console(100)
    rendered = render_detail_panel(view, events, state, plain=True, width=100)
    text = _rich_to_text(console, rendered)
    assert "role: reviewer" in text
    assert "selected route: kr/haiku" in text
    assert "observed model: kr/haiku" in text


def test_detail_panel_reviewer_shows_mismatch() -> None:
    """Reviewer detail panel must render MISMATCH line when selected != observed."""
    events = [
        _event(1, "node_state", "n1", state="RUNNING"),
        _event(
            2,
            "review",
            status="PASS",
            reviewer="ocr",
            route_id="kr/haiku",
            observed_model="cc/sonnet",  # mismatched
            blocking=0,
            findings=0,
        ),
    ]
    view = RunView.from_events(events)
    state = CockpitState()
    state.sync_order(selectable_order_with_roles(view))
    state.selected_index = len(state.node_order) - 1
    assert state.selected_id == ROLE_REVIEWER
    state.detail_open = True
    console = _console(100)
    rendered = render_detail_panel(view, events, state, plain=True, width=100)
    text = _rich_to_text(console, rendered)
    assert "MISMATCH" in text, f"Expected MISMATCH line in reviewer detail; got: {text!r}"


# ---------------------------------------------------------------------------
# render_selected_row for roles
# ---------------------------------------------------------------------------


def test_render_selected_row_controller() -> None:
    events = [
        _event(1, "plan_started", route_id="cc/opus"),
        _event(2, "controller", state="HEALTHY", route_id="cc/opus"),
    ]
    view = RunView.from_events(events)
    state = CockpitState()
    state.sync_order(selectable_order_with_roles(view))
    console = _console(100)
    text = _rich_to_text(console, render_selected_row(view, state, True))
    assert "controller" in text
    assert "HEALTHY" in text


def test_render_selected_row_reviewer() -> None:
    events = [
        _event(1, "node_state", "n1", state="RUNNING"),
        _event(
            2, "review", status="PASS", reviewer="ocr", route_id="kr/haiku", blocking=0, findings=0
        ),
    ]
    view = RunView.from_events(events)
    state = CockpitState()
    state.sync_order(selectable_order_with_roles(view))
    state.selected_index = len(state.node_order) - 1
    console = _console(100)
    text = _rich_to_text(console, render_selected_row(view, state, True))
    assert "reviewer" in text
    assert "PASS" in text


# ---------------------------------------------------------------------------
# Navigation: selecting controller/reviewer, deep-linking to routing
# ---------------------------------------------------------------------------


def test_navigate_to_controller_detail() -> None:
    events = [
        _event(1, "plan_started", route_id="cc/opus"),
        _event(2, "node_state", "n1", state="RUNNING"),
    ]
    view = RunView.from_events(events)
    state = CockpitState()
    state.sync_order(selectable_order_with_roles(view))
    assert state.selected_id == ROLE_CONTROLLER
    nav.dispatch_key(nav.KEY_ENTER, state, view)
    assert state.detail_open
    # Navigate down to worker
    nav.dispatch_key("j", state, view)
    assert state.selected_id == "n1"


def test_navigate_to_reviewer_via_keys() -> None:
    events = [
        _event(1, "plan_started", route_id="cc/opus"),
        _event(2, "node_state", "n1", state="RUNNING"),
        _event(
            3, "review", status="PASS", reviewer="ocr", route_id="kr/haiku", blocking=0, findings=0
        ),
    ]
    view = RunView.from_events(events)
    state = CockpitState()
    state.sync_order(selectable_order_with_roles(view))
    # Navigate up from controller to wrap to reviewer
    nav.dispatch_key("k", state, view)
    assert state.selected_id == ROLE_REVIEWER


# ---------------------------------------------------------------------------
# Health panel
# ---------------------------------------------------------------------------


def test_health_panel_shows_cooldown_evidence() -> None:
    events = [
        _event(1, "run_started", goal="g"),
        _event(2, "selection", "n1", route_id="cc/haiku", provider="cc", attempt=1),
        _event(3, "node_state", "n1", state="RUNNING", route_id="cc/haiku"),
        _event(
            4,
            "failure",
            "n1",
            category="rate_limited",
            action="REROUTE",
            route_id="cc/haiku",
            evidence="429",
        ),
        _event(
            5,
            "cooldown",
            key="cc",
            scope="provider",
            category="rate_limited",
            until="2026-01-01T00:05:00Z",
        ),
    ]
    view = RunView.from_events(events)
    state = cockpit.initial_state(view, events, Path("run"), node_id="n1")
    from verdict.design import PresentationMode

    mode = PresentationMode(color=False, unicode=False, animate=False, width=100, color_system=None)
    console = _console(100)
    rendered = cockpit.render_health(state, view, mode)
    text = _rich_to_text(console, rendered)
    assert "health evidence" in text
    assert "cooldowns:" in text
    assert "cc [provider]" in text
    assert "rate_limited" in text
    assert "failures:" in text
    assert "REROUTE" in text


def test_health_panel_no_issues() -> None:
    events = [
        _event(1, "selection", "n1", route_id="cc/haiku", provider="cc", attempt=1),
        _event(2, "node_state", "n1", state="VALIDATED", route_id="cc/haiku"),
    ]
    view = RunView.from_events(events)
    state = cockpit.initial_state(view, events, Path("run"), node_id="n1")
    from verdict.design import PresentationMode

    mode = PresentationMode(color=False, unicode=False, animate=False, width=100, color_system=None)
    console = _console(100)
    rendered = cockpit.render_health(state, view, mode)
    text = _rich_to_text(console, rendered)
    assert "no recorded health issues" in text


def test_health_panel_for_controller_row() -> None:
    """Health panel for the controller shows cooldowns matching the planner route."""
    events = [
        _event(1, "plan_started", route_id="cc/opus"),
        _event(
            2,
            "controller",
            state="HEALTHY",
            route_id="cc/opus",
            observed_model="cc/opus",
            session_ref="s1",
        ),
        _event(
            3,
            "cooldown",
            key="cc",
            scope="provider",
            category="rate_limited",
            until="2026-01-01T00:05:00Z",
        ),
        _event(4, "node_state", "n1", state="RUNNING"),
    ]
    view = RunView.from_events(events)
    state = cockpit.initial_state(view, events, Path("run"))
    # Select controller
    state.selected_index = 0
    assert state.selected_id == ROLE_CONTROLLER
    from verdict.design import PresentationMode

    mode = PresentationMode(color=False, unicode=False, animate=False, width=100, color_system=None)
    console = _console(100)
    rendered = cockpit.render_health(state, view, mode)
    text = _rich_to_text(console, rendered)
    assert "controller" in text
    assert "cooldowns:" in text


def test_health_key_toggles_panel() -> None:
    events = [
        _event(1, "selection", "n1", route_id="cc/haiku", attempt=1),
        _event(2, "node_state", "n1", state="RUNNING", route_id="cc/haiku"),
    ]
    view = RunView.from_events(events)
    state = cockpit.initial_state(view, events, Path("run"), node_id="n1")
    assert not state.health_open
    cockpit.dispatch_key(nav.KEY_HEALTH, state, view)
    assert state.health_open
    cockpit.dispatch_key(nav.KEY_HEALTH, state, view)
    assert not state.health_open


def test_health_panel_esc_closes() -> None:
    events = [
        _event(1, "selection", "n1", route_id="cc/haiku", attempt=1),
        _event(2, "node_state", "n1", state="RUNNING", route_id="cc/haiku"),
    ]
    view = RunView.from_events(events)
    state = cockpit.initial_state(view, events, Path("run"), node_id="n1")
    cockpit.dispatch_key(nav.KEY_HEALTH, state, view)
    assert state.health_open
    cockpit.dispatch_key(nav.KEY_ESC, state, view)
    assert not state.health_open


def test_health_panel_closes_when_other_panel_opens() -> None:
    events = [
        _event(1, "selection", "n1", route_id="cc/haiku", attempt=1),
        _event(2, "node_state", "n1", state="RUNNING", route_id="cc/haiku"),
    ]
    view = RunView.from_events(events)
    state = cockpit.initial_state(view, events, Path("run"), node_id="n1")
    cockpit.dispatch_key(nav.KEY_HEALTH, state, view)
    assert state.health_open
    cockpit.dispatch_key(nav.KEY_CONTEXT, state, view)
    assert not state.health_open


# ---------------------------------------------------------------------------
# Health panel NEVER probes — mutation check
# ---------------------------------------------------------------------------


def test_health_panel_never_calls_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure the health panel reads only from events, never probes live.

    Mutation check: if render_health were to call any live-probe function
    (openai_health_probe, probe_gateway, probe_catalog, probe_gateways,
    or the urllib3/httpx transport), the spy raises RuntimeError and the test
    FAILS.  Verify this locally by temporarily inserting a probe call into
    render_health, then remove it.
    """
    events = [
        _event(1, "selection", "n1", route_id="cc/haiku", attempt=1),
        _event(2, "node_state", "n1", state="RUNNING", route_id="cc/haiku"),
        _event(3, "failure", "n1", category="rate_limited", action="REROUTE", route_id="cc/haiku"),
        _event(
            4,
            "cooldown",
            key="cc",
            scope="provider",
            category="rate_limited",
            until="2026-01-01T00:05:00Z",
        ),
    ]
    view = RunView.from_events(events)
    state = cockpit.initial_state(view, events, Path("run"), node_id="n1")
    from verdict.design import PresentationMode

    mode = PresentationMode(color=False, unicode=False, animate=False, width=100, color_system=None)

    # Spy on every live-probe entry point used by the eligibility/routing system.
    # These raise RuntimeError so any accidental probe call causes the test to fail.
    probe_error = RuntimeError("live probe called from render_health — this must not happen")
    mocks: list[MagicMock] = []
    for target in (
        "verdict.subagent_selection.openai_health_probe",
        "verdict.home.probe_gateway",
        "verdict.omniroute_catalog.probe_catalog",
        "verdict.provider_detection.probe_gateways",
    ):
        m = MagicMock(side_effect=probe_error)
        monkeypatch.setattr(target, m, raising=True)
        mocks.append(m)

    # Also block the low-level HTTP transport so no accidental network call passes through.
    import urllib.request

    url_mock = MagicMock(side_effect=probe_error)
    monkeypatch.setattr(urllib.request, "urlopen", url_mock, raising=True)
    mocks.append(url_mock)

    # Must complete without raising (no probe was triggered)
    cockpit.render_health(state, view, mode)

    # All spies must be un-called: none of the probe entry points were invoked
    for m in mocks:
        assert m.call_count == 0, f"Live probe was called: {m}"


# ---------------------------------------------------------------------------
# Narrow terminals
# ---------------------------------------------------------------------------


def test_narrow_terminal_controller_detail() -> None:
    events = [
        _event(1, "plan_started", route_id="cc/opus"),
        _event(
            2,
            "controller",
            state="HEALTHY",
            route_id="cc/opus",
            observed_model="cc/opus",
            session_ref="s1",
        ),
    ]
    view = RunView.from_events(events)
    state = CockpitState()
    state.sync_order(selectable_order_with_roles(view))
    assert state.selected_id == ROLE_CONTROLLER
    console = _console(40)
    rendered = render_detail_panel(view, events, state, plain=True, width=40)
    text = _rich_to_text(console, rendered)
    assert "role: controller" in text


def test_narrow_terminal_health_panel() -> None:
    events = [
        _event(1, "selection", "n1", route_id="cc/haiku", attempt=1),
        _event(2, "node_state", "n1", state="RUNNING", route_id="cc/haiku"),
        _event(
            3,
            "cooldown",
            key="cc",
            scope="provider",
            category="rate_limited",
            until="2026-01-01T00:05:00Z",
        ),
    ]
    view = RunView.from_events(events)
    state = cockpit.initial_state(view, events, Path("run"), node_id="n1")
    from verdict.design import PresentationMode

    mode = PresentationMode(color=False, unicode=False, animate=False, width=40, color_system=None)
    console = _console(40)
    rendered = cockpit.render_health(state, view, mode)
    text = _rich_to_text(console, rendered)
    assert "health evidence" in text


# ---------------------------------------------------------------------------
# Dashboard rendering: controller/review blocks show identity
# ---------------------------------------------------------------------------


def test_controller_block_shows_planner_identity() -> None:
    events = [
        _event(1, "run_started", goal="g"),
        _event(2, "plan_started", route_id="cc/opus"),
        _event(
            3,
            "controller",
            state="HEALTHY",
            route_id="cc/opus",
            observed_model="cc/opus-4",
            session_ref="s1",
        ),
        _event(
            4,
            "plan_ready",
            nodes=[{"node_id": "n1", "objective": "build"}],
            layers=[["n1"]],
            topology="SOLO",
        ),
        _event(5, "node_state", "n1", state="VALIDATED"),
        _event(6, "run_finished", outcome="COMPLETE", reason="done"),
    ]
    view = RunView.from_events(events)
    console = _console(120)
    text = _rich_to_text(console, render(view, width=120, plain=True))
    assert "planner selected: cc/opus" in text
    assert "planner observed: cc/opus-4" in text


def test_controller_block_planner_identity_unknown() -> None:
    events = [_event(1, "run_started", goal="g"), _event(2, "plan_started", route_id="cc/opus")]
    view = RunView.from_events(events)
    console = _console(120)
    text = _rich_to_text(console, render(view, width=120, plain=True))
    assert "planner selected: cc/opus" in text
    assert "planner observed: not reported yet" in text


def test_review_block_shows_reviewer_identity() -> None:
    events = [
        _event(1, "run_started", goal="g"),
        _event(
            2,
            "review",
            status="PASS",
            reviewer="ocr",
            route_id="kr/haiku",
            observed_model="kr/haiku",
            blocking=0,
            findings=0,
        ),
    ]
    view = RunView.from_events(events)
    console = _console(120)
    text = _rich_to_text(console, render(view, width=120, plain=True))
    assert "reviewer selected: kr/haiku" in text
    assert "reviewer observed: kr/haiku" in text


def test_review_block_no_observed_model() -> None:
    """When observed_model is missing, 'not reported yet' shown instead of selected route."""
    events = [
        _event(1, "run_started", goal="g"),
        _event(
            2, "review", status="PASS", reviewer="ocr", route_id="kr/haiku", blocking=0, findings=0
        ),
    ]
    view = RunView.from_events(events)
    console = _console(120)
    text = _rich_to_text(console, render(view, width=120, plain=True))
    assert "reviewer selected: kr/haiku" in text
    assert "reviewer observed: not reported yet" in text


# ---------------------------------------------------------------------------
# verdict watch --panel health (CLI integration)
# ---------------------------------------------------------------------------


def test_cli_panel_health_choice() -> None:
    """The CLI accepts --panel health."""
    import argparse

    from verdict.orchestration.cli import add_parsers

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers()
    add_parsers(sub)
    args = parser.parse_args(["watch", "--panel", "health", "dummy-run"])
    assert args.panel == "health"


# ---------------------------------------------------------------------------
# Planner observed identity staleness (HIGH fix, BOD-276)
# ---------------------------------------------------------------------------


def test_planner_observed_clears_on_route_change() -> None:
    """Event 1: route A with observed A' -> Event 2: route B, no observed field.
    The view must show 'not reported yet', not A'.
    """
    events = [
        _event(1, "plan_started", route_id="cc/opus"),
        _event(
            2,
            "controller",
            state="HEALTHY",
            route_id="cc/opus",
            observed_model="cc/opus",
            session_ref="sess-1",
        ),
        # New planner attempt on a different route, no observed identity fields
        _event(3, "controller", state="HEALTHY", route_id="kr/haiku"),
    ]
    view = RunView.from_events(events)
    console = _console(120)
    text = _rich_to_text(console, render(view, width=120, plain=True))
    assert "planner selected: kr/haiku" in text, f"Got: {text!r}"
    assert "not reported yet" in text, f"Stale identity visible, should be gone: {text!r}"
    observed_tail = text.split("planner observed:")[-1].split("\n")[0]
    assert "cc/opus" not in observed_tail, (
        f"Stale observed model leaked into planner observed line: {observed_tail!r}"
    )


def test_planner_observed_set_unconditionally_in_healthy() -> None:
    """HEALTHY with observed_model always overwrites regardless of prior value."""
    events = [
        _event(1, "plan_started", route_id="cc/opus"),
        _event(
            2,
            "controller",
            state="HEALTHY",
            route_id="cc/opus",
            observed_model="cc/opus-old",
            session_ref="sess-0",
        ),
        _event(
            3,
            "controller",
            state="HEALTHY",
            route_id="cc/opus",
            observed_model="cc/opus-new",
            session_ref="sess-1",
        ),
    ]
    view = RunView.from_events(events)
    console = _console(120)
    text = _rich_to_text(console, render(view, width=120, plain=True))
    assert "cc/opus-new" in text, f"New observed model missing: {text!r}"
    assert "cc/opus-old" not in text, f"Old stale observed model leaked: {text!r}"


# ---------------------------------------------------------------------------
# _fmt_until ISO parse guard (LOW fix)
# ---------------------------------------------------------------------------


def test_fmt_until_valid_iso() -> None:
    """Valid ISO timestamp returns HH:MM:SS slice."""
    from verdict.orchestration.cockpit_controls import _fmt_until

    assert _fmt_until("2026-01-01T00:05:00Z") == "00:05:00"


def test_fmt_until_short_value() -> None:
    """Short value (non-ISO) is returned as-is."""
    from verdict.orchestration.cockpit_controls import _fmt_until

    assert _fmt_until("soon") == "soon"


def test_fmt_until_long_non_iso_with_t() -> None:
    """Long non-ISO value containing 'T' is returned as-is, not sliced."""
    from verdict.orchestration.cockpit_controls import _fmt_until

    value = "TENTATIVE-2026-never-a-real-timestamp-but-long-enough"
    result = _fmt_until(value)
    assert result == value, f"Expected raw value back, got: {result!r}"


def test_fmt_until_empty() -> None:
    """Empty string returns '-'."""
    from verdict.orchestration.cockpit_controls import _fmt_until

    assert _fmt_until("") == "-"
