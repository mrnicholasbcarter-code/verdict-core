"""Interactive cockpit navigation for ``verdict watch`` (read-only).

Keyboard-first selection of nodes/workers over a live :class:`RunView`.
Controls (cancel/retry) route through a separate shared-action layer
(BOD-275 + PR #713 control channel) and are NOT implemented here; the
extension points below are intentionally no-ops.

Failure semantics: for each node the failure -> cooldown/exclusion ->
replacement -> continuation is reduced to ONE collapsed transition line;
technical details (error text, category, evidence) expand on ``d``.

Injectable key reader: real TTYs use :class:`_RealKeyReader`; tests use
:class:`ScriptedKeyReader`, which raises :class:`EOFError` after its
scripted keys are consumed. Every cockpit loop has a hard iteration cap
so no scripted-key stub can spin forever.

The public entry point :func:`run_cockpit` is called from
:func:`verdict.orchestration.tui.follow` when stdout is a TTY and no
explicit non-interactive mode is requested. When the output is
non-TTY / NO_COLOR / CI / TERM=dumb, the caller uses the existing plain
follower unchanged.
"""

from __future__ import annotations

import os
import sys
import time
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any, ClassVar, Protocol

from rich.console import Console, Group, RenderableType
from rich.live import Live
from rich.panel import Panel
from rich.text import Text

from verdict.design import TOKENS

# ---------------------------------------------------------------------------
# Key reader contract
# ---------------------------------------------------------------------------


class KeyReader(Protocol):
    """Injectable input source. ``read()`` returns a key or ``None`` on
    timeout (no key ready). ``EOFError`` signals the reader is exhausted
    and the cockpit must exit its loop.
    """

    def read(self, timeout: float = 0.05) -> str | None: ...

    def close(self) -> None: ...


# Semantic key names used across the cockpit.
KEY_UP = "UP"
KEY_DOWN = "DOWN"
KEY_LEFT = "LEFT"
KEY_RIGHT = "RIGHT"
KEY_ENTER = "ENTER"
KEY_ESC = "ESC"
KEY_QUIT = "q"
KEY_HELP = "?"
KEY_DETAILS = "d"
KEY_CONTEXT = "c"  # BOD-278 extension point
KEY_ROUTING = "r"  # BOD-277 extension point
KEY_HEALTH = "h"  # BOD-276 health panel

# Synthetic selectable row IDs for non-worker roles.
ROLE_CONTROLLER = "__controller__"
ROLE_REVIEWER = "__reviewer__"


class ScriptedKeyReader:
    """Deterministic reader for tests. Raises :class:`EOFError` when the
    scripted keys are exhausted so no cockpit loop can spin forever."""

    def __init__(self, keys: Sequence[str]) -> None:
        self._iter: Iterator[str] = iter(list(keys))
        self._closed = False

    def read(self, timeout: float = 0.05) -> str | None:
        if self._closed:
            raise EOFError("ScriptedKeyReader closed")
        try:
            return next(self._iter)
        except StopIteration as exc:
            raise EOFError("ScriptedKeyReader exhausted") from exc

    def close(self) -> None:
        self._closed = True


