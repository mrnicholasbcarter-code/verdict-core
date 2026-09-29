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
from rich.text import Text

from verdict.design import PresentationMode, panel, presentation_mode, token_style
from verdict.orchestration.context_view import ContextView, NodeContextView, SourceEntry
from verdict.security import redact_text

# ---------------------------------------------------------------------------
# Local presentation maps (design.py has no source-state / pressure styles yet)
# Documented as missing tokens in the lane result file.
# ---------------------------------------------------------------------------

PressureBand = Literal["ok", "elevated", "over", "unknown"]

_SOURCE_STYLE: dict[str, tuple[str, str, str, str]] = {
    # label, unicode glyph, ascii glyph  -> token
    # Data-layer state is "truncated"; AC language / UI label is "trimmed".
    "included": ("included", "●", "*", "SUCCESS"),
    "excluded": ("excluded", "○", "o", "MUTED"),
    "truncated": ("trimmed", "✂", "T", "WARNING"),
    "trimmed": ("trimmed", "✂", "T", "WARNING"),  # alias
    "deduplicated": ("deduplicated", "≡", "=", "INFO"),
    "compressed": ("compressed", "▽", "v", "PRIMARY"),
    "unknown": ("unknown", "?", "?", "UNKNOWN"),
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
    """Map observed budget_pressure to a presentation band. None -> unknown."""
    if pressure is None:
        return "unknown"
    if pressure > 1.0:
        return "over"
    if pressure >= 0.8:
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


def _bar(used: int | None, budget: int | None, width: int, *, unicode: bool) -> str:
    """ASCII/unicode meter from observed used/budget. Unknown -> explicit label."""
    width = max(4, min(width, 40))
    if used is None or budget is None or budget <= 0:
        return "[" + ("?" * width) + "]"
    ratio = max(0.0, min(used / budget, 1.0))
    filled = round(ratio * width)
    on = "█" if unicode else "#"
    off = "░" if unicode else "-"
    return "[" + (on * filled) + (off * (width - filled)) + "]"


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


def _node_header(node: NodeContextView, mode: PresentationMode) -> Text:
    band = pressure_band(node.budget_pressure)
    band_label, band_token = _PRESSURE_STYLE[band]
    used = format_bytes(node.prompt_bytes)
    budget = format_bytes(node.budget_bytes)
    press = format_pressure(node.budget_pressure)
    line = f"node {node.node_id}  used {used} / budget {budget}  pressure {press} ({band_label})"
    return Text(line, style=_style(band_token, mode))


def _source_line(src: SourceEntry, mode: PresentationMode, width: int) -> Text:
    st = _source_style(src.state)
    glyph = st.glyph if mode.unicode else st.ascii_glyph
    path = safe_path(src.path)
    kind = _source_kind(src.path)
    size = format_bytes(src.bytes)
    reason = ""
    if src.reason:
        reason = f"  reason={redact_text(src.reason)}"
    trunc = ""
    if src.truncated_at is not None:
        trunc = f"  truncated_at={src.truncated_at}"
    raw = f"  {glyph} {st.label:<13} {size:>10}  {kind:<7}  {path}{reason}{trunc}"
    if len(raw) > width:
        raw = raw[: max(0, width - 1)] + "…"
    return Text(raw, style=_style(st.token, mode))


def render_context(view: ContextView, mode: PresentationMode | None = None) -> RenderableType:
    """Rich renderable: aggregate budget bar + per-node rows + source drill-down."""
    mode = presentation_mode() if mode is None else mode
    width = max(40, mode.width)
    blocks: list[RenderableType] = []

    used, budget, pressure = _aggregate(view)
    band = pressure_band(pressure)
    band_label, band_token = _PRESSURE_STYLE[band]
    bar_width = min(30, max(8, width // 4))
    bar = _bar(used, budget, bar_width, unicode=mode.unicode)
    run = view.run_id or "unknown"
    header = (
        f"context budget  run={run}  "
        f"{format_bytes(used)} / {format_bytes(budget)}  "
        f"pressure {format_pressure(pressure)} ({band_label})  {bar}"
    )
    blocks.append(Text(header, style=_style(band_token, mode)))

    if not view.nodes:
        blocks.append(Text("no hydrate events recorded", style=_style("MUTED", mode)))
    else:
        for node in view.nodes:
            blocks.append(_node_header(node, mode))
            if node.sources is None:
                blocks.append(
                    Text("  sources: unknown (not recorded)", style=_style("MUTED", mode))
                )
                continue
            if not node.sources:
                blocks.append(Text("  sources: (none)", style=_style("MUTED", mode)))
                continue
            for src in node.sources:
                blocks.append(_source_line(src, mode, width))

    body = Group(*blocks)
    return panel(body, title="context", mode=mode, tone="BORDER", width=width)


def render_context_text(view: ContextView, width: int = 100) -> str:
    """Plain, NO_COLOR-safe text render. No ANSI, no required box-drawing."""
    width = max(40, width)
    used, budget, pressure = _aggregate(view)
    band = pressure_band(pressure)
    band_label, _ = _PRESSURE_STYLE[band]
    bar = _bar(used, budget, min(30, max(8, width // 4)), unicode=False)
    run = view.run_id or "unknown"
    lines: list[str] = [
        "context",
        (
            f"budget  run={run}  {format_bytes(used)} / {format_bytes(budget)}  "
            f"pressure {format_pressure(pressure)} ({band_label})  {bar}"
        ),
    ]
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
        if node.sources is None:
            lines.append("  sources: unknown (not recorded)")
            continue
        if not node.sources:
            lines.append("  sources: (none)")
            continue
        for src in node.sources:
            st = _source_style(src.state)
            glyph = st.ascii_glyph
            path = safe_path(src.path)
            kind = _source_kind(src.path)
            size = format_bytes(src.bytes)
            reason = f"  reason={redact_text(src.reason)}" if src.reason else ""
            trunc = f"  truncated_at={src.truncated_at}" if src.truncated_at is not None else ""
            row = f"  {glyph} {st.label:<13} {size:>10}  {kind:<7}  {path}{reason}{trunc}"
            if len(row) > width:
                row = row[: max(0, width - 1)] + "…"
            lines.append(row)
    return "\n".join(lines) + "\n"


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
