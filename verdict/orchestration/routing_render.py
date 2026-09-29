"""Routing explorer TUI + text renderer (BOD-277).

Consumes :class:`RoutingView` only. Never recomputes selection, ranking,
budget, or eligibility. Unknown values stay ``unknown``. Secrets are
redacted. Large inventories are filtered and paged in bounded time.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Any, Literal

from rich.console import Group, RenderableType
from rich.table import Table
from rich.text import Text

from verdict.contracts import redact_contract_secrets
from verdict.design import PresentationMode, panel, presentation_mode, render_state, token_style
from verdict.motion import MotionClock, pulse
from verdict.orchestration.routing_view import CandidateRecord, EligibilityEvaluation, RoutingView
from verdict.security import redact_text

_SECRETISH = re.compile(
    r"(?i)(sk-[A-Za-z0-9_-]{8,}|bearer\s+[A-Za-z0-9._\-]+|"
    r"OMNIROUTE_API_KEY\s*=\s*\S+|api[_-]?key\s*[=:]\s*\S+)"
)

FUNNEL_STAGES: tuple[str, ...] = (
    "DISCOVERED",
    "ENTITLED",
    "HEALTHY",
    "AVAILABLE",
    "TASK_ELIGIBLE",
    "SELECTED",
)

CandidateState = Literal[
    "selected", "admitted", "confirmed", "visible", "cooldown", "rejected", "unknown"
]

# Presentation map for explorer candidate states. ``design.py`` already
# covers selected/admitted/cooldown/rejected/unknown. ``visible`` and
# ``confirmed`` are explorer-only; they reuse MUTED / SUCCESS tokens.
_STATE_TOKEN: dict[str, str] = {
    "selected": "running",  # design has no "selected"; reuse running glyph + SELECTED label
    "admitted": "admitted",
    "confirmed": "validated",  # glyph + VALIDATED label; distinct from admitted
    "visible": "planned",  # inventory-visible, not yet admitted
    "cooldown": "cooldown",
    "rejected": "rejected",
    "unknown": "unknown",
}

_DEFAULT_PAGE_SIZE = 25
_MAX_PAGE_SIZE = 200


def _style(token: str, mode: PresentationMode) -> str:
    if not mode.color:
        return ""
    return token_style(token, mode.color_system)


def _safe_text(value: object) -> str:
    """Redact labeled credentials and bare sk-/bearer tokens."""
    cleaned = redact_text(value)
    return _SECRETISH.sub("[redacted]", cleaned)


def _fmt(value: Any) -> str:
    """Render a recorded value; empty / None stay explicitly unknown."""
    if value is None or value == "":
        return "unknown"
    if isinstance(value, bool):
        return "yes" if value else "no"
    return _safe_text(str(value))


def _wrap_plain(text: str, width: int) -> list[str]:
    """Wrap a prose line on spaces. A single token longer than width is cut.

    A trailing word shorter than 8 characters stays on the previous line,
    even if that line runs a few columns past width. An orphan word is worse.
    """
    if len(text) <= width:
        return [text]
    out: list[str] = []
    rest = text
    while rest:
        if len(rest) <= width:
            out.append(rest)
            break
        cut = rest.rfind(" ", 0, width + 1)
        if cut <= 0:
            out.append(rest[: width - 1] + "\u2026")
            rest = rest[width - 1 :].lstrip()
            continue
        nxt = rest[cut + 1 :]
        if " " not in nxt and len(nxt) < 8:
            out.append(rest)
            break
        out.append(rest[:cut].rstrip())
        rest = nxt.lstrip()
    return out or [""]


def _fmt_remaining(remaining: float | None) -> str:
    if remaining is None:
        return "unknown"
    if remaining <= 0:
        return "elapsed"
    return f"{int(remaining)}s"


def _fmt_rank(rank: int | None) -> str:
    """Missing rank is an em dash, not the word unknown (that word is noise)."""
    return "\u2014" if rank is None else str(rank)


def _fmt_components(components: Mapping[str, Any] | None) -> str:
    if components is None:
        return "unknown"
    if not components:
        return "(none)"
    parts: list[str] = []
    for key, raw in components.items():
        parts.append(f"{key}={_fmt(raw)}")
    return " ".join(parts)


def candidate_state(
    candidate: CandidateRecord, evaluation: EligibilityEvaluation
) -> CandidateState:
    """Map recorded evidence onto a distinct explorer state.

    Rules (first match wins; never inferred beyond recorded fields):
    * ``cooldown`` when ``cooldown_until`` is set.
    * ``selected`` when this route is the evaluation's ``selected_route``.
    * ``rejected`` when a ``failed_stage`` or ``rejection_reason`` is recorded
      (and the route is not the selected winner).
    * ``confirmed`` when the terminal ``observed_route`` matches this route.
    * ``admitted`` when the route reached TASK_ELIGIBLE or SELECTED.
    * ``visible`` when the route was recorded at all (DISCOVERED+).
    * ``unknown`` when the candidate record has no funnel position.
    """
    if candidate.cooldown_until:
        return "cooldown"
    if evaluation.selected_route and candidate.route_id == evaluation.selected_route:
        return "selected"
    if candidate.failed_stage or (
        candidate.rejection_reason and candidate.rejection_reason not in {"", "selected"}
    ):
        return "rejected"
    if evaluation.observed_route and candidate.route_id == evaluation.observed_route:
        return "confirmed"
    reached = candidate.reached or ""
    if reached in {"TASK_ELIGIBLE", "SELECTED"}:
        return "admitted"
    if reached:
        return "visible"
    return "unknown"


def first_rejection(candidate: CandidateRecord) -> tuple[str, str]:
    """Authoritative first rejection: (stage, reason). Unknown stays unknown."""
    stage = candidate.failed_stage or "unknown"
    reason = candidate.rejection_reason or "unknown"
    if reason in {"", "selected"} and candidate.failed_stage is None:
        return "unknown", "unknown"
    return stage, reason if reason else "unknown"


def filter_candidates(
    candidates: Sequence[CandidateRecord],
    evaluation: EligibilityEvaluation,
    *,
    state: str | None = None,
    provider: str | None = None,
    text: str | None = None,
) -> list[CandidateRecord]:
    """Pure filter. No I/O, no ranking, no probing."""
    needle = (text or "").strip().lower()
    want_state = (state or "").strip().lower() or None
    want_provider = (provider or "").strip().lower() or None
    out: list[CandidateRecord] = []
    for cand in candidates:
        if want_provider and cand.provider.lower() != want_provider:
            continue
        if want_state and candidate_state(cand, evaluation) != want_state:
            continue
        if needle:
            hay = " ".join(
                (
                    cand.route_id,
                    cand.provider,
                    cand.reached or "",
                    cand.failed_stage or "",
                    cand.rejection_reason or "",
                    cand.capacity_class or "",
                    cand.plan_label or "",
                )
            ).lower()
            if needle not in hay:
                continue
        out.append(cand)
    return out


def paginate(
    rows: Sequence[CandidateRecord], *, page: int = 0, page_size: int = _DEFAULT_PAGE_SIZE
) -> tuple[list[CandidateRecord], int, int]:
    """Return (page_rows, page_index, page_count). ``page_size`` is capped."""
    size = max(1, min(int(page_size), _MAX_PAGE_SIZE))
    total = len(rows)
    pages = max(1, (total + size - 1) // size) if total else 1
    index = max(0, min(int(page), pages - 1))
    start = index * size
    return list(rows[start : start + size]), index, pages


def _recorded_count(evaluation: EligibilityEvaluation) -> int | None:
    if evaluation.candidates is None:
        return None
    return len(evaluation.candidates)


def _funnel_admitted(evaluation: EligibilityEvaluation) -> int:
    """Routes the funnel says reached TASK_ELIGIBLE (includes the selected winner)."""
    return evaluation.funnel.get("TASK_ELIGIBLE", 0)


def _table_state_counts(evaluation: EligibilityEvaluation) -> dict[str, int]:
    counts: dict[str, int] = {}
    for cand in evaluation.candidates or ():
        state = candidate_state(cand, evaluation)
        counts[state] = counts.get(state, 0) + 1
    return counts


def _headline(evaluation: EligibilityEvaluation) -> str:
    """Counts that match the table, or say exactly why they do not.

    Funnel DISCOVERED is the evaluated population. The table lists only
    recorded candidate rows. ``candidates_omitted`` are counted, not listed.
    Admitted is the funnel TASK_ELIGIBLE count (selected is inside that stage),
    not the number of rows whose explorer state is ADMITTED.
    """
    discovered = evaluation.funnel.get("DISCOVERED", 0)
    admitted = _funnel_admitted(evaluation)
    selected = evaluation.funnel.get("SELECTED", 0)
    recorded = _recorded_count(evaluation)
    omitted = evaluation.candidates_omitted
    core = f"{discovered} candidates, {admitted} admitted, {selected} selected"
    if recorded is None:
        return core
    if omitted:
        return f"{core}; {recorded} recorded, {omitted} counted only"
    if recorded != discovered:
        return f"{core}; {recorded} recorded with details"
    return core


def _funnel_line(evaluation: EligibilityEvaluation) -> str:
    parts: list[str] = []
    for stage in FUNNEL_STAGES:
        count = evaluation.funnel.get(stage)
        parts.append(f"{stage} {_fmt(count) if count is None else count}")
    return " > ".join(parts)


def _funnel_renderable(evaluation: EligibilityEvaluation, mode: PresentationMode) -> Text:
    """One secondary line. Only the SELECTED stage count takes the primary colour."""
    text = Text()
    for index, stage in enumerate(FUNNEL_STAGES):
        if index:
            text.append(" > ", style=_style("SECONDARY", mode))
        count = evaluation.funnel.get(stage)
        shown = _fmt(count) if count is None else str(count)
        text.append(f"{stage} ", style=_style("SECONDARY", mode))
        if stage == "SELECTED":
            text.append(shown, style=_style("PRIMARY", mode))
        else:
            text.append(shown, style=_style("SECONDARY", mode))
    return text


def _because_renderable(line: str, mode: PresentationMode) -> Text:
    """PRIMARY label, TEXT body. The full reason lives here, not in the table."""
    prefix = "selected because:"
    if line.startswith(prefix):
        return Text.assemble(
            (prefix, _style("PRIMARY", mode)), (line[len(prefix) :], _style("TEXT", mode))
        )
    return Text(line, style=_style("TEXT", mode))


def _freshness(evaluation: EligibilityEvaluation, generated_at: str) -> str:
    """Show recorded timestamps. Never fabricates a freshness probe."""
    at = evaluation.at or "unknown"
    gen = generated_at or "unknown"
    return f"evaluated_at={at}  view_generated_at={gen}"


def _capacity_source(candidate: CandidateRecord) -> str:
    """Capacity class + evidence signal; unknown stays unknown."""
    cls = _fmt(candidate.capacity_class)
    ev = _safe_text(candidate.capacity_evidence) if candidate.capacity_evidence else ""
    if ev:
        return f"{cls} ({ev})"
    return cls


def _pool_label(candidate: CandidateRecord) -> str:
    """Backend pool id; empty stays empty."""
    return _safe_text(candidate.pool) if candidate.pool else ""


def _cooldown_scope_label(candidate: CandidateRecord) -> str:
    """Cooldown scope key; empty = no recorded cooldown scope."""
    return _safe_text(candidate.cooldown_scope) if candidate.cooldown_scope else ""


def _selected_because_lines(evaluation: EligibilityEvaluation) -> list[str]:
    if not evaluation.selected_because:
        if evaluation.selected_route:
            return ["selected because: unknown (no rank components recorded)"]
        return ["selected because: (no selection)"]
    return [f"selected because: {_safe_text(item)}" for item in evaluation.selected_because]


def _omitted_summary_line(evaluation: EligibilityEvaluation) -> str:
    """AC7: human-readable summary of omitted candidate states."""
    omitted = evaluation.candidates_omitted
    if omitted <= 0:
        return ""
    summary = evaluation.omitted_summary
    if not summary:
        return f"  + {omitted} more not shown"
    parts = []
    for state in sorted(summary):
        if state == "_total":
            continue
        entry = summary[state]
        if not isinstance(entry, dict):
            continue
        count = entry.get("count", 0)
        reason = _safe_text(str(entry.get("first_reason") or ""))
        if reason:
            parts.append(f"{count} {state} ({reason})")
        else:
            parts.append(f"{count} {state}")
    if not parts:
        return f"  + {omitted} more not shown"
    return "  + " + ", ".join(parts) + f"  (total omitted: {omitted})"


def _cooldown_rows(evaluation: EligibilityEvaluation) -> list[CandidateRecord]:
    if evaluation.candidates is None:
        return []
    return [c for c in evaluation.candidates if c.cooldown_until]


def routing_view_from_run_view(run_view: Any, events: Sequence[Any] | None = None) -> RoutingView:
    """Project a live cockpit ``RunView`` into a :class:`RoutingView`.

    Prefers the recorded event stream (authoritative). Falls back to the
    RunView summary when events are unavailable; candidate rows then stay
    unknown rather than being invented.
    """
    from verdict.orchestration.routing_view import routing_view as _build

    if events:
        payload: list[Mapping[str, Any]] = []
        for ev in events:
            if isinstance(ev, Mapping):
                payload.append(dict(ev))
            elif hasattr(ev, "to_dict"):
                payload.append(ev.to_dict())
            else:
                payload.append(
                    {
                        "seq": getattr(ev, "seq", 0),
                        "at": getattr(ev, "at", ""),
                        "type": getattr(ev, "type", ""),
                        "node_id": getattr(ev, "node_id", ""),
                        "data": dict(getattr(ev, "data", {}) or {}),
                    }
                )
        return _build(payload)

    # Summary-only fallback: funnel counts from RunView.eligibility, no candidates.
    from verdict.orchestration.routing_view import SCHEMA_VERSION
    from verdict.orchestration.routing_view import EligibilityEvaluation as _Eval

    raw = getattr(run_view, "eligibility", {}) or {}
    funnel = {stage: 0 for stage in FUNNEL_STAGES}
    mapping = {
        "discovered": "DISCOVERED",
        "entitled": "ENTITLED",
        "healthy": "HEALTHY",
        "available": "AVAILABLE",
        "eligible": "TASK_ELIGIBLE",
        "selected": "SELECTED",
    }
    for key, stage in mapping.items():
        value = raw.get(key)
        if isinstance(value, int):
            funnel[stage] = value
    selected_route = None
    nodes = getattr(run_view, "nodes", {}) or {}
    # Do not pick a "best" node. Surface the currently selected cockpit node
    # if the caller stashed it; otherwise leave selected_route unknown.
    evaluation = _Eval(
        node_id="",
        seq=int(getattr(run_view, "last_seq", 0) or 0),
        at="",
        funnel=funnel,
        rejections={},
        selected_route=selected_route,
        candidates=None,
        candidates_omitted=0,
        selected_because=[],
        observed_route=None,
        session_ref=None,
        selected_observed_mismatch=False,
    )
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    return RoutingView(
        schema_version=SCHEMA_VERSION,
        source="recorded",
        generated_at=now,
        evaluations=[evaluation] if any(funnel.values()) or nodes else [],
    )


def _state_renderable(
    state: CandidateState,
    mode: PresentationMode,
    *,
    remaining_seconds: float | None = None,
    phase: float = 0.0,
) -> Text:
    """Glyph + label only. Remaining time stays in the cooldowns section."""
    _ = remaining_seconds
    design_state = _STATE_TOKEN[state]
    rendered = render_state(design_state, mode=mode, show_remaining=False)
    # Explorer states that design.py labels differently keep an explicit alias
    # so "visible" / "confirmed" stay distinct in the table.
    if state == "visible":
        glyph = rendered.plain.split(" ", 1)[0] if rendered.plain else "?"
        label = f"{glyph} VISIBLE"
        return Text(label, style=_style("MUTED", mode))
    if state == "confirmed":
        glyph = rendered.plain.split(" ", 1)[0] if rendered.plain else "+"
        label = f"{glyph} CONFIRMED"
        return Text(label, style=_style("SUCCESS", mode))
    if state == "selected":
        glyph = rendered.plain.split(" ", 1)[0] if rendered.plain else "*"
        label = f"{glyph} SELECTED"
        return Text(label, style=_style("PRIMARY", mode))
    # Pulse the activity light only when motion is on AND the mapped design
    # state is observed-running. Candidate explorer states are not running.
    _ = pulse(phase, mode=mode, state=design_state)
    return rendered


def _plain_state(state: CandidateState, *, unicode: bool) -> str:
    from verdict.design import state_style

    design_state = _STATE_TOKEN[state]
    style = state_style(design_state)
    if state == "visible":
        glyph = style.glyph if unicode else style.ascii_glyph
        return f"{glyph} VISIBLE"
    if state == "confirmed":
        glyph = style.glyph if unicode else style.ascii_glyph
        return f"{glyph} CONFIRMED"
    if state == "selected":
        glyph = style.glyph if unicode else style.ascii_glyph
        return f"{glyph} SELECTED"
    # Glyph + label only. Cooldown remaining time is a cooldowns-section fact.
    return style.text(unicode=unicode, show_remaining=False)


def _remaining_seconds(cooldown_until: str | None, now: datetime | None) -> float | None:
    if not cooldown_until:
        return None
    try:
        stamp = cooldown_until.replace("Z", "+00:00")
        until = datetime.fromisoformat(stamp)
        if until.tzinfo is None:
            until = until.replace(tzinfo=timezone.utc)
        if now is None:
            return None  # remaining unknown unless the caller supplies observed now
        moment = now
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return (until - moment).total_seconds()
    except ValueError:
        return None


def _candidate_reason_cell(candidate: CandidateRecord, evaluation: EligibilityEvaluation) -> str:
    state = candidate_state(candidate, evaluation)
    if state == "selected":
        # The full reason is the "selected because" line above the table.
        if evaluation.selected_because:
            return "see selected because"
        return "unknown (no rank components recorded)"
    if state == "rejected" or state == "cooldown":
        stage, reason = first_rejection(candidate)
        return f"{stage}: {_safe_text(reason)}"
    if state == "confirmed":
        return "observed_route matches selected"
    if state == "admitted":
        return f"reached {_fmt(candidate.reached)}"
    if state == "visible":
        return f"reached {_fmt(candidate.reached)}"
    return "unknown"


def _provider_width(rows: Sequence[CandidateRecord]) -> int:
    widest = max((len(_fmt(c.provider) if c.provider else "unknown") for c in rows), default=0)
    return max(10, widest)


def _rank_cell(rank: int | None, mode: PresentationMode) -> Text:
    if rank is None:
        return Text("\u2014", style=_style("MUTED", mode))
    return Text(str(rank), style=_style("TEXT", mode))


def _build_table(
    evaluation: EligibilityEvaluation,
    rows: Sequence[CandidateRecord],
    mode: PresentationMode,
    *,
    now: datetime | None = None,
    phase: float = 0.0,
) -> Table:
    """Fixed widths for state, rank, and capacity. Ratio for route and reason.

    Panel padding is (1, 2), so the inner width is mode.width - 6. Provider
    is at least 10 so names like openrouter are not cut to openrou.
    """
    inner = max(40, mode.width - 6)
    provider_w = _provider_width(rows)
    state_w = 12  # glyph + space + COOLDOWN; remaining time is not in this cell
    show_rank = inner >= 90
    show_comp = inner >= 130
    rank_w = 6
    capacity_w = 12
    fixed = 2 + provider_w + 2 + state_w  # route padding + provider + state
    if show_rank:
        fixed += 2 + rank_w + 2 + capacity_w
    if show_comp:
        fixed += 2 + 12
    fixed += 2  # reason padding
    leftover = max(8, inner - fixed)
    route_w = max(10, leftover // 3)
    reason_w = max(8, leftover - route_w)

    table = Table(
        expand=False,
        show_header=True,
        header_style=_style("SECONDARY", mode),
        border_style=_style("BORDER", mode),
        box=None,
        pad_edge=False,
        padding=(0, 1),
        collapse_padding=True,
    )
    table.add_column(
        "route", overflow="ellipsis", no_wrap=True, ratio=3, min_width=route_w, max_width=route_w
    )
    table.add_column(
        "provider",
        overflow="ellipsis",
        no_wrap=True,
        min_width=provider_w,
        max_width=provider_w,
        width=provider_w,
    )
    table.add_column(
        "state",
        overflow="ellipsis",
        no_wrap=True,
        width=state_w,
        min_width=state_w,
        max_width=state_w,
    )
    if show_rank:
        table.add_column(
            "rank",
            overflow="ellipsis",
            no_wrap=True,
            justify="right",
            width=rank_w,
            min_width=rank_w,
            max_width=rank_w,
        )
        table.add_column(
            "capacity",
            overflow="ellipsis",
            no_wrap=True,
            width=capacity_w,
            min_width=capacity_w,
            max_width=capacity_w,
        )
    if show_comp:
        table.add_column(
            "components", overflow="ellipsis", no_wrap=True, ratio=2, min_width=12, max_width=24
        )
    table.add_column(
        "reason", overflow="ellipsis", no_wrap=True, ratio=4, min_width=reason_w, max_width=reason_w
    )

    for cand in rows:
        state = candidate_state(cand, evaluation)
        state_cell = _state_renderable(state, mode, remaining_seconds=None, phase=phase)
        reason = _candidate_reason_cell(cand, evaluation)
        cells: list[RenderableType] = [
            Text(_fmt(cand.route_id) if cand.route_id else "unknown", style=_style("TEXT", mode)),
            Text(_fmt(cand.provider) if cand.provider else "unknown", style=_style("TEXT", mode)),
            state_cell,
        ]
        if show_rank:
            cells.append(_rank_cell(cand.rank, mode))
            cells.append(Text(_capacity_source(cand), style=_style("TEXT", mode)))
        if show_comp:
            cells.append(Text(_fmt_components(cand.rank_components), style=_style("MUTED", mode)))
        cells.append(Text(reason, style=_style("MUTED", mode)))
        table.add_row(*cells)
    return table


def _evaluation_blocks(
    evaluation: EligibilityEvaluation,
    mode: PresentationMode,
    *,
    page: int,
    page_size: int,
    state_filter: str | None,
    provider_filter: str | None,
    text_filter: str | None,
    now: datetime | None,
    phase: float,
    generated_at: str,
) -> list[RenderableType]:
    blocks: list[RenderableType] = []
    node = evaluation.node_id or "unknown"
    header = Text.assemble(
        (f"node {node}", _style("TEXT", mode)),
        (f"  seq {evaluation.seq}  ", _style("SECONDARY", mode)),
        (_headline(evaluation), _style("SECONDARY", mode)),
    )
    blocks.append(header)
    blocks.append(_funnel_renderable(evaluation, mode))
    blocks.append(Text(_freshness(evaluation, generated_at), style=_style("MUTED", mode)))

    selected = evaluation.selected_route or "unknown"
    observed = evaluation.observed_route or "unknown"
    ident = f"selected_route={selected}  observed_route={observed}"
    if evaluation.session_ref:
        ident += f"  session={_safe_text(evaluation.session_ref)}"
    ident_style = "ERROR" if evaluation.selected_observed_mismatch else "TEXT"
    blocks.append(Text(ident, style=_style(ident_style, mode)))
    if evaluation.selected_observed_mismatch:
        blocks.append(
            Text("MISMATCH: selected route != observed reported_model", style=_style("ERROR", mode))
        )
    for line in _selected_because_lines(evaluation):
        blocks.append(_because_renderable(line, mode))

    if evaluation.candidates is None:
        blocks.append(
            Text(
                "candidates: unknown (not recorded on this eligibility event)",
                style=_style("MUTED", mode),
            )
        )
    else:
        filtered = filter_candidates(
            evaluation.candidates,
            evaluation,
            state=state_filter,
            provider=provider_filter,
            text=text_filter,
        )
        page_rows, page_index, page_count = paginate(filtered, page=page, page_size=page_size)
        filter_bits = []
        if state_filter:
            filter_bits.append(f"state={state_filter}")
        if provider_filter:
            filter_bits.append(f"provider={provider_filter}")
        if text_filter:
            filter_bits.append(f"text={text_filter}")
        filt = ("  filter " + " ".join(filter_bits)) if filter_bits else ""
        blocks.append(
            Text(
                f"showing {len(page_rows)}/{len(filtered)} "
                f"(page {page_index + 1}/{page_count}, size {page_size})"
                f"{filt}",
                style=_style("MUTED", mode),
            )
        )
        if page_rows:
            blocks.append(_build_table(evaluation, page_rows, mode, now=now, phase=phase))
            if any(cand.rank is None for cand in page_rows):
                blocks.append(Text("rank \u2014 not recorded", style=_style("MUTED", mode)))
        else:
            blocks.append(Text("no candidates on this page", style=_style("MUTED", mode)))
        # AC7: show per-state summary for omitted routes
        omitted_line = _omitted_summary_line(evaluation)
        if omitted_line:
            blocks.append(Text(omitted_line, style=_style("MUTED", mode)))

    cooldowns = _cooldown_rows(evaluation)
    if cooldowns:
        blocks.append(Text("cooldowns", style=_style("AMBER", mode)))
        for cand in cooldowns:
            remaining = _remaining_seconds(cand.cooldown_until, now)
            remaining_txt = _fmt_remaining(remaining)
            scope_txt = _cooldown_scope_label(cand)
            pool_txt = _pool_label(cand)
            line = (
                f"  {_fmt(cand.route_id)}  until={_fmt(cand.cooldown_until)}  "
                f"remaining={remaining_txt}  capacity={_capacity_source(cand)}"
            )
            if scope_txt:
                line += f"  scope={scope_txt}"
            if pool_txt:
                line += f"  pool={pool_txt}"
            blocks.append(Text(line, style=_style("COOLDOWN", mode)))
    else:
        blocks.append(Text("cooldowns: none recorded", style=_style("MUTED", mode)))

    if evaluation.rejections:
        bits = []
        for stage in sorted(evaluation.rejections):
            reasons = evaluation.rejections[stage]
            for reason, count in sorted(reasons.items()):
                bits.append(f"{stage}:{_safe_text(reason)}={count}")
        blocks.append(Text("rejections  " + "  ".join(bits), style=_style("MUTED", mode)))
    else:
        blocks.append(Text("rejections: unknown or none recorded", style=_style("MUTED", mode)))
    return blocks


def render_routing(
    view: RoutingView,
    mode: PresentationMode | None = None,
    *,
    page: int = 0,
    page_size: int = _DEFAULT_PAGE_SIZE,
    state: str | None = None,
    provider: str | None = None,
    text: str | None = None,
    clock: MotionClock | None = None,
    now: datetime | None = None,
    evaluation_index: int | None = None,
) -> RenderableType:
    """Rich renderable of an authoritative :class:`RoutingView`."""
    mode = presentation_mode() if mode is None else mode
    phase = 0.0
    if clock is not None and mode.animate:
        phase = clock.phase()

    blocks: list[RenderableType] = []
    source = view.source or "unknown"
    generated = view.generated_at or "unknown"
    n_eval = len(view.evaluations)
    blocks.append(
        Text.assemble(
            ("routing explorer", _style("PRIMARY", mode)),
            (
                f"  source={source}  evaluations={n_eval}  generated_at={generated}",
                _style("SECONDARY", mode),
            ),
        )
    )
    if not view.evaluations:
        blocks.append(Text("no eligibility evidence recorded", style=_style("MUTED", mode)))
        body = Group(*blocks)
        return panel(body, title="routing", mode=mode, tone="BORDER")

    indices: Sequence[int]
    if evaluation_index is None:
        indices = range(len(view.evaluations))
    else:
        idx = max(0, min(evaluation_index, len(view.evaluations) - 1))
        indices = (idx,)

    for i, eval_i in enumerate(indices):
        if i:
            blocks.append(Text(""))
        blocks.extend(
            _evaluation_blocks(
                view.evaluations[eval_i],
                mode,
                page=page,
                page_size=page_size,
                state_filter=state,
                provider_filter=provider,
                text_filter=text,
                now=now,
                phase=phase,
                generated_at=view.generated_at,
            )
        )
    body = Group(*blocks)
    return panel(body, title="routing", mode=mode, tone="BORDER")


def render_routing_text(
    view: RoutingView,
    width: int = 100,
    *,
    page: int = 0,
    page_size: int = _DEFAULT_PAGE_SIZE,
    state: str | None = None,
    provider: str | None = None,
    text: str | None = None,
    now: datetime | None = None,
    evaluation_index: int | None = None,
) -> str:
    """Plain, NO_COLOR-safe text. No ANSI, no required box-drawing."""
    width = max(40, int(width))
    lines: list[str] = []
    source = view.source or "unknown"
    generated = view.generated_at or "unknown"
    lines.append("routing")
    lines.extend(
        _wrap_plain(
            f"explorer  source={source}  evaluations={len(view.evaluations)}  generated_at={generated}",
            width,
        )
    )
    if not view.evaluations:
        lines.append("no eligibility evidence recorded")
        return "\n".join(lines) + "\n"

    indices: Sequence[int]
    if evaluation_index is None:
        indices = range(len(view.evaluations))
    else:
        idx = max(0, min(evaluation_index, len(view.evaluations) - 1))
        indices = (idx,)

    for i, eval_i in enumerate(indices):
        if i:
            lines.append("")
        evaluation = view.evaluations[eval_i]
        node = evaluation.node_id or "unknown"
        lines.extend(
            _wrap_plain(f"node {node}  seq {evaluation.seq}  {_headline(evaluation)}", width)
        )
        lines.extend(_wrap_plain(_funnel_line(evaluation), width))
        lines.extend(_wrap_plain(_freshness(evaluation, view.generated_at), width))
        selected = evaluation.selected_route or "unknown"
        observed = evaluation.observed_route or "unknown"
        ident = f"selected_route={selected}  observed_route={observed}"
        if evaluation.session_ref:
            ident += f"  session={_safe_text(evaluation.session_ref)}"
        lines.extend(_wrap_plain(ident, width))
        if evaluation.selected_observed_mismatch:
            lines.extend(_wrap_plain("MISMATCH: selected route != observed reported_model", width))
        for because in _selected_because_lines(evaluation):
            lines.extend(_wrap_plain(because, width))

        if evaluation.candidates is None:
            lines.append("candidates: unknown (not recorded on this eligibility event)")
        else:
            filtered = filter_candidates(
                evaluation.candidates, evaluation, state=state, provider=provider, text=text
            )
            page_rows, page_index, page_count = paginate(filtered, page=page, page_size=page_size)
            filter_bits = []
            if state:
                filter_bits.append(f"state={state}")
            if provider:
                filter_bits.append(f"provider={provider}")
            if text:
                filter_bits.append(f"text={text}")
            filt = ("  filter " + " ".join(filter_bits)) if filter_bits else ""
            lines.append(
                f"showing {len(page_rows)}/{len(filtered)} "
                f"(page {page_index + 1}/{page_count}, size {page_size})"
                f"{filt}"
            )
            if not page_rows:
                lines.append("no candidates on this page")
            else:
                show_rank = width >= 100
                show_comp = width >= 140
                provider_w = _provider_width(page_rows)
                state_w = 12  # ascii glyph + space + COOLDOWN (10)
                # widths: fixed state/rank/capacity; provider from content; rest to route+reason
                fixed = state_w + provider_w
                if show_rank:
                    fixed += 6 + 12
                if show_comp:
                    fixed += 16
                # one space between route, provider, state, and each optional column, plus reason
                n_gaps = 3 + (2 if show_rank else 0) + (1 if show_comp else 0)
                leftover = max(16, width - fixed - n_gaps)
                route_w = max(8, leftover // 3)
                reason_w = max(8, leftover - route_w)

                def _fit(value: str, size: int, *, right: bool = False) -> str:
                    if len(value) > size:
                        if size <= 1:
                            return value[:size]
                        return value[: size - 1] + "\u2026"
                    if right:
                        return value.rjust(size)
                    return value.ljust(size)

                header_cells = [
                    _fit("route", route_w),
                    _fit("provider", provider_w),
                    _fit("state", state_w),
                ]
                if show_rank:
                    header_cells.append(_fit("rank", 6, right=True))
                    header_cells.append(_fit("capacity", 12))
                if show_comp:
                    header_cells.append(_fit("components", 16))
                header_cells.append(_fit("reason", reason_w))
                lines.append(" ".join(header_cells).rstrip())
                for cand in page_rows:
                    st = candidate_state(cand, evaluation)
                    st_txt = _plain_state(st, unicode=False)
                    cells = [
                        _fit(_fmt(cand.route_id) if cand.route_id else "unknown", route_w),
                        _fit(_fmt(cand.provider) if cand.provider else "unknown", provider_w),
                        _fit(st_txt, state_w),
                    ]
                    if show_rank:
                        cells.append(_fit(_fmt_rank(cand.rank), 6, right=True))
                        cells.append(_fit(_capacity_source(cand), 12))
                    if show_comp:
                        cells.append(_fit(_fmt_components(cand.rank_components), 16))
                    cells.append(_fit(_candidate_reason_cell(cand, evaluation), reason_w))
                    lines.append(" ".join(cells).rstrip())
                if show_rank and any(cand.rank is None for cand in page_rows):
                    lines.append("rank \u2014 not recorded")

        cooldowns = _cooldown_rows(evaluation)
        if cooldowns:
            lines.append("cooldowns")
            for cand in cooldowns:
                remaining = _remaining_seconds(cand.cooldown_until, now)
                remaining_txt = _fmt_remaining(remaining)
                _line = (
                    f"  {_fmt(cand.route_id)}  until={_fmt(cand.cooldown_until)}  "
                    f"remaining={remaining_txt}  capacity={_capacity_source(cand)}"
                )
                scope_txt = _cooldown_scope_label(cand)
                pool_txt = _pool_label(cand)
                if scope_txt:
                    _line += f"  scope={scope_txt}"
                if pool_txt:
                    _line += f"  pool={pool_txt}"
                lines.extend(_wrap_plain(_line, width))
        else:
            lines.append("cooldowns: none recorded")

        # AC7: omitted summary line in text render
        _omitted_line = _omitted_summary_line(evaluation)
        if _omitted_line:
            lines.extend(_wrap_plain(_omitted_line, width))

        if evaluation.rejections:
            bits = []
            for stage in sorted(evaluation.rejections):
                reasons = evaluation.rejections[stage]
                for reason, count in sorted(reasons.items()):
                    bits.append(f"{stage}:{_safe_text(reason)}={count}")
            lines.extend(_wrap_plain("rejections  " + "  ".join(bits), width))
        else:
            lines.append("rejections: unknown or none recorded")
    return "\n".join(lines) + "\n"


def routing_json(view: RoutingView) -> dict[str, Any]:
    """Same facts as the TUI, no secrets. For the CLI after #708 merges."""
    payload = redact_contract_secrets(view.to_dict())
    evaluations: list[dict[str, Any]] = []
    for evaluation in view.evaluations:
        candidates_out: list[dict[str, Any]] | None
        if evaluation.candidates is None:
            candidates_out = None
        else:
            candidates_out = []
            for cand in evaluation.candidates:
                state = candidate_state(cand, evaluation)
                stage, reason = first_rejection(cand)
                record = cand.to_dict()
                record["state"] = state
                record["first_rejection_stage"] = stage
                record["first_rejection_reason"] = (
                    _safe_text(reason) if reason != "unknown" else reason
                )
                record["capacity_source"] = cand.capacity_class  # None stays None
                record["capacity_evidence"] = cand.capacity_evidence or None  # AC6
                record["pool"] = cand.pool or None  # AC6
                record["cooldown_scope"] = cand.cooldown_scope or None  # AC6
                record["freshness"] = evaluation.at or None
                for key, value in list(record.items()):
                    if isinstance(value, str):
                        record[key] = _safe_text(value)
                candidates_out.append(redact_contract_secrets(record))
        evaluations.append(
            {
                "at": evaluation.at or None,
                "candidates": candidates_out,
                "candidates_omitted": evaluation.candidates_omitted,
                "omitted_summary": evaluation.omitted_summary,  # AC7
                "funnel": dict(evaluation.funnel),
                "headline": _headline(evaluation),
                "node_id": evaluation.node_id,
                "observed_route": evaluation.observed_route,
                "rejections": evaluation.rejections,
                "selected_because": [_safe_text(item) for item in evaluation.selected_because],
                "selected_observed_mismatch": evaluation.selected_observed_mismatch,
                "selected_route": evaluation.selected_route,
                "seq": evaluation.seq,
                "session_ref": (
                    _safe_text(evaluation.session_ref) if evaluation.session_ref else None
                ),
            }
        )
    return {
        "evaluations": evaluations,
        "generated_at": payload.get("generated_at"),
        "schema_version": payload.get("schema_version"),
        "source": payload.get("source"),
    }


__all__ = [
    "FUNNEL_STAGES",
    "candidate_state",
    "filter_candidates",
    "first_rejection",
    "paginate",
    "render_routing",
    "render_routing_text",
    "routing_json",
    "routing_view_from_run_view",
]