class _RealKeyReader:
    """POSIX tty reader with raw mode; falls back to line-buffered stdin
    when raw mode is unavailable. Restores original terminal state on
    :meth:`close`."""

    _ESCAPE_MAP: ClassVar[dict[str, str]] = {
        "\x1b[A": KEY_UP,
        "\x1b[B": KEY_DOWN,
        "\x1b[C": KEY_RIGHT,
        "\x1b[D": KEY_LEFT,
    }

    def __init__(self, stream: Any = None) -> None:
        self._stream = stream if stream is not None else sys.stdin
        self._fd: int | None = None
        self._saved: Any = None
        self._raw = False
        try:
            self._fd = self._stream.fileno()
        except Exception:
            self._fd = None
        if self._fd is not None and os.isatty(self._fd):
            try:
                import termios
                import tty

                self._termios = termios
                self._saved = termios.tcgetattr(self._fd)
                tty.setcbreak(self._fd)
                self._raw = True
            except Exception:
                self._saved = None

    def read(self, timeout: float = 0.05) -> str | None:
        if self._fd is None:
            return None
        import select

        try:
            ready, _, _ = select.select([self._fd], [], [], max(0.0, timeout))
        except Exception:
            return None
        if not ready:
            return None
        try:
            first = os.read(self._fd, 1)
        except OSError as exc:
            raise EOFError("terminal input closed") from exc
        if not first:
            raise EOFError("terminal input closed")
        ch = first.decode("utf-8", errors="ignore")
        if ch == "\x1b":
            # Try to read an escape sequence; short timeout so a bare ESC still fires.
            try:
                ready2, _, _ = select.select([self._fd], [], [], 0.02)
            except Exception:
                ready2 = []
            if not ready2:
                return KEY_ESC
            try:
                more = os.read(self._fd, 2)
            except OSError:
                return KEY_ESC
            seq = "\x1b" + more.decode("utf-8", errors="ignore")
            return self._ESCAPE_MAP.get(seq, KEY_ESC)
        if ch in ("\r", "\n"):
            return KEY_ENTER
        if ch == "\x03":  # Ctrl-C
            raise KeyboardInterrupt
        if ch == "\x04":  # Ctrl-D
            raise EOFError("EOF from terminal")
        return ch

    def close(self) -> None:
        import contextlib

        if self._raw and self._fd is not None and self._saved is not None:
            with contextlib.suppress(Exception):
                self._termios.tcsetattr(self._fd, self._termios.TCSADRAIN, self._saved)
        self._raw = False


# ---------------------------------------------------------------------------
# Selection state
# ---------------------------------------------------------------------------


@dataclass
class CockpitState:
    """Read-only navigation state over a live :class:`RunView`."""

    node_order: list[str] = field(default_factory=list)
    selected_index: int = 0
    detail_open: bool = False
    expanded: bool = False
    help_open: bool = False
    quit_requested: bool = False
    routing_open: bool = False
    routing_view: Any = None
    routing_render: Any = None
    routing_render_text: Any = None
    routing_from_run: Any = None

    def sync_order(self, node_ids: Sequence[str]) -> None:
        prev = self.selected_id
        self.node_order = [nid for nid in node_ids if nid]
        if not self.node_order:
            self.selected_index = 0
            return
        if prev in self.node_order:
            self.selected_index = self.node_order.index(prev)
        else:
            self.selected_index = max(0, min(self.selected_index, len(self.node_order) - 1))

    @property
    def selected_id(self) -> str:
        if not self.node_order:
            return ""
        idx = max(0, min(self.selected_index, len(self.node_order) - 1))
        return self.node_order[idx]

    def move(self, delta: int) -> None:
        if not self.node_order:
            return
        self.selected_index = (self.selected_index + delta) % len(self.node_order)


def selectable_order_with_roles(view: Any) -> list[str]:
    """Build selectable row order: controller, workers, reviewer.

    Controller and reviewer entries appear only when the view has evidence
    for those roles.  Workers come from ``view.nodes``.
    """
    order: list[str] = []
    if getattr(view, "controller_route", "") or getattr(view, "controller_state", ""):
        order.append(ROLE_CONTROLLER)
    order.extend(nid for nid in getattr(view, "nodes", {}) if nid)
    if getattr(view, "review", None) is not None:
        order.append(ROLE_REVIEWER)
    return order


def is_role_row(row_id: str) -> bool:
    """True when ``row_id`` is a synthetic role, not a worker node."""
    return row_id in (ROLE_CONTROLLER, ROLE_REVIEWER)


# ---------------------------------------------------------------------------
# Extension points (BOD-277 routing, BOD-278 context, BOD-275/PR-713 controls)
# ---------------------------------------------------------------------------


def open_context_view(state: CockpitState, view: Any) -> None:
    """Toggle recorded context provenance for the selected worker."""
    from verdict.orchestration.cockpit_controls import context_for_selected
    from verdict.orchestration.context_render import render_context, render_context_text

    open_now = not bool(getattr(state, "context_open", False))
    state.context_open = open_now  # type: ignore[attr-defined]
    state.context_view = context_for_selected(state, view) if open_now else None  # type: ignore[attr-defined]
    state.context_render = render_context  # type: ignore[attr-defined]
    state.context_render_text = render_context_text  # type: ignore[attr-defined]


