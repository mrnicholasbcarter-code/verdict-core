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


def _fmt_remaining(remaining: float | None) -> str:
    if remaining is None:
        return "unknown"
    if remaining <= 0:
        return "elapsed"
    return f"{int(remaining)}s"


def _fmt_rank(rank: int | None) -> str:
    return "unknown" if rank is None else str(rank)


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


def _headline(evaluation: EligibilityEvaluation) -> str:
    discovered = evaluation.funnel.get("DISCOVERED", 0)
    admitted = evaluation.funnel.get("TASK_ELIGIBLE", 0)
    selected = evaluation.funnel.get("SELECTED", 0)
    omitted = evaluation.candidates_omitted
    extra = f", {omitted} omitted" if omitted else ""
    return f"{discovered} candidates, {admitted} admitted, {selected} selected{extra}"


def _funnel_line(evaluation: EligibilityEvaluation) -> str:
    parts: list[str] = []
    for stage in FUNNEL_STAGES:
        count = evaluation.funnel.get(stage)
        parts.append(f"{stage} {_fmt(count) if count is None else count}")
    return " > ".join(parts)


def _freshness(evaluation: EligibilityEvaluation, generated_at: str) -> str:
    """Show recorded timestamps. Never fabricates a freshness probe."""
    at = evaluation.at or "unknown"
    gen = generated_at or "unknown"
    return f"evaluated_at={at}  view_generated_at={gen}"


def _capacity_source(candidate: CandidateRecord) -> str:
    """Capacity class is the recorded economic source; unknown stays unknown."""
    return _fmt(candidate.capacity_class)


def _selected_because_lines(evaluation: EligibilityEvaluation) -> list[str]:
    if not evaluation.selected_because:
        if evaluation.selected_route:
            return ["selected because: unknown (no rank components recorded)"]
        return ["selected because: (no selection)"]
    return [f"selected because: {_safe_text(item)}" for item in evaluation.selected_because]


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
    """Glyph + label via design.render_state. Motion only for observed running."""
    design_state = _STATE_TOKEN[state]
    rendered = render_state(design_state, mode=mode, remaining_seconds=remaining_seconds)
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


def _plain_state(
    state: CandidateState, *, unicode: bool, remaining_seconds: float | None = None
) -> str:
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
    return style.text(unicode=unicode, remaining_seconds=remaining_seconds)


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
        if evaluation.selected_because:
            return _safe_text(evaluation.selected_because[0])
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


