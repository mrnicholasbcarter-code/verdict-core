"""context_render.py — BOD-278 lane 2: context budget/provenance TUI + text render.

Consumes :class:`ContextView` only. Never recomputes selection, ranking, budget
or eligibility. Unknown metrics stay unknown. Secrets are redacted; paths under
HOME are abbreviated to ``~``. Content bytes are never shown.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any, Literal

from rich.console import Group, RenderableType
from rich.progress_bar import ProgressBar
from rich.table import Table
from rich.text import Text

from verdict.design import PresentationMode, panel, presentation_mode, token_style
from verdict.orchestration.context_view import ContextView, NodeContextView, SourceEntry
from verdict.security import redact_text

# ---------------------------------------------------------------------------
# Local presentation maps (design.py has no source-state / pressure styles yet)
# Glyphs are width-1 (rich.cells.cell_len == 1). Prefer design.py glyphs when
# a meaning matches; otherwise pick candidates listed in the lane result file.
# ---------------------------------------------------------------------------

PressureBand = Literal["ok", "elevated", "over", "unknown"]

# label, unicode glyph, ascii glyph, token — colour applies to state cell only
_SOURCE_STYLE: dict[str, tuple[str, str, str, str]] = {
    # Data-layer state is "truncated"; AC language / UI label is "trimmed".
    "included": ("included", "●", "*", "SUCCESS"),  # design GLYPHS["running"]
    "excluded": ("excluded", "○", "o", "MUTED"),  # design STATE_STYLES["admitted"]
    "truncated": ("trimmed", "▾", "T", "WARNING"),  # candidate for design.py
    "trimmed": ("trimmed", "▾", "T", "WARNING"),  # alias
    "deduplicated": ("deduplicated", "≡", "=", "INFO"),  # candidate for design.py
    "compressed": ("compressed", "◆", "v", "PRIMARY"),  # candidate for design.py
    "unknown": ("unknown", "?", "?", "UNKNOWN"),  # design STATE_STYLES["unknown"]
}

_PRESSURE_STYLE: dict[PressureBand, tuple[str, str]] = {
    "ok": ("ok", "SUCCESS"),
    "elevated": ("elevated", "WARNING"),
    "over": ("over", "ERROR"),
    "unknown": ("unknown", "UNKNOWN"),
}

_SECRETISH = re.compile(
    r"(?i)(sk-[A-Za-z0-9_-]{8,}|OMNIROUTE_API_KEY\s*=\s*\S+|api[_-]?key\s*[=:]\s*\S+)"
)

# Fixed column widths for plain text; source column absorbs the remainder.
_STATE_W = 15  # "▾ trimmed" / "* included"
_SIZE_W = 10
_KIND_W = 7


@dataclass(frozen=True)
class _SourceStyle:
    label: str
    glyph: str
    ascii_glyph: str
    token: str


def _source_style(state: str) -> _SourceStyle:
    label, glyph, ascii_glyph, token = _SOURCE_STYLE.get(state, _SOURCE_STYLE["unknown"])
    return _SourceStyle(label, glyph, ascii_glyph, token)


def pressure_band(pressure: float | None) -> PressureBand:
    """Map observed budget_pressure to a presentation band. None -> unknown.

    SUCCESS/ok < 70%, WARNING/elevated 70-100%, ERROR/over > 100%.
    """
    if pressure is None:
        return "unknown"
    if pressure > 1.0:
        return "over"
    if pressure >= 0.7:
        return "elevated"
    return "ok"


def abbreviate_home(path: str, home: str | None = None) -> str:
    """Replace a HOME prefix with ``~``. Never expands user input."""
    if not path:
        return path
    candidates: list[str] = []
    if home is not None:
        candidates.append(home)
    else:
        env_home = os.environ.get("HOME") or os.environ.get("USERPROFILE")
        if env_home:
            candidates.append(env_home)
        expanded = os.path.expanduser("~")
        if expanded and expanded != "~":
            candidates.append(expanded)
        # Common local-dev fallback observed in proof fixtures
        candidates.append("/home/nick")
    seen: set[str] = set()
    for home_path in candidates:
        if not home_path or home_path in seen:
            continue
        seen.add(home_path)
        if path == home_path:
            return "~"
        prefix = home_path.rstrip("/") + "/"
        if path.startswith(prefix):
            return "~/" + path[len(prefix) :]
    return path


def safe_path(path: str, *, home: str | None = None) -> str:
    """Redact secret-looking substrings then abbreviate HOME."""
    cleaned = redact_text(path)
    # Extra guard for bare sk-... / OMNIROUTE_API_KEY=... in a source *name*
    cleaned = _SECRETISH.sub("[redacted]", cleaned)
    return abbreviate_home(cleaned, home=home)


def format_bytes(value: int | None) -> str:
    """Human bytes; explicit ``unknown`` when not recorded."""
    if value is None:
        return "unknown"
    if value < 1000:
        return f"{value} B"
    if value < 1_000_000:
        return f"{value / 1000:.1f} KB"
    return f"{value / 1_000_000:.2f} MB"


def format_pressure(pressure: float | None) -> str:
    if pressure is None:
        return "unknown"
    return f"{pressure * 100:.0f}%"


def _bar_chars(
    used: int | None, budget: int | None, width: int, *, unicode: bool
) -> tuple[str, str]:
    """Return (filled, remainder) track characters. Unknown -> ('?'*width, '')."""
    width = max(4, min(width, 40))
    if used is None or budget is None or budget <= 0:
        return ("?" * width, "")
    ratio = max(0.0, min(used / budget, 1.0))
    filled = round(ratio * width)
    on = "█" if unicode else "#"
    off = "░" if unicode else "-"
    return (on * filled, off * (width - filled))


def _bar(used: int | None, budget: int | None, width: int, *, unicode: bool) -> str:
    """ASCII/unicode meter from observed used/budget. Unknown -> explicit label."""
    filled, rem = _bar_chars(used, budget, width, unicode=unicode)
    return "[" + filled + rem + "]"


def _aggregate(view: ContextView) -> tuple[int | None, int | None, float | None]:
    """Sum prompt/budget across nodes; pressure only when both totals known."""
    prompt_total = 0
    budget_total = 0
    any_prompt = False
    any_budget = False
    for node in view.nodes:
        if node.prompt_bytes is not None:
            prompt_total += node.prompt_bytes
            any_prompt = True
        if node.budget_bytes is not None:
            budget_total += node.budget_bytes
            any_budget = True
    used = prompt_total if any_prompt else None
    budget = budget_total if any_budget else None
    if used is None or budget is None or budget == 0:
        return used, budget, None
    return used, budget, round(used / budget, 6)


def _source_kind(path: str) -> str:
    """Cheap provenance kind from the recorded path string (no I/O)."""
    lower = path.lower()
    if lower.startswith("memory:") or "/memory/" in lower:
        return "memory"
    if lower.startswith("session:") or "prior-run" in lower:
        return "session"
    if lower.startswith("http://") or lower.startswith("https://"):
        return "url"
    if path.endswith((".py", ".md", ".ts", ".tsx", ".js", ".json", ".toml", ".yaml", ".yml")):
        return "file"
    if "/" in path or path.endswith(".txt"):
        return "file"
    return "source"


def _style(token: str, mode: PresentationMode) -> str:
    if not mode.color:
        return ""
    return token_style(token, mode.color_system)


def _state_cell_style(token: str, mode: PresentationMode) -> str:
    """Colour for glyph+label only; never bold (headers own bold)."""
    style = _style(token, mode)
    if style.startswith("bold "):
        return style[len("bold ") :]
    return style


def _source_detail(src: SourceEntry) -> str:
    parts: list[str] = []
    if src.reason:
        parts.append(f"reason={redact_text(src.reason)}")
    if src.truncated_at is not None:
        parts.append(f"truncated_at={src.truncated_at}")
    return "  ".join(parts)


def _ellipsis(text: str, max_width: int) -> str:
    """Hard-cap a string to ``max_width`` cells with a trailing ellipsis."""
    if max_width <= 0:
        return ""
    if len(text) <= max_width:
        return text
    if max_width == 1:
        return "…"
    return text[: max_width - 1] + "…"


def _inner_width(width: int) -> int:
    """Approximate panel content width (borders + horizontal padding)."""
    return max(40, width - 6)


def _budget_header_text(
    *,
    run: str,
    used: int | None,
    budget: int | None,
    pressure: float | None,
    width: int,
    unicode: bool,
    mode: PresentationMode | None = None,
) -> RenderableType:
    """Labelled budget line: TEXT metrics + a pressure-coloured bar sized at render time.

    The bar lives in an expanding grid column, so it takes whatever width the
    terminal actually gives the panel (never truncated, never past the border).
    Unknown used/budget renders the word 'unknown', not an empty or guessed bar.
    """
    band = pressure_band(pressure)
    band_label, band_token = _PRESSURE_STYLE[band]
    metrics = f"{format_bytes(used)} / {format_bytes(budget)} ({format_pressure(pressure)})"
    label = Text(no_wrap=True, overflow="ellipsis")
    label.append("budget  ", style=_style("SECONDARY", mode) if mode else "")
    label.append(f"run={run}  ", style=_style("TEXT", mode) if mode else "")
    label.append(metrics, style=_style("TEXT", mode) if mode else "")
    label.append(f"  {band_label}", style=_style("MUTED", mode) if mode else "")
    known = used is not None and budget is not None and budget > 0
    bar: RenderableType
    if not known:
        bar = Text("unknown", style=_style("MUTED", mode) if mode else "")
    elif mode is not None and mode.color and unicode:
        fill = _state_cell_style(band_token, mode)
        bar = ProgressBar(
            total=float(budget or 1),
            completed=float(min(used or 0, budget or 0)),
            width=None,
            style=_style("MUTED", mode),
            complete_style=fill,
            finished_style=fill,
        )
    else:
        filled, rem = _bar_chars(used, budget, 20, unicode=unicode)
        bar = Text("[" + filled + rem + "]", style=_style("MUTED", mode) if mode else "")
    grid = Table.grid(expand=True, padding=(0, 2))
    grid.add_column(no_wrap=True, overflow="ellipsis")
    grid.add_column(ratio=1, min_width=4)
    if width < label.cell_len + 12:
        # Narrow terminal: metrics on one row, full-width bar on the next.
        label.no_wrap = False
        label.overflow = "fold"
        grid = Table.grid(expand=True)
        grid.add_column(ratio=1)
        grid.add_row(label)
        grid.add_row(bar)
        return grid
    grid.add_row(label, bar)
    return grid


def _node_header(node: NodeContextView, mode: PresentationMode) -> Text:
    band = pressure_band(node.budget_pressure)
    band_label, band_token = _PRESSURE_STYLE[band]
    used = format_bytes(node.prompt_bytes)
    budget = format_bytes(node.budget_bytes)
    press = format_pressure(node.budget_pressure)
    # Bold TEXT for the node identity/metrics; only the pressure badge is coloured.
    out = Text()
    text_style = _style("TEXT", mode)
    header_style = f"bold {text_style}".strip() if text_style else "bold"
    out.append(
        f"node {node.node_id}  used {used} / budget {budget}  pressure {press} ", style=header_style
    )
    out.append(f"({band_label})", style=_state_cell_style(band_token, mode))
    return out


def _sources_table(sources: list[SourceEntry], mode: PresentationMode, width: int) -> Table:
    """Fixed-column source table; colour only the state glyph+label cell."""
    # Leading columns are fixed; source/detail share the rest via ratio so Rich
    # can shrink them under a tight console without dropping a column.
    table = Table(
        box=None,
        padding=(0, 1),
        pad_edge=False,
        show_header=False,
        show_edge=False,
        collapse_padding=True,
        expand=True,
        width=width,
    )
    table.add_column("state", width=_STATE_W, no_wrap=True, overflow="ellipsis")
    table.add_column("size", width=_SIZE_W, justify="right", no_wrap=True)
    table.add_column("kind", width=_KIND_W, no_wrap=True, style=_style("SECONDARY", mode))
    table.add_column(
        "source", ratio=3, no_wrap=True, overflow="ellipsis", style=_style("TEXT", mode)
    )
    table.add_column(
        "detail", ratio=2, no_wrap=True, overflow="ellipsis", style=_style("MUTED", mode)
    )

    for src in sources:
        st = _source_style(src.state)
        glyph = st.glyph if mode.unicode else st.ascii_glyph
        state_cell = Text(f"{glyph} {st.label}", style=_state_cell_style(st.token, mode))
        size_cell = Text(format_bytes(src.bytes), style=_style("SECONDARY", mode))
        kind_cell = Text(_source_kind(src.path))
        path_cell = Text(safe_path(src.path))
        detail_cell = Text(_source_detail(src))
        table.add_row(state_cell, size_cell, kind_cell, path_cell, detail_cell)
    return table


def render_context(view: ContextView, mode: PresentationMode | None = None) -> RenderableType:
    """Rich renderable: aggregate budget bar + per-node rows + source drill-down."""
    mode = presentation_mode() if mode is None else mode
    width = max(40, mode.width)
    inner = _inner_width(width)
    blocks: list[RenderableType] = []

    used, budget, pressure = _aggregate(view)
    run = view.run_id or "unknown"
    blocks.append(
        _budget_header_text(
            run=run,
            used=used,
            budget=budget,
            pressure=pressure,
            width=inner,
            unicode=mode.unicode,
            mode=mode,
        )
    )

    if not view.nodes:
        blocks.append(Text("no hydrate events recorded", style=_style("MUTED", mode)))
    else:
        for node in view.nodes:
            blocks.append(_node_header(node, mode))
            if node.compression is not None:
                blocks.append(
                    Text(f"  compression: {node.compression}", style=_style("MUTED", mode))
                )
            if node.sources is None:
                blocks.append(
                    Text("  sources: unknown (not recorded)", style=_style("MUTED", mode))
                )
                continue
            if not node.sources:
                blocks.append(Text("  sources: (none)", style=_style("MUTED", mode)))
                continue
            blocks.append(_sources_table(node.sources, mode, inner))

    body = Group(*blocks)
    return panel(body, title="context", mode=mode, tone="BORDER", width=width)


def _plain_source_line(src: SourceEntry, width: int) -> str:
    """Aligned plain-text source row; path ellipsized so the line fits ``width``."""
    st = _source_style(src.state)
    glyph = st.ascii_glyph
    state = f"{glyph} {st.label}"
    size = format_bytes(src.bytes)
    kind = _source_kind(src.path)
    path = safe_path(src.path)
    detail = _source_detail(src)

    # Layout: "  " + state + " " + size + "  " + kind + "  " + path + optional detail
    prefix = f"  {state:<{_STATE_W}} {size:>{_SIZE_W}}  {kind:<{_KIND_W}}  "
    detail_part = f"  {detail}" if detail else ""
    avail = width - len(prefix) - len(detail_part)
    # Prefer a readable path over detail when the row is tight.
    if avail < 12 and detail_part:
        detail_part = ""
        avail = width - len(prefix)
    path_part = _ellipsis(path, max(1, avail))
    line = prefix + path_part + detail_part
    if len(line) > width:
        line = _ellipsis(line, width)
    return line


def render_context_text(view: ContextView, width: int = 100) -> str:
    """Plain, NO_COLOR-safe text render. No ANSI, no required box-drawing."""
    width = max(40, width)
    used, budget, pressure = _aggregate(view)
    band = pressure_band(pressure)
    band_label, _ = _PRESSURE_STYLE[band]
    run = view.run_id or "unknown"
    metrics = f"{format_bytes(used)} / {format_bytes(budget)} ({format_pressure(pressure)})"
    prefix = f"budget  run={run}  {metrics}  {band_label}  "
    bar_w = min(30, max(4, width - len(prefix) - 2))
    bar = _bar(used, budget, bar_w, unicode=False)
    header = prefix + bar
    if len(header) > width:
        # Drop band label before truncating the bar
        prefix = f"budget  run={run}  {metrics}  "
        bar_w = min(30, max(4, width - len(prefix) - 2))
        header = prefix + _bar(used, budget, bar_w, unicode=False)
    lines: list[str] = ["context", header if len(header) <= width else _ellipsis(header, width)]
    if not view.nodes:
        lines.append("no hydrate events recorded")
        return "\n".join(lines) + "\n"

    for node in view.nodes:
        band_n = pressure_band(node.budget_pressure)
        band_label_n, _ = _PRESSURE_STYLE[band_n]
        lines.append(
            f"node {node.node_id}  used {format_bytes(node.prompt_bytes)} / "
            f"budget {format_bytes(node.budget_bytes)}  "
            f"pressure {format_pressure(node.budget_pressure)} ({band_label_n})"
        )
        if node.compression is not None:
            lines.append(f"  compression: {node.compression}")
        if node.sources is None:
            lines.append("  sources: unknown (not recorded)")
            continue
        if not node.sources:
            lines.append("  sources: (none)")
            continue
        for src in node.sources:
            lines.append(_plain_source_line(src, width))

    # Final hard cap: never emit a line longer than width
    capped = [_ellipsis(line, width) if len(line) > width else line for line in lines]
    return "\n".join(capped) + "\n"


def context_json(view: ContextView) -> dict[str, Any]:
    """Machine-readable non-secret projection for the CLI (post-#708)."""
    used, budget, pressure = _aggregate(view)
    nodes: list[dict[str, Any]] = []
    for node in view.nodes:
        sources_out: list[dict[str, Any]] | None
        if node.sources is None:
            sources_out = None
        else:
            sources_out = []
            for src in node.sources:
                sources_out.append(
                    {
                        "bytes": src.bytes,
                        "kind": _source_kind(src.path),
                        "path": safe_path(src.path),
                        "reason": redact_text(src.reason) if src.reason else None,
                        "state": _source_style(src.state).label,
                        "truncated_at": src.truncated_at,
                    }
                )
        nodes.append(
            {
                "budget_bytes": node.budget_bytes,
                "budget_pressure": node.budget_pressure,
                "compression": node.compression,
                "node_id": node.node_id,
                "pressure_band": pressure_band(node.budget_pressure),
                "prompt_bytes": node.prompt_bytes,
                "sources": sources_out,
                "totals_by_state": node.totals_by_state,
            }
        )
    return {
        "aggregate": {
            "budget_bytes": budget,
            "pressure": pressure,
            "pressure_band": pressure_band(pressure),
            "prompt_bytes": used,
        },
        "nodes": nodes,
        "run_id": view.run_id,
        "schema_version": view.schema_version,
    }


def context_view_from_run_view(run_view: Any) -> ContextView:
    """Best-effort projection from a live :class:`RunView` (no source list).

    Used when the cockpit only has the RunView summary. Sources stay unknown.
    """
    nodes: list[NodeContextView] = []
    for node_id, node in getattr(run_view, "nodes", {}).items():
        prompt = getattr(node, "prompt_bytes", None) or None
        # NodeView uses 0 as default; treat 0 budget with 0 prompt as unknown-ish
        budget_raw = getattr(node, "budget_bytes", 0) or 0
        budget = budget_raw if budget_raw > 0 else None
        prompt_val: int | None = int(prompt) if prompt else None
        if prompt_val == 0:
            prompt_val = None if budget is None else 0
        pressure = None
        if prompt_val is not None and budget is not None and budget > 0:
            pressure = round(prompt_val / budget, 6)
        nodes.append(
            NodeContextView(
                node_id=str(node_id),
                budget_bytes=budget,
                sources=None,
                prompt_bytes=prompt_val,
                totals_by_state=None,
                budget_pressure=pressure,
            )
        )
    run_id = None
    return ContextView(schema_version="1", run_id=run_id, nodes=nodes)


__all__ = [
    "abbreviate_home",
    "context_json",
    "context_view_from_run_view",
    "format_bytes",
    "format_pressure",
    "pressure_band",
    "render_context",
    "render_context_text",
    "safe_path",
]
