"""Trace timeline renderer — human-readable text and Rich renderable (BOD-279).

Consumes :class:`TraceView` only. Never reads events directly. Plain text
honours VERDICT_PLAIN, NO_COLOR, non-TTY, and reduced motion. JSON has no ANSI.
"""

from __future__ import annotations

from typing import Any

from rich.cells import cell_len
from rich.console import Group, RenderableType
from rich.table import Table
from rich.text import Text

from verdict.design import PresentationMode, panel, presentation_mode, state_style, token_style
from verdict.orchestration.trace_view import STEP_KINDS, TraceStep, TraceView

# Kind -> (design.py state name, token). Glyphs come only from state_style,
# and every one of those glyphs is width-1. Kinds with no matching state use
# the muted unknown glyph.
_KIND_STATE: dict[str, tuple[str, str]] = {
    "request": ("dispatched", "SECONDARY"),
    "classification": ("unknown", "SECONDARY"),
    "plan": ("planned", "SECONDARY"),
    "context": ("unknown", "SECONDARY"),
    "routing": ("unknown", "SECONDARY"),
    "selection": ("validated", "SECONDARY"),
    "dispatch": ("dispatched", "SECONDARY"),
    "terminal": ("validated", "SUCCESS"),
    "failure": ("failed", "ERROR"),
    "cooldown": ("cooldown", "WARNING"),
    "reassign": ("reassigned", "PRIMARY"),
    "verify": ("validated", "SUCCESS"),
    "barrier": ("blocked", "SECONDARY"),
    "integrate": ("validated", "SECONDARY"),
    "review": ("validated", "SECONDARY"),
    "receipt": ("unknown", "SECONDARY"),
    "run_finished": ("validated", "SUCCESS"),
}

_KIND_WIDTH = max(len(kind) for kind in STEP_KINDS)


def _kind_glyph(kind: str, *, unicode: bool) -> str:
    state, _token = _KIND_STATE.get(kind, ("unknown", "SECONDARY"))
    style = state_style(state)
    glyph = style.glyph if unicode else style.ascii_glyph
    if cell_len(glyph) != 1:
        raise ValueError(f"kind glyph for {kind} is not width-1: {glyph!r}")
    return glyph


def _kind_token(kind: str) -> str:
    return _KIND_STATE.get(kind, ("unknown", "SECONDARY"))[1]


def _step_state(step: TraceStep) -> tuple[str, str]:
    """(state, token) for one step, from its observed outcome, not only its kind.

    A terminal or verify step that failed must never render as validated.
    """
    state, token = _KIND_STATE.get(step.kind, ("unknown", "SECONDARY"))
    if step.kind in ("terminal", "verify", "review"):
        ev = step.evidence
        ok = ev.get("ok", ev.get("passed", ev.get("valid")))
        status = str(ev.get("status") or "").upper()
        if ok is False or status in ("FAIL", "FAILED", "BLOCKED", "REJECTED"):
            return "failed", "ERROR"
        if step.kind == "review" and status == "PASS":
            return "validated", "SUCCESS"
    return state, token