def open_routing_view(state: CockpitState, view: Any) -> None:
    """BOD-277 extension point: toggle the routing explorer panel.

    Builds a :class:`~verdict.orchestration.routing_view.RoutingView` from the
    live :class:`RunView`. The compose path refreshes from the event stream
    when present so the explorer stays authoritative without probing.
    """
    from verdict.orchestration.routing_render import (
        render_routing,
        render_routing_text,
        routing_view_from_run_view,
    )

    open_now = not state.routing_open
    state.routing_open = open_now
    if not open_now:
        state.routing_view = None
        return
    state.routing_view = routing_view_from_run_view(view)
    state.routing_render = render_routing
    state.routing_render_text = render_routing_text
    state.routing_from_run = routing_view_from_run_view


def submit_control(state: CockpitState, action: str) -> None:
    """Submit through the shared CLI/cockpit action, never mutate run state."""
    from verdict.orchestration.cockpit_controls import submit_control as submit

    submit(state, action)


# ---------------------------------------------------------------------------
# Failure -> cooldown -> replacement -> continuation collapse
# ---------------------------------------------------------------------------


@dataclass
class TransitionChain:
    """One collapsed failure/cooldown/replacement/continuation transition.

    ``summary`` is the short one-line form (always safe to render).
    ``technical`` holds the raw evidence, exposed only when ``d`` expands
    the detail panel.
    """

    summary: str = ""
    technical: list[str] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not self.summary and not self.technical


def transition_chain_for(view: Any, node_id: str) -> TransitionChain:
    """Reduce this node's failure/cooldown/reassign history into ONE line.

    ``view`` is duck-typed on :class:`verdict.orchestration.tui.RunView`
    to avoid an import cycle.  The chain is derived from what the runtime
    recorded (no inference from missing evidence).
    """
    failures = [f for f in getattr(view, "failures", []) if getattr(f, "node_id", "") == node_id]
    reassigns = [
        r for r in getattr(view, "reassignments", []) if getattr(r, "node_id", "") == node_id
    ]
    if not failures and not reassigns:
        return TransitionChain()

    chain = TransitionChain()
    last_failure = failures[-1] if failures else None
    last_reassign = reassigns[-1] if reassigns else None

    if last_failure is not None:
        cat = last_failure.category or "unknown"
        cooldowns = [c for c in getattr(view, "cooldowns", {}).values()]
        cooldown_bits = []
        for c in cooldowns:
            # Match either the failing route or its provider prefix.
            fail_route = last_failure.route_id or ""
            if not fail_route:
                continue
            if c.key == fail_route or (
                c.scope == "provider" and fail_route.startswith(c.key + "/")
            ):
                cooldown_bits.append(f"{c.scope}:{c.key}")
        parts = [f"failed({cat})"]
        if cooldown_bits:
            parts.append("cooldown[" + ",".join(sorted(set(cooldown_bits))) + "]")
        if last_reassign is not None:
            parts.append(
                f"replaced[{last_reassign.from_route or '?'} -> {last_reassign.to_route or '?'}]"
            )
        node = getattr(view, "nodes", {}).get(node_id)
        if node is not None and getattr(node, "state", None) is not None:
            parts.append(f"now[{node.state.value}]")
        chain.summary = " -> ".join(parts)
    elif last_reassign is not None:
        chain.summary = (
            f"replaced[{last_reassign.from_route or '?'} -> {last_reassign.to_route or '?'}]"
        )

    for f in failures[-3:]:
        line = f"{f.category or 'unknown'} action={f.action or '-'} route={f.route_id or '-'}"
        if f.evidence:
            line += f" evidence={f.evidence}"
        chain.technical.append(line)
    for r in reassigns[-3:]:
        chain.technical.append(
            f"reassign attempt={r.attempt or '-'} {r.from_route or '?'} -> {r.to_route or '?'}"
            f" ({r.reason or '-'})"
        )
    return chain


# ---------------------------------------------------------------------------
# Identity reconciliation: selected route vs observed reported_model
# ---------------------------------------------------------------------------


@dataclass
class IdentityView:
    selected_route: str = ""
    observed_route: str = ""
    session_ref: str = ""
    mismatch: bool = False


