"""Shared-action cockpit controls and recorded execution deep links.

This module is a presenter. Domain actions decide whether a request can be
queued; the orchestration controller remains the authority for admission,
recovery, cancellation and proof. A queued request is never shown as completion.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from io import StringIO
from pathlib import Path
from typing import Any, Literal

from rich.console import Console, Group, RenderableType
from rich.live import Live
from rich.text import Text

from verdict.actions import ActionResult
from verdict.design import PresentationMode, panel, presentation_mode, render_state, tokens_for
from verdict.orchestration import cockpit_nav as nav
from verdict.orchestration.context_render import context_view_from_run_view, render_context
from verdict.orchestration.context_view import ContextView, context_view
from verdict.orchestration.contracts import RunEvent
from verdict.orchestration.routing_render import render_routing, routing_view_from_run_view
from verdict.orchestration.routing_view import RoutingView
from verdict.orchestration.tui import RunView, read_events, render


def _fmt_until(value: str) -> str:
    """Return HH:MM:SS time slice for ISO timestamps, raw value otherwise."""
    if not value:
        return "-"
    try:
        from datetime import datetime

        datetime.fromisoformat(value)
        return value[11:19]
    except ValueError:
        return value

PanelName = Literal["routing", "context", "receipt", "health"]
KEY_CANCEL_RUN = "x"
KEY_CANCEL_NODE = "X"
KEY_RETRY_NODE = "t"
KEY_RECEIPT = "p"
CONTROL_KEYS = {
    KEY_CANCEL_RUN: "run.cancel",
    KEY_CANCEL_NODE: "run.cancel-node",
    KEY_RETRY_NODE: "run.retry-node",
}


@dataclass
class ControlCockpitState(nav.CockpitState):
    """Presentation state; no recovery budget or policy lives here."""

    run_dir: Path | None = None
    events: list[RunEvent] = field(default_factory=list)
    pending_action: str = ""
    last_action: str = ""
    control_result: ActionResult | None = None
    receipt_open: bool = False
    receipt_result: ActionResult | None = None
    context_open: bool = False
    health_open: bool = False
    context_view: ContextView | None = None
    context_render: Any = None
    context_render_text: Any = None


def submit_control(state: nav.CockpitState, action: str) -> ActionResult:
    """Delegate every mutation to the same registered action as the CLI."""
    from verdict.actions import run_action

    params: dict[str, Any] = {"run_dir": getattr(state, "run_dir", None), "requested_by": "cockpit"}
    if action in {"run.cancel-node", "run.retry-node"}:
        params["node_id"] = state.selected_id
    result = run_action(action, params)
    if isinstance(state, ControlCockpitState):
        state.last_action = action
        state.control_result = result
    return result


def _records(events: Sequence[Any]) -> list[RunEvent]:
    return [e if isinstance(e, RunEvent) else RunEvent.from_dict(e) for e in events]


def context_for_selected(
    state: nav.CockpitState, view: RunView, events: Sequence[Any] | None = None
) -> ContextView:
    """Select a node from the existing provenance projection, not repo files."""
    events = getattr(state, "events", ()) if events is None else events
    projected = context_view(_records(events)) if events else context_view_from_run_view(view)
    return replace(projected, nodes=[n for n in projected.nodes if n.node_id == state.selected_id])


def routing_for_selected(
    state: nav.CockpitState, view: RunView, events: Sequence[Any] | None = None
) -> RoutingView:
    """Select recorded evaluations for the worker, without ranking candidates."""
    events = getattr(state, "events", None) if events is None else events
    projected = routing_view_from_run_view(view, events)
    evaluations = [e for e in projected.evaluations if e.node_id == state.selected_id]
    return replace(projected, evaluations=evaluations[-1:])


def open_receipt_view(state: ControlCockpitState) -> None:
    """Read the receipt through the existing proof action; never create one."""
    from verdict.actions import run_action

    state.receipt_open = not state.receipt_open
    if state.receipt_open:
        state.receipt_result = run_action("run-receipt", {"run_dir": str(state.run_dir or "")})


def select_panel(state: ControlCockpitState, view: RunView, name: PanelName) -> None:
    state.routing_open = False
    state.context_open = False
    state.receipt_open = False
    state.health_open = False
    if name == "routing":
        nav.open_routing_view(state, view)
    elif name == "context":
        nav.open_context_view(state, view)
    elif name == "health":
        state.health_open = True
    else:
        open_receipt_view(state)


def dispatch_key(key: str, state: ControlCockpitState, view: RunView) -> bool:
    """Confirmation is presentation only; the domain still decides policy."""
    if state.pending_action:
        if key == KEY_CANCEL_RUN and state.pending_action == "run.cancel":
            nav.submit_control(state, state.pending_action)
            state.pending_action = ""
            return True
        if key == nav.KEY_ENTER and state.pending_action != "run.cancel":
            nav.submit_control(state, state.pending_action)
            state.pending_action = ""
            return True
        if key == nav.KEY_ESC:
            state.pending_action = ""
            return True
        if key == nav.KEY_QUIT:
            state.pending_action = ""
            state.quit_requested = True
            return True
        return False
    if key in CONTROL_KEYS:
        state.pending_action = CONTROL_KEYS[key]
        return True
    if key == KEY_RECEIPT:
        state.context_open = False
        state.routing_open = False
        state.health_open = False
        open_receipt_view(state)
        return True
    if key == nav.KEY_HEALTH:
        state.context_open = False
        state.routing_open = False
        state.receipt_open = False
        state.health_open = not state.health_open
        return True
    if key == nav.KEY_ESC and state.health_open:
        state.health_open = False
        return True
    if key == nav.KEY_ESC and state.receipt_open:
        state.receipt_open = False
        return True
    if key == nav.KEY_CONTEXT:
        state.routing_open = False
        state.receipt_open = False
        state.health_open = False
    elif key == nav.KEY_ROUTING:
        state.context_open = False
        state.receipt_open = False
        state.health_open = False
    return nav.dispatch_key(key, state, view) or key in {nav.KEY_CONTEXT, nav.KEY_ROUTING}


def _style(token: str, mode: PresentationMode) -> str:
    return tokens_for(mode.color_system).get(token, "") if mode.color else ""


def render_controls(state: ControlCockpitState, mode: PresentationMode) -> RenderableType:
    lines: list[RenderableType] = [
        Text(
            "x cancel run (twice) | X cancel worker | t retry worker", style=_style("ACCENT", mode)
        ),
        Text(
            "Controls use shared actions. Controller policy decides; proof stays required.",
            style=_style("MUTED", mode),
        ),
    ]
    if state.pending_action:
        target = (
            "run" if state.pending_action == "run.cancel" else state.selected_id or "unknown worker"
        )
        confirmation = (
            "press x again to cancel the run; Esc backs out."
            if state.pending_action == "run.cancel"
            else f"Confirm {state.pending_action} for {target}: Enter sends, Esc backs out."
        )
        lines.append(Text(confirmation, style=_style("TEXT", mode)))
    if state.control_result is not None:
        result = state.control_result
        data = result.data if isinstance(result.data, Mapping) else {}
        if result.ok:
            request = data.get("request", data)
            request_id = request.get("id", "unknown") if isinstance(request, Mapping) else "unknown"
            text = f"Request {request_id} queued; controller policy outcome pending."
            token = "INFO"
        else:
            reason = str(data.get("reason") or data.get("error") or "domain policy refused")
            lines.append(
                Text.assemble(
                    ("Refused ", _style("SECONDARY", mode)),
                    (state.last_action or "control", _style("TEXT", mode)),
                    (f": {reason}", _style("SECONDARY", mode)),
                )
            )
            text = ""
            token = "SECONDARY"
        if text:
            lines.append(Text(text, style=_style(token, mode)))
    controls = [
        e
        for e in state.events
        if e.type == "control" and (not e.node_id or e.node_id == state.selected_id)
    ]
    for event in controls[-3:]:
        accepted = event.data.get("accepted")
        label = "accepted" if accepted is True else "refused" if accepted is False else "unknown"
        lines.append(
            Text(
                f"Observed {event.data.get('kind', '?')}: {label}; {event.data.get('reason', 'unknown')}",
                style=_style("INFO" if accepted is True else "SECONDARY", mode),
            )
        )
    return panel(Group(*lines), title="safe controls", mode=mode)


def render_receipt(state: ControlCockpitState, mode: PresentationMode) -> RenderableType:
    result = state.receipt_result
    data = result.data if result is not None and isinstance(result.data, Mapping) else {}
    receipt = data.get("receipt")
    lines: list[RenderableType] = []
    if not isinstance(receipt, Mapping):
        lines.append(
            Text(
                "Receipt not available. Completion and proof are unknown.",
                style=_style("UNKNOWN", mode),
            )
        )
        if data.get("error"):
            lines.append(Text(str(data["error"]), style=_style("MUTED", mode)))
    else:
        lines.append(
            Text(
                f"run: {receipt.get('run_id', 'unknown')}  outcome: {data.get('outcome', 'unknown')}",
                style=_style("PRIMARY", mode),
            )
        )
        lines.append(Text(f"reason: {data.get('reason', 'unknown')}"))
        problems = data.get("problems", [])
        lines.append(
            Text(
                "digest/proof checks: "
                + (
                    "; ".join(map(str, problems))
                    if problems
                    else "no verification problems recorded"
                ),
                style=_style("WARNING" if problems else "INFO", mode),
            )
        )
        lines.append(
            Text(
                f"events: {receipt.get('event_count', 'unknown')}  digest: {receipt.get('events_digest', 'unknown')}",
                style=_style("SECONDARY", mode),
            )
        )
        selected = [n for n in receipt.get("nodes", []) if n.get("node_id") == state.selected_id]
        if not selected:
            lines.append(
                Text(
                    f"worker {state.selected_id or 'unknown'}: no receipt evidence",
                    style=_style("UNKNOWN", mode),
                )
            )
        for node in selected:
            lines.append(Text(f"worker: {state.selected_id}"))
            lines.append(render_state(str(node.get("final_state", "unknown")), mode=mode))
            lines.append(
                Text(
                    f"attempts: {len(node.get('attempts', []))}  reason: {node.get('reason') or 'not recorded'}"
                )
            )
    return panel(Group(*lines), title="receipt summary", mode=mode)


def _health_for_selected(
    state: ControlCockpitState, view: RunView
) -> tuple[list[Any], list[Any], list[Any]]:
    """Extract recorded health evidence for the selected row.

    Returns (cooldowns, failures, eligibility_events) from the event stream.
    Never probes — reads only from the authoritative recorded events.
    """
    selected = state.selected_id or ""
    # For role rows, find the role's route to match against cooldowns/failures.
    if selected == nav.ROLE_CONTROLLER:
        route = getattr(view, "controller_route", "") or ""
    elif selected == nav.ROLE_REVIEWER:
        review = getattr(view, "review", None)
        route = getattr(review, "route_id", "") if review else ""
    else:
        node = view.nodes.get(selected)
        route = getattr(node, "route_id", "") if node else ""

    cooldowns = [
        c
        for c in view.cooldowns.values()
        if c.key == route or (c.scope == "provider" and route.startswith(c.key + "/"))
    ]
    failures = [
        f
        for f in view.failures
        if f.route_id == route or (not nav.is_role_row(selected) and f.node_id == selected)
    ]
    # eligibility events matching this route
    elig_events = [
        e
        for e in state.events
        if e.type == "eligibility"
        and (
            e.node_id == selected
            or (e.data.get("selected") == route and route)
            or (e.data.get("revoked") == route and route)
        )
    ]
    return cooldowns, failures, elig_events


def render_health(
    state: ControlCockpitState, view: RunView, mode: PresentationMode
) -> RenderableType:
    """Health panel: recorded cooldown and failure evidence for the selected route.

    Reads ONLY from the authoritative recorded events. Never live-probes.
    """
    cooldowns, failures, elig_events = _health_for_selected(state, view)
    lines: list[RenderableType] = []

    selected = state.selected_id or ""
    if nav.is_role_row(selected):
        label = "controller" if selected == nav.ROLE_CONTROLLER else "reviewer"
    else:
        label = selected
    lines.append(Text(f"health evidence for: {label}", style=_style("PRIMARY", mode)))

    if not cooldowns and not failures and not elig_events:
        lines.append(Text("no recorded health issues", style=_style("INFO", mode)))
    else:
        if cooldowns:
            lines.append(Text("cooldowns:", style=_style("ACCENT", mode)))
            for c in cooldowns:
                lines.append(
                    Text(
                        f"  {c.key} [{c.scope}] {c.category} until {_fmt_until(c.until)}",
                        style=_style("COOLDOWN", mode),
                    )
                )
        if failures:
            lines.append(Text("failures:", style=_style("ACCENT", mode)))
            for f in failures[-5:]:
                lines.append(
                    Text(
                        f"  {f.node_id or 'run'}: {f.category} -> {f.action}"
                        f"{' on ' + f.route_id if f.route_id else ''}"
                        f"{' [injected]' if f.fault_injected else ''}",
                        style=_style("WARNING", mode),
                    )
                )
        if elig_events:
            lines.append(Text("eligibility:", style=_style("ACCENT", mode)))
            for e in elig_events[-3:]:
                revoked = e.data.get("revoked")
                selected_route = e.data.get("selected")
                if revoked:
                    lines.append(Text(f"  revoked: {revoked}", style=_style("ERROR", mode)))
                elif selected_route:
                    lines.append(Text(f"  selected: {selected_route}", style=_style("INFO", mode)))

    return panel(Group(*lines), title="health evidence", mode=mode)


def render_cockpit(
    view: RunView, state: ControlCockpitState, mode: PresentationMode, *, dashboard: bool = True
) -> RenderableType:
    """Compose existing views and renderers; every indicator is observed data."""
    plain = not mode.color
    blocks: list[RenderableType] = [nav.render_selected_row(view, state, plain)]
    if dashboard:
        blocks.append(render(view, width=mode.width, plain=plain))
    if state.detail_open:
        blocks.append(
            nav.render_detail_panel(
                view, state.events, state, plain=plain, width=mode.width, mode=mode
            )
        )
    if state.routing_open:
        blocks.append(render_routing(routing_for_selected(state, view), mode))
    if state.context_open:
        blocks.append(render_context(context_for_selected(state, view), mode))
    if state.receipt_open:
        blocks.append(render_receipt(state, mode))
    if state.health_open:
        blocks.append(render_health(state, view, mode))
    blocks.append(render_controls(state, mode))
    if state.help_open:
        blocks.append(nav.render_help(plain=plain))
    blocks.append(nav.render_footer(plain=plain))
    return Group(*blocks)


def initial_state(
    view: RunView,
    events: Sequence[Any],
    run_dir: Path,
    *,
    node_id: str | None = None,
    panel_name: PanelName | None = None,
) -> ControlCockpitState:
    state = ControlCockpitState(run_dir=run_dir, events=_records(events))
    state.sync_order(nav.selectable_order_with_roles(view))
    if node_id is not None:
        if node_id not in view.nodes:
            raise ValueError(f"unknown node: {node_id}")
        state.selected_index = state.node_order.index(node_id)
        state.detail_open = True
    if panel_name is not None:
        select_panel(state, view, panel_name)
    return state


def render_run_text(
    run_dir: Path,
    *,
    node_id: str | None = None,
    panel_name: PanelName | None = None,
    mode: PresentationMode | None = None,
) -> str:
    events = read_events(run_dir / "events.jsonl")
    view = RunView.from_events(events)
    state = initial_state(view, events, run_dir, node_id=node_id, panel_name=panel_name)
    mode = presentation_mode() if mode is None else mode
    target = Console(
        file=StringIO(),
        width=mode.width,
        height=200,
        record=True,
        color_system=mode.color_system if mode.color else None,
        force_terminal=mode.color,
        no_color=not mode.color,
        markup=False,
        highlight=False,
    )
    target.print(render_cockpit(view, state, mode))
    return target.export_text(styles=mode.color)


def run_cockpit(
    run_dir: Path,
    *,
    console: Console,
    key_reader: nav.KeyReader,
    node_id: str | None = None,
    panel_name: PanelName | None = None,
    events_source: Callable[[], list[RunEvent]] | None = None,
    initial_view: RunView | None = None,
    poll_seconds: float = 0.05,
    max_iterations: int = 2000,
    stop_when_final: bool = True,
) -> RunView:
    """Bounded input loop. Bursts project once and coalesce into one frame."""
    source = events_source or (lambda: read_events(run_dir / "events.jsonl"))
    events = source()
    view = RunView.from_events(events) if initial_view is None else initial_view
    if initial_view is not None:
        for event in events:
            view.apply(event)
    state = initial_state(view, events, run_dir, node_id=node_id, panel_name=panel_name)
    seen = len(events)

    class _Stream:
        width = console.width

        def isatty(self) -> bool:
            return console.is_terminal

    mode = presentation_mode(_Stream())
    live: Live | None = None
    last_render = 0.0
    changed = True
    dirty = True
    try:
        if mode.animate:
            live = Live(
                render_cockpit(view, state, mode),
                console=console,
                auto_refresh=False,
                transient=False,
            )
            live.start(refresh=True)
        for _ in range(max(0, max_iterations)):
            fresh = source()
            updated = len(fresh) > seen
            if updated:
                for event in fresh[seen:]:
                    view.apply(event)
                seen = len(fresh)
                state.events = fresh
                state.sync_order(nav.selectable_order_with_roles(view))
                if state.receipt_open and view.final:
                    state.receipt_open = False
                    open_receipt_view(state)
            dirty = dirty or updated
            now = time.monotonic()
            if changed or (dirty and now - last_render >= 0.1) or (view.final and stop_when_final):
                frame = render_cockpit(view, state, mode)
                if live is not None:
                    live.update(frame, refresh=True)
                else:
                    console.print(frame)
                last_render = now
                changed = False
                dirty = False
            try:
                key = key_reader.read(timeout=poll_seconds)
            except EOFError:
                break
            if key is not None:
                changed = dispatch_key(key, state, view)
            if state.quit_requested or (view.final and stop_when_final):
                break
    finally:
        if live is not None:
            live.stop()
        key_reader.close()
    return view
