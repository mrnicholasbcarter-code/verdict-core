"""BOD-177: Verdict-native live terminal view of one orchestration run.

:class:`RunView` is a pure projection over ``contracts.RunEvent``; :func:`render`
turns it into a compact Rich dashboard built on the existing Verdict design
tokens. Nothing here calls a provider or trusts event text: every external label
goes through :func:`verdict.terminal_ui.clean`. Plain mode (``NO_COLOR``, ``CI``,
non-TTY) is ASCII only and emits no colour.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from io import StringIO
from pathlib import Path
from typing import Any

from rich import box
from rich.console import Console, Group, RenderableType
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from verdict.orchestration.contracts import NodeState, RunEvent
from verdict.terminal_ui import TOKENS, clean

STAGES: tuple[str, ...] = (
    "GOAL",
    "UNDERSTAND",
    "CONTROLLER",
    "PLAN",
    "DAG",
    "SELECT",
    "HYDRATE",
    "WORKERS",
    "QUOTA/COOLDOWN",
    "FAILURE/REASSIGN",
    "VERIFY",
    "REVIEW",
)

# glyph key -> (rich glyph, ascii glyph, design token)
GLYPHS: Mapping[str, tuple[str, str, str]] = {
    "running": ("\u25cf", "*", "PRIMARY"),
    "validated": ("\u2713", "+", "SUCCESS"),
    "failed": ("\u2717", "x", "ERROR"),
    "reassigned": ("\u21bb", "~", "WARNING"),
    "planned": ("\u25cc", "o", "MUTED"),
    "blocked": ("\u25a0", "#", "ERROR"),
}
_STATE_GLYPH: Mapping[NodeState, str] = {
    NodeState.PLANNED: "planned",
    NodeState.ADMITTED: "planned",
    NodeState.DISPATCHED: "running",
    NodeState.RUNNING: "running",
    NodeState.TERMINAL_SUCCESS: "validated",
    NodeState.VALIDATED: "validated",
    NodeState.TERMINAL_FAILURE: "failed",
    NodeState.REJECTED: "failed",
    NodeState.BLOCKED: "blocked",
}
LADDER: tuple[str, ...] = ("discovered", "entitled", "healthy", "available", "eligible")


def _t(value: object, limit: int = 120) -> str:
    """Sanitized, whitespace-collapsed, length-bounded label."""
    out = " ".join(clean(value).split())
    return out if len(out) <= limit else out[: limit - 3] + "..."


def _i(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        return int(float(value))
    except ValueError:
        return None


def _f(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _seq(value: object) -> list[Any]:
    return list(value) if isinstance(value, (list, tuple)) else []


def _count(value: object) -> int:
    return len(value) if isinstance(value, (list, tuple)) else (_i(value) or 0)


def _moment(at: str) -> float | None:
    try:
        return datetime.fromisoformat(clean(at).strip().replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def provider_of(route_id: str) -> str:
    head, _, tail = _t(route_id, 64).partition("/")
    return head.strip().lower() if tail else ""


@dataclass
class NodeView:
    """Per-node projection: state, current route, attempt, and route history."""

    node_id: str
    objective: str = ""
    kind: str = ""
    state: NodeState = NodeState.PLANNED
    route_id: str = ""
    capacity_class: str = ""
    rank: int | None = None
    attempt: int = 0
    reassigned: bool = False
    history: list[tuple[str, str]] = field(default_factory=list)
    started_at: float | None = None
    elapsed_seconds: float | None = None
    context_files: int = 0
    prompt_bytes: int = 0
    truncated: bool = False
    budget_bytes: int = 0

    @property
    def provider(self) -> str:
        return provider_of(self.route_id)

    def glyph_key(self) -> str:
        if self.reassigned and self.state in {NodeState.PLANNED, NodeState.ADMITTED}:
            return "reassigned"
        return _STATE_GLYPH.get(self.state, "planned")

    def elapsed(self, now: float | None) -> str:
        seconds = self.elapsed_seconds
        if seconds is None and self.started_at is not None and now is not None:
            seconds = max(0.0, now - self.started_at)
        return "-" if seconds is None else f"{seconds:.0f}s"


@dataclass
class Cooldown:
    key: str
    scope: str
    category: str
    until: str


@dataclass
class Reassignment:
    node_id: str
    from_route: str
    to_route: str
    reason: str
    attempt: int | None = None


@dataclass
class Failure:
    node_id: str
    category: str
    action: str
    route_id: str
    evidence: str = ""
    fault_injected: bool = False


@dataclass
class CheckResult:
    label: str
    ok: bool
    detail: str = ""


@dataclass
class ReviewView:
    status: str = ""
    reviewer: str = ""
    route_id: str = ""
    blocking: int = 0
    findings: int = 0


class RunView:
    """Pure projection of a run event stream; ``apply`` is the only mutator."""

    def __init__(self) -> None:
        self.goal = ""
        self.understand: dict[str, Any] = {}
        self.topology = ""
        self.rationale: list[str] = []
        self.max_parallel: int | None = None
        self.layers: list[list[str]] = []
        self.nodes: dict[str, NodeView] = {}
        self.eligibility: dict[str, int] = {}
        self.cooldowns: dict[str, Cooldown] = {}
        self.reassignments: list[Reassignment] = []
        self.failures: list[Failure] = []
        self.barriers: list[CheckResult] = []
        self.verifications: list[CheckResult] = []
        self.integrations: list[CheckResult] = []
        self.controller: list[tuple[str, str]] = []
        self.controller_route = ""
        self.controller_state = ""
        self.review_independence = ""
        self.remediation_rounds = 0
        self.review: ReviewView | None = None
        self.outcome = ""
        self.reason = ""
        self.event_count = 0
        self.last_seq = -1
        self.now: float | None = None

    @classmethod
    def from_events(cls, events: Iterable[RunEvent | Mapping[str, Any]]) -> RunView:
        view = cls()
        for event in events:
            view.apply(event)
        return view

    @property
    def final(self) -> bool:
        return bool(self.outcome)

    def node(self, node_id: str) -> NodeView:
        key = _t(node_id, 64)
        node = self.nodes.get(key)
        if node is None:
            node = self.nodes[key] = NodeView(node_id=key)
        return node

    def apply(self, event: RunEvent | Mapping[str, Any]) -> None:
        record = event if isinstance(event, RunEvent) else RunEvent.from_dict(event)
        self.event_count += 1
        self.last_seq = record.seq
        moment = _moment(record.at)
        if moment is not None:
            self.now = moment
        handler = getattr(self, f"_on_{record.type}", None)
        if handler is not None:
            handler(_t(record.node_id, 64), dict(record.data))

    # --------------------------------------------------------- event handlers

    def _on_run_started(self, node_id: str, data: dict[str, Any]) -> None:
        self.goal = _t(data.get("goal", ""), 200)

    def _on_understand(self, node_id: str, data: dict[str, Any]) -> None:
        self.understand = {
            "goal_chars": _i(data.get("goal_chars")) or 0,
            "risk": _t(data.get("risk", "unknown"), 24) or "unknown",
            "proof_requirements": [_t(item, 60) for item in _seq(data.get("proof_requirements"))],
            "scope": _t(data.get("scope", ""), 90),
        }

    def _on_plan_started(self, node_id: str, data: dict[str, Any]) -> None:
        route = _t(data.get("route_id", "unassigned"), 64)
        self.controller_route = route
        self.controller_state = "PLANNING"
        self.controller.append(("PLANNING", f"frontier decomposition on {route}"))

    def _on_plan_ready(self, node_id: str, data: dict[str, Any]) -> None:
        for raw in _seq(data.get("nodes")):
            if isinstance(raw, Mapping):
                node = self.node(str(raw.get("node_id", "")))
                node.objective = _t(raw.get("objective", ""), 90)
                node.kind = _t(raw.get("kind", ""), 24)
        self.layers = [
            [_t(item, 64) for item in layer]
            for layer in _seq(data.get("layers"))
            if isinstance(layer, Sequence) and not isinstance(layer, (str, bytes))
        ]
        self._on_topology(node_id, data)

    def _on_topology(self, node_id: str, data: dict[str, Any]) -> None:
        if data.get("topology"):
            self.topology = _t(data["topology"], 40)
        if data.get("max_parallel") is not None:
            self.max_parallel = _i(data["max_parallel"])
        raw = data.get("rationale")
        for item in _seq(raw) or ([raw] if raw else []):
            self._note(_t(item, 90))

    def _note(self, line: str) -> None:
        if line and line not in self.rationale:
            self.rationale.append(line)

    def _on_eligibility(self, node_id: str, data: dict[str, Any]) -> None:
        for key in LADDER:
            value = _i(data.get(key))
            if value is not None:
                self.eligibility[key] = value
        if not node_id:
            return
        node = self.node(node_id)
        node.route_id = _t(data.get("selected", ""), 64) or node.route_id
        node.capacity_class = _t(data.get("capacity_class", ""), 24) or node.capacity_class
        rank = _i(data.get("rank"))
        node.rank = node.rank if rank is None else rank

    def _on_selection(self, node_id: str, data: dict[str, Any]) -> None:
        node = self.node(node_id)
        node.route_id = _t(data.get("route_id", ""), 64)
        node.capacity_class = _t(data.get("capacity_class", ""), 24)
        node.rank = _i(data.get("rank"))
        node.attempt = _i(data.get("attempt")) or max(node.attempt, 1)

    def _on_node_state(self, node_id: str, data: dict[str, Any]) -> None:
        node = self.node(node_id)
        try:
            node.state = NodeState(_t(data.get("state", ""), 32))
        except ValueError:
            return
        node.route_id = _t(data.get("route_id", ""), 64) or node.route_id
        attempt = _i(data.get("attempt"))
        node.attempt = node.attempt if attempt is None else attempt
        if node.state is NodeState.RUNNING and node.started_at is None:
            node.started_at = self.now

    def _on_dispatch(self, node_id: str, data: dict[str, Any]) -> None:
        node = self.node(node_id)
        node.route_id = _t(data.get("route_id", ""), 64) or node.route_id
        node.attempt = _i(data.get("attempt")) or max(node.attempt, 1)
        node.started_at, node.elapsed_seconds = self.now, None

    def _on_hydrate(self, node_id: str, data: dict[str, Any]) -> None:
        node = self.node(node_id)
        node.context_files = _count(data.get("context_files"))
        node.prompt_bytes = _i(data.get("prompt_bytes")) or 0
        node.truncated = bool(data.get("truncated"))
        node.budget_bytes = _i(data.get("budget_bytes")) or node.budget_bytes

    def _on_terminal(self, node_id: str, data: dict[str, Any]) -> None:
        node = self.node(node_id)
        route = _t(data.get("route_id", ""), 64) or node.route_id
        node.history.append((route, "ok" if data.get("ok") else "failed"))
        duration = _f(data.get("duration_seconds"))
        if duration is None and node.started_at is not None and self.now is not None:
            duration = max(0.0, self.now - node.started_at)
        node.elapsed_seconds = duration

    def _on_failure(self, node_id: str, data: dict[str, Any]) -> None:
        self.failures.append(
            Failure(
                node_id,
                _t(data.get("category", "unknown"), 40),
                _t(data.get("action", ""), 40),
                _t(data.get("route_id", ""), 64),
                _t(data.get("evidence", ""), 90),
                bool(data.get("fault_injected")),
            )
        )

    def _on_cooldown(self, node_id: str, data: dict[str, Any]) -> None:
        key = _t(data.get("key", ""), 64)
        self.cooldowns[key] = Cooldown(
            key,
            _t(data.get("scope", ""), 24),
            _t(data.get("category", ""), 40),
            _t(data.get("until", ""), 32),
        )

    def _on_reassign(self, node_id: str, data: dict[str, Any]) -> None:
        node = self.node(node_id)
        target = _t(data.get("to_route", ""), 64)
        attempt = _i(data.get("attempt"))
        source = _t(data.get("from_route", ""), 64) or node.route_id
        self.reassignments.append(
            Reassignment(node.node_id, source, target, _t(data.get("reason", ""), 70), attempt)
        )
        node.reassigned = True
        node.route_id = target or node.route_id
        node.attempt = node.attempt if attempt is None else attempt

    def _on_verify(self, node_id: str, data: dict[str, Any]) -> None:
        exit_code = _i(data.get("exit_code"))
        detail = _t(data.get("command", ""), 70)
        if exit_code is not None:
            detail = f"{detail} (exit {exit_code})".strip()
        self.verifications.append(CheckResult(node_id or "run", bool(data.get("ok")), detail))

    def _on_barrier(self, node_id: str, data: dict[str, Any]) -> None:
        self.barriers.append(
            CheckResult(
                _t(data.get("name", "barrier"), 40),
                bool(data.get("ok")),
                _t(data.get("detail", ""), 70),
            )
        )

    def _on_integrate(self, node_id: str, data: dict[str, Any]) -> None:
        detail = f"{_count(data.get('commits'))} commit(s)"
        self.integrations.append(CheckResult(node_id or "integrate", bool(data.get("ok")), detail))

    def _on_review(self, node_id: str, data: dict[str, Any]) -> None:
        self.review = ReviewView(
            _t(data.get("status", ""), 24),
            _t(data.get("reviewer", ""), 64),
            _t(data.get("route_id", ""), 64),
            _i(data.get("blocking")) or 0,
            _count(data.get("findings")),
        )

    def _on_remediation(self, node_id: str, data: dict[str, Any]) -> None:
        self.remediation_rounds = _i(data.get("round")) or self.remediation_rounds + 1

    def _on_controller(self, node_id: str, data: dict[str, Any]) -> None:
        state = _t(data.get("state", ""), 32)
        detail = _t(data.get("detail", ""), 70)
        if state == "REVIEW_INDEPENDENCE":
            excluded = ", ".join(_t(r, 48) for r in _seq(data.get("excluded_routes")))
            level = _t(data.get("level", ""), 16)
            self.review_independence = f"{level}-level; excluded implementers: {excluded or 'none'}"
            if data.get("reviewer_route"):
                self.review_independence += f"; reviewer {_t(data.get('reviewer_route'), 48)}"
            return
        route = _t(data.get("route_id", ""), 64)
        if route:
            self.controller_route = route
        self.controller_state = state or self.controller_state
        self.controller.append((state, detail + (f" [{route}]" if route else "")))

    def _on_run_finished(self, node_id: str, data: dict[str, Any]) -> None:
        self.outcome = _t(data.get("outcome", ""), 24).upper()
        self.reason = _t(data.get("reason", ""), 140)

    # -------------------------------------------------------------- summaries

    def ladder(self) -> str:
        if not self.eligibility:
            return "no eligibility evidence yet"
        return " > ".join(
            f"{k.upper()} {self.eligibility[k]}" for k in LADDER if k in self.eligibility
        )

    def counts(self) -> dict[str, int]:
        totals: dict[str, int] = {}
        for node in self.nodes.values():
            totals[node.glyph_key()] = totals.get(node.glyph_key(), 0) + 1
        return totals


# ------------------------------------------------------------------ narration

# event type -> (stage tag, template rendered against the context below)
_LINES: Mapping[str, tuple[str, str]] = {
    "run_started": ("GOAL", "{goal}"),
    "understand": (
        "UNDERSTAND",
        "goal_chars={goal_chars} risk={risk} scope={scope} proof={proof_requirements}",
    ),
    "plan_started": ("PLAN", "planning on {route}"),
    "hydrate": (
        "HYDRATE",
        "{node} prompt {prompt_kb}KB context {context_files} file(s){truncated_note}",
    ),
    "plan_ready": ("DAG", "{nodes} node(s) in {layers} layer(s) ({topology})"),
    "topology": ("PLAN", "topology {topology} max_parallel={max_parallel}"),
    "eligibility": ("SELECT", "{node} ladder {ladder}"),
    "selection": ("SELECT", "{node} -> {route}{rank}"),
    "node_state": ("WORKERS", "{node} {state}"),
    "dispatch": ("WORKERS", "{node} dispatch {route} attempt {attempt}"),
    "heartbeat": ("WORKERS", "{node} heartbeat {detail}"),
    "terminal": ("WORKERS", "{node} terminal {ok} on {route}{seconds}"),
    "failure": ("FAILURE/REASSIGN", "{node} {category} -> {action} on {route}"),
    "cooldown": ("QUOTA/COOLDOWN", "{key} ({scope}) {category} until {until}"),
    "reassign": ("FAILURE/REASSIGN", "{node} {from_route} -> {to_route} ({reason})"),
    "verify": ("VERIFY", "{node} {passfail} {command}{exit}"),
    "barrier": ("VERIFY", "barrier {name} {ok} {detail}"),
    "integrate": ("VERIFY", "integrate {ok} {commits} commit(s)"),
    "review": (
        "REVIEW",
        "{status} by {reviewer} on {route} ({blocking} blocking / {findings} findings)",
    ),
    "remediation": ("REVIEW", "remediation round {round}"),
    "controller": ("WORKERS", "controller {state} {detail}"),
    "run_finished": ("{outcome}", "{outcome} {reason}"),
}


def _context(node_id: str, data: Mapping[str, Any]) -> dict[str, str]:
    """Sanitized, display-ready values for every template placeholder."""
    rank = _i(data.get("rank"))
    seconds = _f(data.get("duration_seconds"))
    exit_code = _i(data.get("exit_code"))
    prompt_bytes = _i(data.get("prompt_bytes")) or 0
    detail = ", ".join(
        x
        for x in (_t(data.get("capacity_class", ""), 24), "" if rank is None else f"rank {rank}")
        if x
    )
    context = {
        "node": node_id or "run",
        "goal": _t(data.get("goal", ""), 140),
        "route": _t(data.get("route_id", ""), 64) or "unassigned",
        "from_route": _t(data.get("from_route", ""), 64) or "unassigned",
        "to_route": _t(data.get("to_route", ""), 64) or "unassigned",
        "nodes": str(_count(data.get("nodes"))),
        "layers": str(_count(data.get("layers"))),
        "topology": _t(data.get("topology", "unknown"), 40),
        "max_parallel": str(_i(data.get("max_parallel")) or 1),
        "ladder": " ".join(f"{k}={_i(data.get(k))}" for k in LADDER if data.get(k) is not None),
        "rank": f" ({detail})" if detail else "",
        "state": _t(data.get("state", "UNKNOWN"), 32),
        "attempt": str(_i(data.get("attempt")) or 1),
        "ok": "ok" if data.get("ok") else "failed",
        "passfail": "pass" if data.get("ok") else "fail",
        "seconds": "" if seconds is None else f" in {seconds:.0f}s",
        "category": _t(data.get("category", "unknown"), 40),
        "action": _t(data.get("action", "BLOCK"), 40),
        "key": _t(data.get("key", ""), 64),
        "scope": _t(data.get("scope", ""), 90),
        "until": _t(data.get("until", "unknown"), 32),
        "reason": _t(data.get("reason", ""), 140),
        "command": _t(data.get("command", ""), 70),
        "exit": "" if exit_code is None else f" (exit {exit_code})",
        "name": _t(data.get("name", "barrier"), 40),
        "detail": _t(data.get("detail", ""), 70),
        "status": _t(data.get("status", "ERROR"), 24),
        "reviewer": _t(data.get("reviewer", "unknown"), 64) or "unknown",
        "blocking": str(_i(data.get("blocking")) or 0),
        "findings": str(_count(data.get("findings"))),
        "commits": str(_count(data.get("commits"))),
        "round": str(_i(data.get("round")) or 1),
        "outcome": _t(data.get("outcome", "BLOCKED"), 24).upper(),
        "goal_chars": str(_i(data.get("goal_chars")) or 0),
        "risk": _t(data.get("risk", "unknown"), 24) or "unknown",
        "proof_requirements": ", ".join(
            _t(item, 60) for item in _seq(data.get("proof_requirements"))
        ),
        "context_files": str(_count(data.get("context_files"))),
        "prompt_kb": f"{prompt_bytes / 1024:.1f}",
        "truncated_note": " [truncated]" if data.get("truncated") else "",
    }
    return context


def event_line(event: RunEvent | Mapping[str, Any]) -> str:
    """One sanitized narrative line per event, prefixed with its stage tag."""
    record = event if isinstance(event, RunEvent) else RunEvent.from_dict(event)
    context = _context(_t(record.node_id, 48), dict(record.data))
    tag, template = _LINES.get(record.type, ("WORKERS", "{node} " + _t(record.type, 40)))
    body = " ".join(template.format(**context).split())
    return f"[{tag.format(**context)}] {body}".strip()


# ------------------------------------------------------------------ rendering


def _style(token: str, plain: bool) -> str:
    return "" if plain else TOKENS[token]


def _glyph(key: str, plain: bool) -> tuple[str, str]:
    rich_glyph, ascii_glyph, token = GLYPHS[key]
    return (ascii_glyph if plain else rich_glyph), token


def _section(title: str, body: RenderableType, *, plain: bool, width: int) -> RenderableType:
    if plain:
        return Group(Text(title), body, Text(""))
    return Panel(
        body,
        title=Text(title, style=TOKENS["SECONDARY"]),
        border_style=TOKENS["BORDER"],
        box=box.ROUNDED,
        padding=(0, 1),
        width=width,
    )


def _lines(items: Sequence[str], empty: str) -> Text:
    return Text("\n".join(item for item in items if item) if items else empty)


def _workers(view: RunView, plain: bool) -> Table:
    table = Table(
        box=None if plain else box.SIMPLE,
        pad_edge=False,
        show_edge=False,
        header_style="" if plain else TOKENS["MUTED"],
    )
    for name in ("node", "state", "route", "provider", "att", "elapsed", "history"):
        table.add_column(name, overflow="fold")
    for node in view.nodes.values():
        glyph, token = _glyph(node.glyph_key(), plain)
        table.add_row(
            Text(node.node_id),
            Text(f"{glyph} {node.state.value}", style=_style(token, plain)),
            Text(short_route(node.route_id) if node.route_id else "-"),
            Text(node.provider or "-"),
            Text(str(node.attempt)),
            Text(node.elapsed(view.now)),
            Text(_history(node, plain)),
        )
    if not view.nodes:
        table.add_row(*(Text(x) for x in ("-", "no workers yet", "-", "-", "-", "-", "-")))
    return table


def short_route(route_id: str) -> str:
    """cc/claude-haiku-4-5-20251001 -> haiku-4-5; cx/gpt-5.5 -> gpt-5.5 (display only)."""
    tail = route_id.split("/")[-1] or "merge"
    tail = tail.removeprefix("claude-")
    parts = tail.split("-")
    if len(parts) > 1 and parts[-1].isdigit() and len(parts[-1]) >= 8:
        parts = parts[:-1]
    return "-".join(parts)[:18]


def _history(node: NodeView, plain: bool) -> str:
    ok, bad, arrow = ("+", "x", ">") if plain else ("✓", "✗", "→")
    steps = [f"{short_route(r)}{ok if o == 'ok' else bad}" for r, o in node.history]
    return f" {arrow} ".join(steps[-4:]) if steps else "-"


def _banner(view: RunView, plain: bool, width: int) -> RenderableType:
    token = "SUCCESS" if view.outcome == "COMPLETE" else "ERROR"
    if plain:
        return Group(Text(f"{view.outcome} {view.reason}".strip()), Text(""))
    text = Text(view.outcome, style=TOKENS[token])
    if view.reason:
        text.append(f"  {view.reason}", style=TOKENS["MUTED"])
    return Panel(text, border_style=TOKENS[token], box=box.HEAVY, padding=(0, 1), width=width)


def _plan_lines(view: RunView) -> list[str]:
    return [
        f"topology: {view.topology or 'unknown'}",
        f"max parallel: {view.max_parallel or 1}",
        *(f"rationale: {line}" for line in view.rationale),
    ]


def _understand_lines(view: RunView) -> list[str]:
    profile = view.understand
    if not profile:
        return []
    return [
        f"goal_chars: {profile.get('goal_chars', 0)}",
        f"risk: {profile.get('risk', 'unknown')}",
        f"scope: {profile.get('scope', '')}",
        "proof requirements: " + ", ".join(profile.get("proof_requirements", []) or ["-"]),
    ]


def _hydrate_lines(view: RunView) -> list[str]:
    return [
        f"{n.node_id}: {n.prompt_bytes / 1024:.1f}KB prompt, "
        f"{n.context_files} context file(s)"
        f"{' [truncated]' if n.truncated else ''}"
        for n in view.nodes.values()
        if n.prompt_bytes or n.context_files
    ]


def _select_lines(view: RunView) -> list[str]:
    return [
        f"ladder: {view.ladder()}",
        *(
            f"{n.node_id} -> {n.route_id}  "
            f"[{n.capacity_class or 'unknown'} #{'-' if n.rank is None else n.rank}]"
            for n in view.nodes.values()
            if n.route_id
        ),
    ]


def _trouble_lines(view: RunView) -> list[str]:
    lines = [
        f"{f.node_id or 'run'}: {f.category} -> {f.action}"
        f"{' on ' + f.route_id if f.route_id else ''}"
        f"{' [injected]' if f.fault_injected else ''}"
        for f in view.failures
    ]
    lines += [
        f"{r.node_id}: {r.from_route or 'unassigned'} -> {r.to_route or 'unassigned'} ({r.reason})"
        for r in view.reassignments
    ]
    return lines


def _review_lines(view: RunView) -> list[str]:
    review = view.review
    if review is None:
        return []
    lines = [
        f"{review.status} by {review.reviewer or 'unknown'} on "
        f"{review.route_id or 'unassigned'} "
        f"({review.blocking} blocking / {review.findings} findings)"
    ]
    if view.review_independence:
        lines.append(f"independence: {view.review_independence}")
    if view.remediation_rounds:
        lines.append(f"remediation rounds: {view.remediation_rounds}")
    return lines


def _controller_lines(view: RunView) -> list[str]:
    head = []
    if view.controller_route or view.controller_state:
        head.append(
            f"frontier controller: {view.controller_route or '-'} "
            f"[{view.controller_state or 'UNKNOWN'}]"
        )
    return head + [f"{state} {detail}".strip() for state, detail in view.controller]


def render(view: RunView, *, width: int = 100, plain: bool = False) -> RenderableType:
    """Run dashboard: one screen, one labelled block per orchestration stage.

    Layout (wide terminals): header, then GOAL, then CONTROLLER | PLAN+DAG side by
    side, SELECT, WORKERS (full width), QUOTA/COOLDOWN | FAILURE/REASSIGN side by
    side, VERIFY | REVIEW side by side, then the COMPLETE/BLOCKED banner. Narrow or
    plain terminals stack the same blocks vertically.
    """
    checks_ok = [c for c in (*view.verifications, *view.barriers, *view.integrations) if c.ok]
    checks_bad = [c for c in (*view.verifications, *view.barriers, *view.integrations) if not c.ok]
    verify_lines = (
        [f"{len(checks_ok)} passed, {len(checks_bad)} failed"]
        + [f"FAIL {c.label} {c.detail}".strip() for c in checks_bad[-4:]]
        + [f"PASS {c.label} {c.detail}".strip() for c in view.verifications if c.ok][-4:]
    )
    dag = [f"L{i}: " + ", ".join(layer) for i, layer in enumerate(view.layers)]
    plan = [*_plan_lines(view)[:2], "", *dag] if dag else _plan_lines(view)
    blocks: dict[str, RenderableType] = {
        "GOAL": Text(view.goal or "no goal recorded"),
        "UNDERSTAND": _lines(_understand_lines(view), "no task profile recorded yet"),
        "CONTROLLER": _lines(_controller_lines(view)[-6:], "no controller events"),
        "PLAN / DAG": _lines(plan, "no plan recorded"),
        "SELECT": _lines(_select_lines(view), "nothing selected yet"),
        "HYDRATE": _lines(_hydrate_lines(view), "no prompt hydration recorded yet"),
        "WORKERS": _workers(view, plain),
        "QUOTA/COOLDOWN": _lines(
            [
                f"{c.key} [{c.scope}] {c.category} until {c.until[11:19] or '-'}"
                for c in view.cooldowns.values()
            ],
            "no active cooldowns",
        ),
        "FAILURE/REASSIGN": _lines(_trouble_lines(view)[-8:], "no failures recorded"),
        "VERIFY": _lines(verify_lines, "no verification evidence"),
        "REVIEW": _lines(_review_lines(view), "no review recorded"),
    }
    out: list[RenderableType] = [_header(view, plain, width)]
    wide = not plain and width >= 110
    rows: list[tuple[str, ...]] = (
        [
            ("GOAL", "UNDERSTAND"),
            ("CONTROLLER", "PLAN / DAG"),
            ("SELECT", "HYDRATE"),
            ("WORKERS",),
            ("QUOTA/COOLDOWN", "FAILURE/REASSIGN"),
            ("VERIFY", "REVIEW"),
        ]
        if wide
        else [(name,) for name in blocks]
    )
    for row in rows:
        if len(row) == 1:
            out.append(_section(row[0], blocks[row[0]], plain=plain, width=width))
            continue
        half = width // 2
        grid = Table.grid(expand=False)
        grid.add_column(width=half)
        grid.add_column(width=width - half)
        grid.add_row(
            *(
                _section(name, blocks[name], plain=plain, width=w)
                for name, w in zip(row, (half, width - half), strict=True)
            )
        )
        out.append(grid)
    if view.final:
        out.append(_banner(view, plain, width))
    return Group(*out)


def _header(view: RunView, plain: bool, width: int) -> RenderableType:
    counts = view.counts()
    status = view.outcome or (
        "RUNNING"
        if any(n.state.value in {"RUNNING", "DISPATCHED", "ADMITTED"} for n in view.nodes.values())
        else "PLANNING"
    )
    summary = (
        f"{status}  |  nodes {len(view.nodes)}  running {counts.get('running', 0)}  "
        f"validated {counts.get('validated', 0)}  failed {counts.get('failed', 0)}  "
        f"reassignments {len(view.reassignments)}  cooldowns {len(view.cooldowns)}"
    )
    if plain:
        return Group(Text("VERDICT  autonomous control plane"), Text(summary), Text(""))
    title = Text("VERDICT", style=TOKENS["PRIMARY"])
    title.append("  autonomous control plane", style=TOKENS["SECONDARY"])
    line = Text(
        summary,
        style=TOKENS["ACCENT"]
        if not view.outcome
        else TOKENS["SUCCESS" if view.outcome == "COMPLETE" else "ERROR"],
    )
    return Panel(
        Group(title, line),
        border_style=TOKENS["PRIMARY"],
        box=box.HEAVY,
        padding=(0, 1),
        width=width,
    )


def render_text(
    events: Iterable[RunEvent | Mapping[str, Any]], width: int = 100, plain: bool = True
) -> str:
    """Render a whole event stream to text, for tests and receipts."""
    console = Console(
        file=StringIO(),
        width=width,
        height=200,  # rich ignores an explicit width on TERM=dumb unless height is set too
        record=True,
        force_terminal=not plain,
        color_system=None if plain else "truecolor",
        no_color=plain,
        markup=False,
        highlight=False,
        legacy_windows=False,
    )
    console.print(render(RunView.from_events(events), width=width, plain=plain))
    return console.export_text(styles=not plain)


def plain_mode(console: Console | None = None) -> bool:
    """Same fallback rules as :class:`verdict.terminal_ui.TerminalUI`."""
    source = console or Console()
    return (
        not source.is_terminal
        or "NO_COLOR" in os.environ
        or os.getenv("TERM") == "dumb"
        or bool(os.getenv("CI"))
        or os.getenv("VERDICT_PLAIN") == "1"
    )


def read_events(path: Path) -> list[RunEvent]:
    """Parse a JSONL events file, skipping blank, malformed, or unknown rows."""
    if not path.exists():
        return []
    events: list[RunEvent] = []
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
            if isinstance(payload, dict):
                events.append(RunEvent.from_dict(payload))
        except (ValueError, KeyError, TypeError):
            continue
    return events


def _event_seq(event: RunEvent | Mapping[str, Any]) -> int:
    value = event.seq if isinstance(event, RunEvent) else event.get("seq", 0)
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def follow(
    events_path: Path,
    *,
    console: Console | None = None,
    refresh_hz: int = 4,
    stop_when_final: bool = True,
    poll_seconds: float = 0.25,
    max_polls: int | None = None,
    start_seq: int = 0,
) -> RunView:
    """Live-tail a JSONL events file; plain mode prints one narrative line per event.

    ``start_seq`` skips events from earlier controller lives of a resumed run, so
    a previous ``run_finished`` cannot end (or mislabel) the current live view.
    """
    target = console or Console()
    plain = plain_mode(console)
    width = target.width or 100
    view, seen, polls = RunView(), 0, 0
    if start_seq:
        seen = sum(1 for e in read_events(events_path) if _event_seq(e) <= start_seq)
    live = (
        None
        if plain
        else Live(render(view, width=width), console=target, refresh_per_second=max(1, refresh_hz))
    )
    if live is not None:
        live.start(refresh=True)
    try:
        while max_polls is None or polls < max_polls:
            polls += 1
            events = read_events(events_path)
            for event in events[seen:]:
                view.apply(event)
                if plain:
                    target.print(event_line(event), markup=False, highlight=False)
            seen = len(events)
            if live is not None:
                live.update(render(view, width=width), refresh=True)
            if stop_when_final and view.final:
                break
            time.sleep(max(0.0, poll_seconds))
    finally:
        if live is not None:
            live.stop()
    return view