def _terminal_events_for(events: Iterable[Any], node_id: str) -> list[Any]:
    """Return terminal events for ``node_id`` in stream order.

    Accepts :class:`RunEvent` or plain mappings.  Uses explicit isinstance
    checks (not ``getattr(..., default)`` fallback) because ``RunEvent``
    attribute defaults are empty strings, not missing attributes.
    """
    from collections.abc import Mapping as _Mapping

    out: list[Any] = []
    for e in events:
        if isinstance(e, _Mapping):
            etype = e.get("type")
            enode = e.get("node_id", "")
        else:
            etype = getattr(e, "type", "")
            enode = getattr(e, "node_id", "")
        if etype == "terminal" and enode == node_id:
            out.append(e)
    return out


def identity_for(view: Any, events: Sequence[Any], node_id: str) -> IdentityView:
    """Compare selected route (selection) with observed identity
    (terminal.reported_model, terminal.session_ref)."""
    node = getattr(view, "nodes", {}).get(node_id)
    selected = getattr(node, "route_id", "") if node is not None else ""
    observed = ""
    session_ref = ""
    from collections.abc import Mapping as _Mapping

    for e in _terminal_events_for(events, node_id):
        data = (
            (e.get("data", {}) or {}) if isinstance(e, _Mapping) else (getattr(e, "data", {}) or {})
        )
        observed = str(data.get("reported_model") or observed)
        session_ref = str(data.get("session_ref") or session_ref)
    mismatch = bool(selected and observed and selected != observed)
    return IdentityView(selected, observed, session_ref, mismatch)


# ---------------------------------------------------------------------------
# Renderers
# ---------------------------------------------------------------------------


_HELP_LINES = (
    "keys:",
    "  up/down or j/k   move selection",
    "  enter            open node detail",
    "  d                expand technical details",
    "  esc              back / close detail",
    "  c                context view (toggle budget/provenance)",
    "  r                routing view (toggle explorer)",
    "  p                verified receipt summary",
    "  h                health evidence (cooldowns, failures)",
    "  x (twice)        request run cancellation",
    "  X / t            cancel / retry selected worker (Enter confirms)",
    "  ?                toggle this help",
    "  q                quit watch",
)


def _style_token(token: str, plain: bool) -> str:
    return "" if plain else TOKENS.get(token, "")


def render_help(plain: bool = False) -> RenderableType:
    text = Text("\n".join(_HELP_LINES), style=_style_token("MUTED", plain))
    if plain:
        return text
    return Panel(text, title="help", border_style=TOKENS["BORDER"])


def render_selected_row(view: Any, state: CockpitState, plain: bool) -> RenderableType:
    """Compact "who is selected" strip for the top of the cockpit."""
    nid = state.selected_id or "-"
    if nid == ROLE_CONTROLLER:
        st_txt = getattr(view, "controller_state", "") or "-"
        label = "controller"
    elif nid == ROLE_REVIEWER:
        review = getattr(view, "review", None)
        st_txt = getattr(review, "status", "") if review else "-"
        label = "reviewer"
    else:
        node = getattr(view, "nodes", {}).get(state.selected_id)
        st = getattr(node, "state", None)
        st_txt = st.value if st is not None else "-"
        label = nid
    body = f"selected: {label}  state: {st_txt}   ({state.selected_index + 1}/{len(state.node_order) or 1})"
    if plain:
        return Text(body)
    return Text(body, style=TOKENS["SECONDARY"])


