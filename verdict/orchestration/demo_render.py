"""Demo claims renderer — CLAIMS panel for offline/live demo runs (BOD-279).

Consumes :func:`derive_claims` output only. Never hard-codes a claim. Plain
text honours VERDICT_PLAIN, NO_COLOR, non-TTY, and reduced motion.
"""

from __future__ import annotations

from rich.console import Group, RenderableType
from rich.table import Table
from rich.text import Text

from verdict.design import PresentationMode, panel, presentation_mode, state_style, token_style
from verdict.orchestration.claims import (
    CLAIM_STATUS_CONTRADICTED,
    CLAIM_STATUS_NOT_OBSERVED,
    CLAIM_STATUS_VERIFIED,
    Claim,
)

# State names already styled in design.py. Contradicted has no state of its own;
# ERROR is the failed-state token.
_STATUS_STATE: dict[str, str] = {
    CLAIM_STATUS_VERIFIED: "validated",
    CLAIM_STATUS_NOT_OBSERVED: "unknown",
    CLAIM_STATUS_CONTRADICTED: "failed",
}

_STATUS_LABEL: dict[str, str] = {
    CLAIM_STATUS_VERIFIED: "VERIFIED",
    CLAIM_STATUS_NOT_OBSERVED: "NOT SHOWN",
    CLAIM_STATUS_CONTRADICTED: "CONTRADICTED",
}


def _status_glyph(status: str, *, unicode: bool) -> str:
    style = state_style(_STATUS_STATE[status])
    return style.glyph if unicode else style.ascii_glyph


def _status_token(status: str) -> str:
    if status == CLAIM_STATUS_NOT_OBSERVED:
        return "MUTED"
    return state_style(_STATUS_STATE[status]).token


def _evidence_refs(claim: Claim) -> str:
    """Compact evidence reference string, with duplicate sources removed."""
    seen: dict[str, None] = {}
    for evidence in claim.evidence:
        seen.setdefault(evidence.source, None)
    return ", ".join(seen)


def _fit(line: str, width: int) -> str:
    if len(line) <= width:
        return line
    return line[: width - 1] + "…"


def render_claims_text(claims: list[Claim], *, width: int = 100, label: str = "") -> str:
    """Plain, NO_COLOR-safe claims panel. No ANSI."""
    width = max(40, int(width))
    lines: list[str] = []
    if label:
        lines.append(_fit(label, width))
        lines.append("")

    groups = (
        ("CLAIMS VERIFIED", [c for c in claims if c.status == CLAIM_STATUS_VERIFIED], True),
        (
            "NOT SHOWN BY THIS RUN",
            [c for c in claims if c.status == CLAIM_STATUS_NOT_OBSERVED],
            False,
        ),
        ("CONTRADICTED", [c for c in claims if c.status == CLAIM_STATUS_CONTRADICTED], True),
    )
    for title, group, show_refs in groups:
        if title != "CLAIMS VERIFIED" and not group:
            continue
        lines.append(title)
        lines.append("-" * min(width, 60))
        if not group:
            lines.append("  (none)")
        for claim in group:
            glyph = _status_glyph(claim.status, unicode=False)
            refs = _evidence_refs(claim) if show_refs else ""
            line = f"  {glyph} {_STATUS_LABEL[claim.status]}  {claim.text}"
            if refs:
                line += f"  ({refs})"
            lines.append(_fit(line, width))
        lines.append("")
    return "\n".join(lines)


def _claims_table(claims: list[Claim], mode: PresentationMode, *, show_evidence: bool) -> Table:
    table = Table(box=None, pad_edge=False, show_edge=False, expand=True)
    table.add_column("", width=2, no_wrap=True)
    table.add_column("claim", ratio=3, overflow="fold")
    if show_evidence:
        table.add_column("evidence", ratio=2, overflow="fold")
    if not claims:
        muted = token_style("MUTED", mode.color_system)
        row: list[Text] = [Text(""), Text("(none)", style=muted)]
        if show_evidence:
            row.append(Text(""))
        table.add_row(*row)
        return table
    for claim in claims:
        token = _status_token(claim.status)
        style = token_style(token, mode.color_system)
        glyph = Text(_status_glyph(claim.status, unicode=mode.unicode), style=style)
        text = Text(claim.text, style=style if token != "SUCCESS" else "")
        row = [glyph, text]
        if show_evidence:
            row.append(
                Text(_evidence_refs(claim), style=token_style("SECONDARY", mode.color_system))
            )
        table.add_row(*row)
    return table


def render_claims(
    claims: list[Claim], mode: PresentationMode | None = None, *, label: str = ""
) -> RenderableType:
    """Rich renderable of claims from derive_claims.

    Panels expand to the console width. A fixed panel width plus an expanding
    table misaligns the right border.
    """
    mode = presentation_mode() if mode is None else mode
    blocks: list[RenderableType] = []
    if label:
        label_text = Text(label, style=token_style("WARNING", mode.color_system))
        label_text.truncate(mode.width, overflow="ellipsis")
        blocks.append(label_text)

    sections = (
        ("CLAIMS VERIFIED", [c for c in claims if c.status == CLAIM_STATUS_VERIFIED], True),
        (
            "NOT SHOWN BY THIS RUN",
            [c for c in claims if c.status == CLAIM_STATUS_NOT_OBSERVED],
            False,
        ),
        ("CONTRADICTED", [c for c in claims if c.status == CLAIM_STATUS_CONTRADICTED], True),
    )
    for title, group, show_evidence in sections:
        if title != "CLAIMS VERIFIED" and not group:
            continue
        blocks.append(
            panel(_claims_table(group, mode, show_evidence=show_evidence), title=title, mode=mode)
        )
    return Group(*blocks)
