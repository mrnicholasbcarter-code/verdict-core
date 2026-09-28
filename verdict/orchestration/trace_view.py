"""Trace projection (BOD-279): ordered, navigable timeline of run steps.

``trace_view(run_dir)`` builds a :class:`TraceView` from the recorded event log
and receipt.  It reuses :func:`context_view` and :func:`routing_view` for their
respective sections rather than re-deriving.

Every step has a ``kind`` drawn from a fixed vocabulary, a ``seq`` reference
into the event log, and an ``evidence`` dict with the raw data.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from verdict.orchestration.context_view import context_view as _context_view
from verdict.orchestration.contracts import RunEvent
from verdict.orchestration.receipt import EventLog
from verdict.orchestration.routing_view import routing_view as _routing_view

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SCHEMA_VERSION = "trace-view-v1"

# Canonical step kinds in presentation order (within a node / globally).
STEP_KINDS: tuple[str, ...] = (
    "request",
    "classification",
    "plan",
    "context",
    "routing",
    "selection",
    "dispatch",
    "terminal",
    "failure",
    "cooldown",
    "reassign",
    "verify",
    "barrier",
    "integrate",
    "review",
    "receipt",
    "run_finished",
)

# Map event types to step kinds.
_EVENT_TO_KIND: dict[str, str] = {
    "run_started": "request",
    "understand": "classification",
    "plan_started": "plan",
    "plan_ready": "plan",
    "plan_repair_started": "plan",
    "plan_repair_terminal": "plan",
    "topology": "plan",
    "hydrate": "context",
    "rehydrate": "context",
    "eligibility": "routing",
    "selection": "selection",
    "dispatch": "dispatch",
    "terminal": "terminal",
    "failure": "failure",
    "cooldown": "cooldown",
    "reassign": "reassign",
    "verify": "verify",
    "barrier": "barrier",
    "integrate": "integrate",
    "review": "review",
    "review_attempt": "review",
    "run_finished": "run_finished",
}


# ---------------------------------------------------------------------------
# Public data types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TraceStep:
    """One step in the timeline."""

    seq: int
    at: str
    kind: str
    node_id: str
    evidence: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "at": self.at,
            "kind": self.kind,
            "node_id": self.node_id,
            "evidence": dict(self.evidence),
        }


@dataclass
class TraceView:
    """An ordered, navigable timeline of steps for a run."""

    schema_version: str
    run_id: str
    goal: str
    steps: tuple[TraceStep, ...]
    context_view: Mapping[str, Any] | None = None
    routing_view: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        # Validate ordering
        seqs = [s.seq for s in self.steps]
        assert seqs == sorted(seqs), "steps must be sorted by seq"

    def steps_for_node(self, node_id: str) -> list[TraceStep]:
        """Return steps for a specific node, in seq order."""
        return [s for s in self.steps if s.node_id == node_id]

    def steps_by_kind(self, kind: str) -> list[TraceStep]:
        """Return all steps of a given kind, in seq order."""
        return [s for s in self.steps if s.kind == kind]

    def node_ids(self) -> list[str]:
        """Distinct node_ids in first-appearance order."""
        seen: dict[str, None] = {}
        for s in self.steps:
            if s.node_id and s.node_id not in seen:
                seen[s.node_id] = None
        return list(seen)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "goal": self.goal,
            "steps": [s.to_dict() for s in self.steps],
        }
        if self.context_view is not None:
            d["context_view"] = dict(self.context_view)
        if self.routing_view is not None:
            d["routing_view"] = dict(self.routing_view)
        return d

    def to_json(self, **kwargs: Any) -> str:
        return json.dumps(self.to_dict(), **kwargs)


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------


def _build_evidence(event: RunEvent) -> dict[str, Any]:
    """Extract evidence dict from an event."""
    ev: dict[str, Any] = {"type": event.type}
    data = dict(event.data)
    if data:
        ev.update(data)
    return ev


def _steps_from_events(events: Sequence[RunEvent]) -> list[TraceStep]:
    """Convert events to trace steps, filtering to known kinds."""
    steps: list[TraceStep] = []
    for event in events:
        kind = _EVENT_TO_KIND.get(event.type)
        if kind is None:
            continue  # skip node_state, heartbeat, controller, etc.
        steps.append(TraceStep(
            seq=event.seq,
            at=event.at,
            kind=kind,
            node_id=event.node_id,
            evidence=_build_evidence(event),
        ))
    # Already sorted by seq because events are append-only ordered
    steps.sort(key=lambda s: s.seq)
    return steps


def trace_view(run_dir: Path | str) -> TraceView:
    """Build a :class:`TraceView` from a run directory.

    Parameters
    ----------
    run_dir:
        Path to the run directory containing ``events.jsonl`` (and optionally
        ``receipt.json``).

    Returns
    -------
    TraceView
        Deterministic projection of the run timeline.
    """
    run_dir = Path(run_dir)
    events_path = run_dir / "events.jsonl"
    log = EventLog(events_path)
    events = log.read()

    # Extract run metadata from run_started event
    run_id = ""
    goal = ""
    for ev in events:
        if ev.type == "run_started":
            run_id = str(ev.data.get("run_id", ""))
            goal = str(ev.data.get("goal", ""))
            break

    steps = _steps_from_events(events)

    # Reuse context_view and routing_view
    ctx_v: Mapping[str, Any] | None = None
    try:
        cv = _context_view(events)
        ctx_v = cv.to_dict()
    except Exception:
        pass

    rout_v: Mapping[str, Any] | None = None
    try:
        event_dicts = [e.to_dict() for e in events]
        rv = _routing_view(event_dicts)
        rout_v = rv.to_dict()
    except Exception:
        pass

    return TraceView(
        schema_version=SCHEMA_VERSION,
        run_id=run_id,
        goal=goal,
        steps=tuple(steps),
        context_view=ctx_v,
        routing_view=rout_v,
    )


def trace_view_from_events(
    events: Sequence[RunEvent],
    *,
    run_id: str = "",
    goal: str = "",
) -> TraceView:
    """Build a :class:`TraceView` from in-memory events (for testing).

    Parameters
    ----------
    events:
        Sequence of :class:`RunEvent` objects.
    run_id:
        Override run_id (extracted from run_started if empty).
    goal:
        Override goal (extracted from run_started if empty).
    """
    rid = run_id
    g = goal
    for ev in events:
        if ev.type == "run_started":
            if not rid:
                rid = str(ev.data.get("run_id", ""))
            if not g:
                g = str(ev.data.get("goal", ""))
            break

    steps = _steps_from_events(events)

    # Build context/routing views from events
    ctx_v: Mapping[str, Any] | None = None
    try:
        cv = _context_view(events)
        ctx_v = cv.to_dict()
    except Exception:
        pass

    rout_v: Mapping[str, Any] | None = None
    try:
        event_dicts = [e.to_dict() for e in events]
        rv = _routing_view(event_dicts)
        rout_v = rv.to_dict()
    except Exception:
        pass

    return TraceView(
        schema_version=SCHEMA_VERSION,
        run_id=rid,
        goal=g,
        steps=tuple(steps),
        context_view=ctx_v,
        routing_view=rout_v,
    )