def render_detail_panel(
    view: Any,
    events: Sequence[Any],
    state: CockpitState,
    *,
    plain: bool = False,
    width: int = 100,
    mode: Any = None,
) -> RenderableType:
    """Detail panel for the currently selected node.

    Layout:
      * Header: node id + authoritative :class:`NodeState` value.
      * Identity: selected route vs observed (reported_model / session_ref);
        MISMATCH flag when they disagree.
      * Transition chain: one collapsed line (failure -> cooldown ->
        replacement -> continuation). Technical details only when
        ``state.expanded`` is True.
      * Expected provider errors (429, quota, 5xx, timeout, overflow) render
        semantically via ``token`` categories, not raw text.
    """
    nid = state.selected_id
    if not nid:
        text = Text("no node selected", style=_style_token("MUTED", plain))
        if plain:
            return text
        from verdict.design import panel

        return panel(text, title="detail", mode=mode)

    # BOD-276: controller and reviewer are selectable role rows.
    if nid == ROLE_CONTROLLER:
        return _render_controller_detail(view, events, state, plain=plain, width=width, mode=mode)
    if nid == ROLE_REVIEWER:
        return _render_reviewer_detail(view, events, state, plain=plain, width=width, mode=mode)

    node = getattr(view, "nodes", {}).get(nid)
    state_value = node.state.value if node is not None else "-"
    attempt = getattr(node, "attempt", 0) if node is not None else 0

    ident = identity_for(view, events, nid)
    chain = transition_chain_for(view, nid)

    lines: list[Text] = []
    lines.append(
        Text(
            f"node: {nid}  state: {state_value}  attempt: {attempt}",
            style=_style_token("PRIMARY", plain),
        )
    )
    lines.append(
        Text(
            f"selected route: {ident.selected_route or 'not selected yet'}",
            style=_style_token("TEXT" if ident.selected_route else "MUTED", plain),
        )
    )
    obs_line = f"observed model: {ident.observed_route or 'not reported yet'}"
    if ident.session_ref:
        obs_line += f"  session: {ident.session_ref}"
    lines.append(
        Text(obs_line, style=_style_token("TEXT" if ident.observed_route else "MUTED", plain))
    )
    if ident.mismatch:
        lines.append(
            Text(
                "MISMATCH: selected route != observed reported_model",
                style=_style_token("ERROR", plain),
            )
        )
    if chain.is_empty:
        lines.append(
            Text("transition: none (no failures recorded)", style=_style_token("MUTED", plain))
        )
    else:
        token = _classify_summary_token(chain.summary)
        lines.append(Text(f"transition: {chain.summary}", style=_style_token(token, plain)))
        if state.expanded and chain.technical:
            lines.append(Text("technical:", style=_style_token("MUTED", plain)))
            for tline in chain.technical:
                lines.append(Text(f"  {tline}"))
        elif chain.technical:
            lines.append(
                Text("(press d for technical details)", style=_style_token("MUTED", plain))
            )
    body: RenderableType = Group(*lines)
    if plain:
        return body
    from verdict.design import panel

    return panel(body, title=f"detail: {nid}", mode=mode)


def identity_for_controller(view: Any, events: Sequence[Any]) -> IdentityView:
    """Selected vs observed identity for the planner role."""
    selected = getattr(view, "controller_route", "") or ""
    observed = getattr(view, "planner_observed_model", "") or ""
    session_ref = getattr(view, "planner_session_ref", "") or ""
    mismatch = bool(selected and observed and selected != observed)
    return IdentityView(selected, observed, session_ref, mismatch)


def identity_for_reviewer(view: Any, events: Sequence[Any]) -> IdentityView:
    """Selected vs observed identity for the reviewer role."""
    review = getattr(view, "review", None)
    selected = getattr(review, "route_id", "") if review else ""
    observed = getattr(review, "observed_model", "") if review else ""
    mismatch = bool(selected and observed and selected != observed)
    return IdentityView(selected, observed, "", mismatch)


def _render_controller_detail(
    view: Any,
    events: Sequence[Any],
    state: CockpitState,
    *,
    plain: bool = False,
    width: int = 100,
    mode: Any = None,
) -> RenderableType:
    """Detail panel for the controller (planner) role."""
    ident = identity_for_controller(view, events)
    ctrl_state = getattr(view, "controller_state", "") or "-"
    lines: list[Text] = [
        Text(f"role: controller  state: {ctrl_state}", style=_style_token("PRIMARY", plain)),
        Text(
            f"selected route: {ident.selected_route or 'not selected yet'}",
            style=_style_token("TEXT" if ident.selected_route else "MUTED", plain),
        ),
    ]
    obs_line = f"observed model: {ident.observed_route or 'not reported yet'}"
    if ident.session_ref:
        obs_line += f"  session: {ident.session_ref}"
    lines.append(
        Text(obs_line, style=_style_token("TEXT" if ident.observed_route else "MUTED", plain))
    )
    if ident.mismatch:
        lines.append(
            Text(
                "MISMATCH: selected route != observed reported_model",
                style=_style_token("ERROR", plain),
            )
        )
    body: RenderableType = Group(*lines)
    if plain:
        return body
    from verdict.design import panel as _panel

    return _panel(body, title="detail: controller", mode=mode)