def _first(evidence: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = evidence.get(key)
        if value not in (None, ""):
            return str(value)
    return ""


def _evidence_summary(step: TraceStep) -> str:
    """One-line summary from evidence dict."""
    ev = dict(step.evidence)
    kind = step.kind
    parts: list[str] = []
    if kind == "request":
        goal = _first(ev, "goal")
        if goal:
            parts.append(f"goal={_trunc(goal, 50)}")
    elif kind == "selection":
        route = _first(ev, "selected_route", "route_id")
        model = _first(ev, "model", "reported_model")
        if route:
            parts.append(f"route={route}")
        if model and model != route:
            parts.append(f"model={model}")
    elif kind == "failure":
        category = _first(ev, "category", "reason", "failure_kind")
        if category:
            parts.append(f"category={_trunc(category, 40)}")
    elif kind == "cooldown":
        key = _first(ev, "key", "route_id", "route")
        until = _first(ev, "until")
        if key:
            parts.append(f"key={key}")
        if until:
            parts.append(f"until={until}")
    elif kind == "reassign":
        old = _first(ev, "from_route", "old_route")
        new = _first(ev, "to_route", "new_route", "replacement_route")
        if old or new:
            parts.append(f"{old} → {new}".strip())
    elif kind == "terminal":
        route = _first(ev, "executed_model", "reported_model", "route_id")
        outcome = "ok" if ev.get("ok") is True else "failed" if ev.get("ok") is False else ""
        if route:
            parts.append(f"model={route}")
        if outcome:
            parts.append(outcome)
    elif kind == "verify":
        ok = ev.get("ok", ev.get("passed", ev.get("valid")))
        if ok is not None:
            parts.append(f"passed={ok}")
    elif kind == "review":
        route = _first(ev, "route_id", "reviewer_route")
        status = _first(ev, "status", "outcome", "verdict")
        # Two source events share this kind: the reviewer's run (review_attempt)
        # and the review verdict (review). Label them so they never read as a duplicate.
        if ev.get("type") == "review_attempt":
            attempt = _first(ev, "attempt")
            parts.append(f"attempt {attempt}" if attempt else "attempt")
        else:
            parts.append("verdict")
            blocking = ev.get("blocking")
            if blocking is not None:
                parts.append(f"blocking={blocking}")
        if route:
            parts.append(f"reviewer={route}")
        if status:
            parts.append(status)
    elif kind == "run_finished":
        outcome = _first(ev, "outcome")
        if outcome:
            parts.append(f"outcome={outcome}")
    return "  ".join(parts)


def _trunc(s: str, n: int) -> str:
    s = str(s).replace("\n", " ").strip()
    return s[:n] + "…" if len(s) > n else s


def render_trace_text(view: TraceView, width: int = 100) -> str:
    """Plain, NO_COLOR-safe text. No ANSI, no required box-drawing."""
    width = max(40, int(width))
    lines: list[str] = []
    lines.append(_trunc(f"trace  run={view.run_id or 'unknown'}", width))
    if view.goal:
        lines.append(_trunc(f"  goal: {view.goal}", width))
    lines.append(_trunc(f"  steps: {len(view.steps)}  schema: {view.schema_version}", width))
    lines.append("")

    seq_w = 5
    kind_w = _KIND_WIDTH + 1
    node_w = min(12, max((len(s.node_id) for s in view.steps), default=4))
    header = f"{'seq':<{seq_w}} {'kind':<{kind_w}} {'node':<{node_w}} detail"
    lines.append(header[:width])
    lines.append("-" * min(width, len(header)))

    for step in view.steps:
        summary = _evidence_summary(step).replace("→", "->")
        node = (step.node_id or "")[:node_w]
        line = f"{step.seq:<{seq_w}} {step.kind:<{kind_w}} {node:<{node_w}} {summary}"
        if len(line) > width:
            line = line[: width - 1] + "…"
        lines.append(line)
        # Hint: show the exact command to open the context panel for context steps
        if step.kind == "context" and step.node_id and view.run_id:
            hint = f"      # verdict trace {view.run_id} --node {step.node_id} --panel context"
            if len(hint) <= width:
                lines.append(hint)
            else:
                lines.append(hint[: width - 1] + "…")

    lines.append("")
    if view.projection_errors:
        lines.append("projection errors:")
        for err in view.projection_errors:
            lines.append(_trunc(f"  {err}", width))
        lines.append("")
    return "\n".join(lines)


def render_trace(
    view: TraceView, mode: PresentationMode | None = None, *, step_index: int | None = None
) -> RenderableType:
    """Rich renderable of a :class:`TraceView`."""
    mode = presentation_mode() if mode is None else mode
    blocks: list[RenderableType] = []

    run_part = f"  run={view.run_id or 'unknown'}"
    goal_part = f"  goal={view.goal}" if view.goal else ""
    # Keep the whole header inside the console. Styled truncation keeps ANSI
    # and can still print past the requested width.
    room = max(0, mode.width - len("trace") - len(run_part))
    if len(goal_part) > room:
        goal_part = (goal_part[: max(0, room - 1)] + "…") if room else ""
    header = Text.assemble(
        ("trace", token_style("PRIMARY", mode.color_system)),
        (run_part, token_style("SECONDARY", mode.color_system)),
        (goal_part, token_style("MUTED", mode.color_system)),
    )
    blocks.append(header)
    blocks.append(
        Text(
            f"steps: {len(view.steps)}  schema: {view.schema_version}",
            style=token_style("MUTED", mode.color_system),
        )
    )

    # Glyph plus one space plus the longest kind. Fixed so "classification"
    # cannot wrap under its glyph.
    kind_col = 1 + 1 + _KIND_WIDTH
    table = Table(
        box=None,
        pad_edge=False,
        show_edge=False,
        expand=True,
        header_style=token_style("MUTED", mode.color_system),
        padding=(0, 1),
    )
    table.add_column("seq", width=4, no_wrap=True)
    table.add_column("kind", width=kind_col, no_wrap=True, overflow="ellipsis")
    table.add_column("node", width=10, no_wrap=True, overflow="ellipsis")
    table.add_column("detail", ratio=1, overflow="fold")

    for i, step in enumerate(view.steps):
        state, token = _step_state(step)
        style = token_style(token, mode.color_system)
        glyph = state_style(state).glyph if mode.unicode else state_style(state).ascii_glyph
        arrow = "→" if mode.unicode else "->"
        detail = _evidence_summary(step).replace("→", arrow)
        highlight = step_index is not None and i == step_index
        row_style = "reverse" if highlight else ""
        table.add_row(
            Text(str(step.seq), style=style),
            Text(f"{glyph} {step.kind}", style=style),
            Text(step.node_id, style=token_style("SECONDARY", mode.color_system)),
            Text(detail, style=style),
            style=row_style,
        )

    blocks.append(panel(table, title="TIMELINE", mode=mode))

    if view.projection_errors:
        err_text = Text()
        for err in view.projection_errors:
            err_text.append(f"  {err}\n", style=token_style("ERROR", mode.color_system))
        blocks.append(panel(err_text, title="PROJECTION ERRORS", mode=mode))
    return Group(*blocks)
