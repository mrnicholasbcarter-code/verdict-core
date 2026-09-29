"""Demo claims renderer — CLAIMS panel for offline/live demo runs (BOD-279).

Consumes :func:`derive_claims` output only. Never hard-codes a claim. Plain
text honours VERDICT_PLAIN, NO_COLOR, non-TTY, and reduced motion.
"""

from __future__ import annotations

from rich.console import Group, RenderableType
from rich.table import Table
from rich.text import Text

from verdict.design import PresentationMode, panel, presentation_mode, token_style
from verdict.orchestration.claims import (
    CLAIM_STATUS_CONTRADICTED,
    CLAIM_STATUS_NOT_OBSERVED,
    CLAIM_STATUS_VERIFIED,
    Claim,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_STATUS_GLYPHS: dict[str, str] = {
    CLAIM_STATUS_VERIFIED: "✓",
    CLAIM_STATUS_NOT_OBSERVED: "—",
    CLAIM_STATUS_CONTRADICTED: "✖",
}

_STATUS_TOKENS: dict[str, str] = {
    CLAIM_STATUS_VERIFIED: "SUCCESS",
    CLAIM_STATUS_NOT_OBSERVED: "MUTED",
    CLAIM_STATUS_CONTRADICTED: "RED",
}


def _evidence_refs(claim: Claim) -> str:
    """Compact evidence reference string."""
    return ", ".join(e.source for e in claim.evidence) if claim.evidence else ""


# ---------------------------------------------------------------------------
# Text renderer
# ---------------------------------------------------------------------------


def render_claims_text(claims: list[Claim], *, width: int = 100, label: str = "") -> str:
    """Plain, NO_COLOR-safe claims panel. No ANSI."""
    width = max(40, int(width))
    lines: list[str] = []
    if label:
        lines.append(label)
        lines.append("")

    verified = [c for c in claims if c.status == CLAIM_STATUS_VERIFIED]
    not_observed = [c for c in claims if c.status == CLAIM_STATUS_NOT_OBSERVED]
    contradicted = [c for c in claims if c.status == CLAIM_STATUS_CONTRADICTED]

    lines.append("CLAIMS VERIFIED")
    lines.append("-" * min(width, 60))
    if verified:
        for c in verified:
            refs = _evidence_refs(c)
            line = f"  [VERIFIED] {c.text}"
            if refs:
                line += f"  ({refs})"
            if len(line) > width:
                line = line[: width - 1] + "…"
            lines.append(line)
    else:
        lines.append("  (none)")
    lines.append("")

    if not_observed:
        lines.append("NOT SHOWN BY THIS RUN")
        lines.append("-" * min(width, 60))
        for c in not_observed:
            line = f"  [NOT OBSERVED] {c.text}"
            if len(line) > width:
                line = line[: width - 1] + "…"
            lines.append(line)
        lines.append("")

    if contradicted:
        lines.append("CONTRADICTED")
        lines.append("-" * min(width, 60))
        for c in contradicted:
            refs = _evidence_refs(c)
            line = f"  [CONTRADICTED] {c.text}"
            if refs:
                line += f"  ({refs})"
            if len(line) > width:
                line = line[: width - 1] + "…"
            lines.append(line)
        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Rich renderable
# ---------------------------------------------------------------------------


def render_claims(
    claims: list[Claim], mode: PresentationMode | None = None, *, label: str = ""
) -> RenderableType:
    """Rich renderable of claims from derive_claims."""
    mode = presentation_mode() if mode is None else mode
    blocks: list[RenderableType] = []

    if label:
        blocks.append(Text(label, style=token_style("AMBER", mode.color_system)))

    verified = [c for c in claims if c.status == CLAIM_STATUS_VERIFIED]
    not_observed = [c for c in claims if c.status == CLAIM_STATUS_NOT_OBSERVED]
    contradicted = [c for c in claims if c.status == CLAIM_STATUS_CONTRADICTED]

    # Verified table
    v_table = Table(box=None, pad_edge=False, show_edge=False)
    v_table.add_column("", width=3)
    v_table.add_column("claim")
    v_table.add_column("evidence", style=token_style("MUTED", mode.color_system))
    if verified:
        for c in verified:
            v_table.add_row(
                Text(
                    _STATUS_GLYPHS[CLAIM_STATUS_VERIFIED],
                    style=token_style("SUCCESS", mode.color_system),
                ),
                Text(c.text),
                Text(_evidence_refs(c)),
            )
    else:
        v_table.add_row(
            Text(""), Text("(none)", style=token_style("MUTED", mode.color_system)), Text("")
        )

    blocks.append(panel(v_table, title="CLAIMS VERIFIED", mode=mode, width=min(mode.width, 96)))

    # Not observed
    if not_observed:
        no_table = Table(box=None, pad_edge=False, show_edge=False)
        no_table.add_column("", width=3)
        no_table.add_column("claim")
        for c in not_observed:
            no_table.add_row(
                Text(
                    _STATUS_GLYPHS[CLAIM_STATUS_NOT_OBSERVED],
                    style=token_style("MUTED", mode.color_system),
                ),
                Text(c.text, style=token_style("MUTED", mode.color_system)),
            )
        blocks.append(
            panel(no_table, title="NOT SHOWN BY THIS RUN", mode=mode, width=min(mode.width, 96))
        )

    # Contradicted
    if contradicted:
        cx_table = Table(box=None, pad_edge=False, show_edge=False)
        cx_table.add_column("", width=3)
        cx_table.add_column("claim")
        cx_table.add_column("evidence", style=token_style("MUTED", mode.color_system))
        for c in contradicted:
            cx_table.add_row(
                Text(
                    _STATUS_GLYPHS[CLAIM_STATUS_CONTRADICTED],
                    style=token_style("RED", mode.color_system),
                ),
                Text(c.text, style=token_style("RED", mode.color_system)),
                Text(_evidence_refs(c)),
            )
        blocks.append(panel(cx_table, title="CONTRADICTED", mode=mode, width=min(mode.width, 96)))

    return Group(*blocks)