def _render_reviewer_detail(
    view: Any,
    events: Sequence[Any],
    state: CockpitState,
    *,
    plain: bool = False,
    width: int = 100,
    mode: Any = None,
) -> RenderableType:
    """Detail panel for the reviewer role."""
    ident = identity_for_reviewer(view, events)
    review = getattr(view, "review", None)
    status = getattr(review, "status", "-") if review else "-"
    lines: list[Text] = [
        Text(f"role: reviewer  status: {status}", style=_style_token("PRIMARY", plain)),
        Text(
            f"selected route: {ident.selected_route or 'not selected yet'}",
            style=_style_token("TEXT" if ident.selected_route else "MUTED", plain),
        ),
        Text(
            f"observed model: {ident.observed_route or 'not reported yet'}",
            style=_style_token("TEXT" if ident.observed_route else "MUTED", plain),
        ),
    ]
    if ident.mismatch:
        lines.append(
            Text(
                "MISMATCH: selected route != observed reported_model",
                style=_style_token("ERROR", plain),
            )
        )
    body: RenderableType = Group(*lines)
    if plain:
        return body
    from verdict.design import panel as _panel

    return _panel(body, title="detail: reviewer", mode=mode)


def _classify_summary_token(summary: str) -> str:
    """Map an expected provider-error category into a semantic token."""
    lower = summary.lower()
    if "quota" in lower or "429" in lower or "rate_limited" in lower:
        return "COOLDOWN"
    if "overflow" in lower or "context_length" in lower:
        return "WARNING"
    if any(k in lower for k in ("upstream_temporary", "5xx", "timeout", "transport_temporary")):
        return "DEGRADED"
    if "failed" in lower:
        return "WARNING"
    if "replaced" in lower:
        return "INFO"
    return "MUTED"


def render_footer(plain: bool = False) -> RenderableType:
    hint = "? help  c context  r routing  p receipt  h health  q quit"
    if plain:
        return Text(hint)
    return Text(hint, style=TOKENS["MUTED"])


# ---------------------------------------------------------------------------
# Cockpit run loop
# ---------------------------------------------------------------------------


def dispatch_key(key: str, state: CockpitState, view: Any) -> bool:
    """Apply ``key`` to ``state``.  Returns True when the state changed."""
    if key in (KEY_UP, "k"):
        state.move(-1)
        return True
    if key in (KEY_DOWN, "j"):
        state.move(1)
        return True
    if key == KEY_ENTER:
        state.detail_open = True
        return True
    if key == KEY_ESC:
        if state.expanded:
            state.expanded = False
            return True
        if state.help_open:
            state.help_open = False
            return True
        if state.routing_open:
            state.routing_open = False
            return True
        if getattr(state, "context_open", False):
            state.context_open = False  # type: ignore[attr-defined]
            return True
        if state.detail_open:
            state.detail_open = False
            return True
        return False
    if key == KEY_HELP:
        state.help_open = not state.help_open
        return True
    if key == KEY_DETAILS:
        state.expanded = not state.expanded
        return True
    if key == KEY_QUIT:
        state.quit_requested = True
        return True
    if key == KEY_CONTEXT:
        open_context_view(state, view)  # BOD-278 extension point
        return True
    if key == KEY_ROUTING:
        open_routing_view(state, view)  # BOD-277 extension point
        return True
    return False


# Maximum render fps for the cockpit.  Bursts of events coalesce into one
# render per 100ms (state itself is applied to the view every poll cycle).
_MIN_FRAME_INTERVAL = 0.1


