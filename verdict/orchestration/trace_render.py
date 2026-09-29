"""Trace timeline renderer — human-readable text and Rich renderable (BOD-279).

Consumes :class:`TraceView` only. Never reads events directly. Plain text
honours VERDICT_PLAIN, NO_COLOR, non-TTY, and reduced motion. JSON has no ANSI.
"""

from __future__ import annotations

from rich.console import Group, RenderableType
from rich.table import Table
from rich.text import Text

from verdict.design import PresentationMode, panel, presentation_mode, token_style
from verdict.orchestration.trace_view import STEP_KINDS, TraceStep, TraceView

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_KIND_GLYPHS: dict[str, str] = {
    "request": "▶",
    "classification": "🔍",
    "plan": "📋",
    "context": "📦",
    "routing": "🔀",
    "selection": "✓",
    "dispatch": "🚀",
    "terminal": "✔",
    "failure": "✖",
    "cooldown": "⏸",
    "reassign": "↻",
    "verify": "🔒",
    "barrier": "⏳",
    "integrate": "⊕",
    "review": "📝",
    "receipt": "📄",
    "run_finished": "■",
}

_KIND_TOKENS: dict[str, str] = {
    "request": "CYAN",
    "failure": "RED",
    "cooldown": "AMBER",
    "reassign": "AMBER",
    "verify": "SUCCESS",
    "terminal": "SUCCESS",
    "review": "PURPLE",
    "run_finished": "SUCCESS",
}


def _kind_style(kind: str, mode: PresentationMode) -> str:
    token = _KIND_TOKENS.get(kind, "SECONDARY")
    return token_style(token, mode.color_system)


def _evidence_summary(step: TraceStep) -> str:
    """One-line summary from evidence dict."""
    ev = step.evidence
    kind = step.kind
    parts: list[str] = []
    if kind == "request":
        goal = ev.get("goal", "")
        if goal:
            parts.append(f"goal={_trunc(goal, 50)}")
    elif kind == "selection":
        route = ev.get("selected_route") or ev.get("route_id", "")
        model = ev.get("model", "")
        if route:
            parts.append(f"route={route}")
        if model and model != route:
            parts.append(f"model={model}")
    elif kind == "failure":
        reason = ev.get("reason", "") or ev.get("failure_kind", "")
        if reason:
            parts.append(f"reason={_trunc(reason, 40)}")
    elif kind == "cooldown":
        route = ev.get("route_id", "") or ev.get("route", "")
        duration = ev.get("duration_s")
        if route:
            parts.append(f"route={route}")
        if duration is not None:
            parts.append(f"duration={duration}s")
    elif kind == "reassign":
        old = ev.get("old_route", "")
        new = ev.get("new_route", "") or ev.get("replacement_route", "")
        if old:
            parts.append(f"from={old}")
        if new:
            parts.append(f"to={new}")
    elif kind == "terminal":
        route = ev.get("executed_model", "") or ev.get("route_id", "")
        if route:
            parts.append(f"model={route}")
    elif kind == "verify":
        ok = ev.get("passed", ev.get("valid"))
        if ok is not None:
            parts.append(f"passed={ok}")
    elif kind == "review":
        outcome = ev.get("outcome", "") or ev.get("verdict", "")
        if outcome:
            parts.append(f"outcome={outcome}")
    elif kind == "run_finished":
        outcome = ev.get("outcome", "")
        if outcome:
            parts.append(f"outcome={outcome}")
    return "  ".join(parts)


def _trunc(s: str, n: int) -> str:
    s = str(s).replace("\n", " ").strip()
    return s[:n] + "…" if len(s) > n else s


# ---------------------------------------------------------------------------
# Text renderer
# ---------------------------------------------------------------------------


def render_trace_text(view: TraceView, width: int = 100) -> str:
    """Plain, NO_COLOR-safe text. No ANSI, no required box-drawing."""
    width = max(40, int(width))
    lines: list[str] = []
    lines.append(f"trace  run={view.run_id or 'unknown'}")
    if view.goal:
        lines.append(f"  goal: {_trunc(view.goal, width - 8)}")
    lines.append(f"  steps: {len(view.steps)}  schema: {view.schema_version}")
    lines.append("")

    # Column widths
    seq_w = 5
    kind_w = max(len(k) for k in STEP_KINDS) + 1
    node_w = min(20, max((len(s.node_id) for s in view.steps), default=4))
    detail_w = max(10, width - seq_w - kind_w - node_w - 6)  # 6 for separators

    lines.append(f"{'seq':<{seq_w}} {'kind':<{kind_w}} {'node':<{node_w}} detail")
    lines.append("-" * min(width, seq_w + kind_w + node_w + detail_w + 6))

    for step in view.steps:
        summary = _evidence_summary(step)
        node = step.node_id[:node_w] if step.node_id else ""
        line = f"{step.seq:<{seq_w}} {step.kind:<{kind_w}} {node:<{node_w}} {summary}"
        if len(line) > width:
            line = line[: width - 1] + "…"
        lines.append(line)

    lines.append("")

    # Projection errors
    if view.projection_errors:
        lines.append("projection errors:")
        for err in view.projection_errors:
            lines.append(f"  {err}")
        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Rich renderable
# ---------------------------------------------------------------------------


def render_trace(
    view: TraceView, mode: PresentationMode | None = None, *, step_index: int | None = None
) -> RenderableType:
    """Rich renderable of a :class:`TraceView`."""
    mode = presentation_mode() if mode is None else mode
    blocks: list[RenderableType] = []

    header = Text.assemble(
        ("trace", token_style("PRIMARY", mode.color_system)),
        (f"  run={view.run_id or 'unknown'}", token_style("SECONDARY", mode.color_system)),
    )
    if view.goal:
        header.append(
            f"  goal={_trunc(view.goal, 50)}", style=token_style("MUTED", mode.color_system)
        )
    blocks.append(header)
    blocks.append(
        Text(
            f"steps: {len(view.steps)}  schema: {view.schema_version}",
            style=token_style("MUTED", mode.color_system),
        )
    )

    table = Table(
        box=None,
        pad_edge=False,
        show_edge=False,
        header_style=token_style("MUTED", mode.color_system),
    )
    table.add_column("seq", width=5)
    table.add_column("kind", width=14)
    table.add_column("node", width=16)
    table.add_column("detail")

    for i, step in enumerate(view.steps):
        style = _kind_style(step.kind, mode)
        glyph = _KIND_GLYPHS.get(step.kind, " ")
        highlight = step_index is not None and i == step_index
        row_style = "reverse" if highlight else ""
        table.add_row(
            Text(str(step.seq)),
            Text(f"{glyph} {step.kind}", style=style),
            Text(
                step.node_id[:16] if step.node_id else "",
                style=token_style("SECONDARY", mode.color_system),
            ),
            Text(_evidence_summary(step)),
            style=row_style,
        )

    blocks.append(panel(table, title="TIMELINE", mode=mode, width=min(mode.width, 96)))

    if view.projection_errors:
        err_text = Text()
        for err in view.projection_errors:
            err_text.append(f"  {err}\n", style=token_style("ERROR", mode.color_system))
        blocks.append(
            panel(err_text, title="PROJECTION ERRORS", mode=mode, width=min(mode.width, 96))
        )

    return Group(*blocks)