def _build_table(
    evaluation: EligibilityEvaluation,
    rows: Sequence[CandidateRecord],
    mode: PresentationMode,
    *,
    now: datetime | None = None,
    phase: float = 0.0,
) -> Table:
    table = Table(
        expand=True,
        show_header=True,
        header_style=_style("SECONDARY", mode),
        border_style=_style("BORDER", mode),
        box=None,
        pad_edge=False,
        collapse_padding=True,
    )
    table.add_column("route", overflow="ellipsis", no_wrap=True, ratio=3, min_width=10)
    table.add_column("provider", overflow="ellipsis", no_wrap=True, ratio=1, min_width=4)
    table.add_column("state", overflow="ellipsis", no_wrap=True, ratio=3, min_width=12)
    if mode.width >= 100:
        table.add_column("rank", overflow="ellipsis", no_wrap=True, justify="right", min_width=4)
        table.add_column("capacity", overflow="ellipsis", no_wrap=True, ratio=2, min_width=10)
    if mode.width >= 140:
        table.add_column("components", overflow="ellipsis", no_wrap=True, ratio=3, min_width=8)
    table.add_column("reason", overflow="ellipsis", no_wrap=True, ratio=4, min_width=8)

    for cand in rows:
        state = candidate_state(cand, evaluation)
        remaining = _remaining_seconds(cand.cooldown_until, now)
        state_cell = _state_renderable(state, mode, remaining_seconds=remaining, phase=phase)
        reason = _candidate_reason_cell(cand, evaluation)
        cells: list[RenderableType] = [
            Text(_fmt(cand.route_id) if cand.route_id else "unknown"),
            Text(_fmt(cand.provider) if cand.provider else "unknown"),
            state_cell,
        ]
        if mode.width >= 100:
            cells.append(Text(_fmt_rank(cand.rank)))
            cells.append(Text(_capacity_source(cand)))
        if mode.width >= 140:
            cells.append(Text(_fmt_components(cand.rank_components)))
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
    header = f"node {node}  seq {evaluation.seq}  {_headline(evaluation)}"
    blocks.append(Text(header, style=_style("PRIMARY", mode)))
    blocks.append(Text(_funnel_line(evaluation), style=_style("SECONDARY", mode)))
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
        blocks.append(Text(line, style=_style("CYAN", mode)))

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
        else:
            blocks.append(Text("no candidates on this page", style=_style("MUTED", mode)))

    cooldowns = _cooldown_rows(evaluation)
    if cooldowns:
        blocks.append(Text("cooldowns", style=_style("AMBER", mode)))
        for cand in cooldowns:
            remaining = _remaining_seconds(cand.cooldown_until, now)
            remaining_txt = _fmt_remaining(remaining)
            line = (
                f"  {_fmt(cand.route_id)}  until={_fmt(cand.cooldown_until)}  "
                f"remaining={remaining_txt}  capacity={_capacity_source(cand)}"
            )
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
        Text(
            f"routing explorer  source={source}  evaluations={n_eval}  generated_at={generated}",
            style=_style("PRIMARY", mode),
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
    lines.append(
        f"explorer  source={source}  evaluations={len(view.evaluations)}  generated_at={generated}"
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
        lines.append(f"node {node}  seq {evaluation.seq}  {_headline(evaluation)}")
        lines.append(_funnel_line(evaluation))
        lines.append(_freshness(evaluation, view.generated_at))
        selected = evaluation.selected_route or "unknown"
        observed = evaluation.observed_route or "unknown"
        ident = f"selected_route={selected}  observed_route={observed}"
        if evaluation.session_ref:
            ident += f"  session={_safe_text(evaluation.session_ref)}"
        lines.append(ident)
        if evaluation.selected_observed_mismatch:
            lines.append("MISMATCH: selected route != observed reported_model")
        lines.extend(_selected_because_lines(evaluation))

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
                header = ["route", "provider", "state"]
                if show_rank:
                    header.extend(["rank", "capacity"])
                if show_comp:
                    header.append("components")
                header.append("reason")
                lines.append("  ".join(header))
                for cand in page_rows:
                    st = candidate_state(cand, evaluation)
                    remaining = _remaining_seconds(cand.cooldown_until, now)
                    st_txt = _plain_state(st, unicode=False, remaining_seconds=remaining)
                    cells = [
                        _fmt(cand.route_id) if cand.route_id else "unknown",
                        _fmt(cand.provider) if cand.provider else "unknown",
                        st_txt,
                    ]
                    if show_rank:
                        cells.append(_fmt_rank(cand.rank))
                        cells.append(_capacity_source(cand))
                    if show_comp:
                        cells.append(_fmt_components(cand.rank_components))
                    cells.append(_candidate_reason_cell(cand, evaluation))
                    row = "  ".join(cells)
                    if len(row) > width:
                        row = row[: max(0, width - 1)] + "..."
                    lines.append(row)

        cooldowns = _cooldown_rows(evaluation)
        if cooldowns:
            lines.append("cooldowns")
            for cand in cooldowns:
                remaining = _remaining_seconds(cand.cooldown_until, now)
                remaining_txt = _fmt_remaining(remaining)
                lines.append(
                    f"  {_fmt(cand.route_id)}  until={_fmt(cand.cooldown_until)}  "
                    f"remaining={remaining_txt}  capacity={_capacity_source(cand)}"
                )
        else:
            lines.append("cooldowns: none recorded")

        if evaluation.rejections:
            bits = []
            for stage in sorted(evaluation.rejections):
                reasons = evaluation.rejections[stage]
                for reason, count in sorted(reasons.items()):
                    bits.append(f"{stage}:{_safe_text(reason)}={count}")
            lines.append("rejections  " + "  ".join(bits))
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