def run_cockpit(
    view: Any,
    events_source: Callable[[], list[Any]],
    render_dashboard: Callable[[Any, int, bool], RenderableType],
    *,
    console: Console,
    key_reader: KeyReader,
    plain: bool,
    poll_seconds: float = 0.05,
    max_iterations: int = 2000,
    stop_when_final: bool = True,
) -> CockpitState:
    """Drive the interactive cockpit.

    ``events_source`` is called each poll to return the full event list
    (the caller keeps the file path and de-duplicates internally).
    ``render_dashboard(view, width, plain)`` returns the shared cockpit
    body; this loop wraps it with selected-row / detail / help / footer.

    Every scripted-key exhaustion raises :class:`EOFError` from
    ``key_reader.read`` and terminates the loop cleanly.  A hard cap
    (``max_iterations``) prevents any accidental infinite loop.
    """
    state = CockpitState()
    seen: list[Any] = []
    last_render = 0.0

    def _compose(width: int) -> RenderableType:
        blocks: list[RenderableType] = [render_selected_row(view, state, plain)]
        blocks.append(render_dashboard(view, width, plain))
        if state.detail_open:
            blocks.append(render_detail_panel(view, seen, state, plain=plain, width=width))
        if state.routing_open:
            # BOD-277 render call — projection prepared by open_routing_view
            rview = (
                state.routing_from_run(view, seen)
                if state.routing_from_run is not None
                else state.routing_view
            )
            if rview is not None:
                nid = state.selected_id
                eval_idx = None
                if nid:
                    for i, ev in enumerate(getattr(rview, "evaluations", ())):
                        if getattr(ev, "node_id", "") == nid:
                            eval_idx = i
                            break
                if plain:
                    if state.routing_render_text is not None:
                        blocks.append(
                            Text(
                                state.routing_render_text(
                                    rview, width=width, evaluation_index=eval_idx
                                )
                            )
                        )
                else:
                    from verdict.design import presentation_mode as _pm

                    if state.routing_render is not None:
                        blocks.append(
                            state.routing_render(
                                rview, _pm(console, env=None), evaluation_index=eval_idx
                            )
                        )
        if getattr(state, "context_open", False):
            # BOD-278 render call — projection prepared by open_context_view
            cview = getattr(state, "context_view", None)
            if cview is not None:
                if plain:
                    text_fn = getattr(state, "context_render_text", None)
                    if text_fn is not None:
                        blocks.append(Text(text_fn(cview, width=width)))
                else:
                    from verdict.design import presentation_mode as _pm

                    render_fn = getattr(state, "context_render", None)
                    if render_fn is not None:
                        blocks.append(render_fn(cview, _pm(console, env=None)))
        if state.help_open:
            blocks.append(render_help(plain=plain))
        blocks.append(render_footer(plain=plain))
        return Group(*blocks)

    live: Live | None = None
    iterations = 0
    try:
        if not plain:
            live = Live(
                _compose(console.width or 100),
                console=console,
                refresh_per_second=10,  # coalesced max ~10 fps
                transient=False,
            )
            live.start(refresh=True)
        while iterations < max_iterations:
            iterations += 1
            # 1. Apply any newly appended events.
            fresh = events_source()
            if len(fresh) > len(seen):
                for e in fresh[len(seen) :]:
                    view.apply(e)
                seen = fresh
                # Refresh node order (preserves selection identity).
                state.sync_order(selectable_order_with_roles(view))

            # 2. Read one key with a short timeout.
            try:
                key = key_reader.read(timeout=poll_seconds)
            except EOFError:
                break
            changed = False
            if key is not None:
                changed = dispatch_key(key, state, view)
                if state.quit_requested:
                    break

            # 3. Coalesced render (max ~10 fps).
            now = time.monotonic()
            if live is not None and (changed or now - last_render >= _MIN_FRAME_INTERVAL):
                live.update(_compose(console.width or 100), refresh=True)
                last_render = now

            # 4. Stop when the run has reached a terminal outcome.
            if stop_when_final and getattr(view, "final", False):
                # Draw the terminal state once more, then exit.
                if live is not None:
                    live.update(_compose(console.width or 100), refresh=True)
                break
    finally:
        import contextlib

        if live is not None:
            with contextlib.suppress(Exception):
                live.stop()
        with contextlib.suppress(Exception):
            key_reader.close()
    return state


__all__ = [
    "KEY_CONTEXT",
    "KEY_DETAILS",
    "KEY_DOWN",
    "KEY_ENTER",
    "KEY_ESC",
    "KEY_HEALTH",
    "KEY_HELP",
    "KEY_LEFT",
    "KEY_QUIT",
    "KEY_RIGHT",
    "KEY_ROUTING",
    "KEY_UP",
    "ROLE_CONTROLLER",
    "ROLE_REVIEWER",
    "CockpitState",
    "IdentityView",
    "KeyReader",
    "ScriptedKeyReader",
    "TransitionChain",
    "_RealKeyReader",
    "dispatch_key",
    "identity_for",
    "identity_for_controller",
    "identity_for_reviewer",
    "is_role_row",
    "open_context_view",
    "open_routing_view",
    "render_detail_panel",
    "render_footer",
    "render_help",
    "render_selected_row",
    "run_cockpit",
    "selectable_order_with_roles",
    "submit_control",
    "transition_chain_for",
]
