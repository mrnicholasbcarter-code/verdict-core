"""
context_view.py — BOD-278 lane 1: UI-independent context provenance/budget projection.

Builds a :class:`ContextView` from recorded :class:`RunEvent` objects (or a run
directory).  Never re-reads repo files, never re-runs hydration.  Unknown values
stay unknown — nothing is inferred or fabricated.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from verdict.contracts import redact_contract_secrets
from verdict.orchestration.contracts import RunEvent
from verdict.orchestration.receipt import EventLog

# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------

SourceState = Literal["included", "excluded", "truncated", "deduplicated", "compressed", "unknown"]

_SCHEMA_VERSION = "1"


@dataclass(frozen=True)
class SourceEntry:
    """One file/chunk source as recorded in a hydrate event."""

    path: str
    bytes: int | None
    state: SourceState
    reason: str | None
    truncated_at: int | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "bytes": self.bytes,
            "path": self.path,
            "reason": self.reason,
            "state": self.state,
            "truncated_at": self.truncated_at,
        }


@dataclass
class NodeContextView:
    """Context view for a single work-graph node."""

    node_id: str
    budget_bytes: int | None
    sources: list[SourceEntry] | None  # None = sources not recorded (legacy event)
    prompt_bytes: int | None

    # totals by state — computed from sources; None when sources unknown
    totals_by_state: dict[str, int] | None  # state -> sum of bytes (None for unknown bytes)

    # budget pressure = prompt_bytes / budget_bytes; None when either is unknown
    budget_pressure: float | None

    # compression: "not_performed" when orchestrate engine ran (it never compresses),
    # or the algorithm name if a future engine compresses, or None for legacy events
    # that pre-date this field.
    compression: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "budget_bytes": self.budget_bytes,
            "budget_pressure": self.budget_pressure,
            "compression": self.compression,
            "node_id": self.node_id,
            "prompt_bytes": self.prompt_bytes,
            "sources": (None if self.sources is None else [s.to_dict() for s in self.sources]),
            "totals_by_state": self.totals_by_state,
        }


@dataclass
class ContextView:
    """Projection of context provenance and budget across an entire run."""

    schema_version: str
    run_id: str | None
    nodes: list[NodeContextView]

    # index for O(1) node lookup
    _by_node: dict[str, NodeContextView] = field(default_factory=dict, repr=False, compare=False)

    def __post_init__(self) -> None:
        self._by_node = {n.node_id: n for n in self.nodes}

    def node(self, node_id: str) -> NodeContextView | None:
        return self._by_node.get(node_id)

    def to_dict(self) -> dict[str, Any]:
        return {
            "nodes": [n.to_dict() for n in self.nodes],
            "run_id": self.run_id,
            "schema_version": self.schema_version,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _state_from_raw(entry: Mapping[str, Any]) -> SourceState:
    """Derive SourceState from a raw sources dict entry produced by _hydrate_sources."""
    included = entry.get("included")
    truncated_at = entry.get("truncated_at")
    reason = entry.get("reason")

    if included is False:
        # excluded or a named non-inclusion reason
        if reason in ("deduplicated",):
            return "deduplicated"
        if reason in ("compressed",):
            return "compressed"
        return "excluded"

    if included is True:
        if truncated_at is not None:
            return "truncated"
        return "included"

    # Neither True nor False — unknown
    return "unknown"


def _source_entry_from_raw(entry: Mapping[str, Any]) -> SourceEntry:
    """Convert one raw dict from event data['sources'] into a :class:`SourceEntry`."""
    raw_reason: Any = entry.get("reason")
    # Redact any secrets that may have leaked into free-text reason fields
    redacted_reason: str | None
    if raw_reason is not None:
        redacted = redact_contract_secrets(str(raw_reason))
        redacted_reason = str(redacted) if redacted is not None else None
    else:
        redacted_reason = None

    raw_bytes = entry.get("bytes")
    byte_count: int | None = int(raw_bytes) if raw_bytes is not None else None

    raw_trunc = entry.get("truncated_at")
    truncated_at: int | None = int(raw_trunc) if raw_trunc is not None else None

    return SourceEntry(
        path=str(entry.get("path", "")),
        bytes=byte_count,
        state=_state_from_raw(entry),
        reason=redacted_reason,
        truncated_at=truncated_at,
    )


def _totals_by_state(sources: list[SourceEntry]) -> dict[str, int]:
    """Sum bytes per state.  Sources with unknown bytes contribute 0."""
    totals: dict[str, int] = {}
    for src in sources:
        b = src.bytes if src.bytes is not None else 0
        totals[src.state] = totals.get(src.state, 0) + b
    return dict(sorted(totals.items()))


def _budget_pressure(prompt_bytes: int | None, budget_bytes: int | None) -> float | None:
    if prompt_bytes is None or budget_bytes is None or budget_bytes == 0:
        return None
    return round(prompt_bytes / budget_bytes, 6)


def _node_view_from_event(event: RunEvent) -> NodeContextView:
    """Build a :class:`NodeContextView` from a single hydrate/rehydrate event."""
    data = event.data

    raw_budget = data.get("budget_bytes")
    budget_bytes: int | None = int(raw_budget) if raw_budget is not None else None

    raw_prompt = data.get("prompt_bytes")
    prompt_bytes: int | None = int(raw_prompt) if raw_prompt is not None else None

    raw_sources = data.get("sources")
    sources: list[SourceEntry] | None
    totals: dict[str, int] | None

    if raw_sources is None:
        # Legacy event — sources not recorded
        sources = None
        totals = None
    else:
        sources = [_source_entry_from_raw(s) for s in raw_sources]
        totals = _totals_by_state(sources)

    raw_compression = data.get("compression")
    compression: str | None = str(raw_compression) if raw_compression is not None else None

    return NodeContextView(
        node_id=event.node_id,
        budget_bytes=budget_bytes,
        sources=sources,
        prompt_bytes=prompt_bytes,
        totals_by_state=totals,
        budget_pressure=_budget_pressure(prompt_bytes, budget_bytes),
        compression=compression,
    )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def context_view(events: Iterable[RunEvent] | Path | str) -> ContextView:
    """Build a :class:`ContextView` from *events* or a run directory path.

    Parameters
    ----------
    events:
        An iterable of :class:`RunEvent` objects, or a :class:`Path` / ``str``
        pointing to a run directory that contains ``events.jsonl``.

    Returns
    -------
    ContextView
        Deterministic projection — calling this function twice with the same
        inputs always produces the same result.
    """
    if isinstance(events, (str, Path)):
        run_dir = Path(events)
        log = EventLog(run_dir / "events.jsonl")
        event_list = log.read()
    else:
        event_list = list(events)

    # Extract run_id from the first "start" event (if present)
    run_id: str | None = None
    for ev in event_list:
        if ev.type == "run_started":
            run_id = str(ev.data.get("run_id", "")) or None
            break

    # Per-node: keep LAST hydrate/rehydrate event for each node_id
    # (rehydrate replaces hydrate; a second hydrate after a context-overflow shrink
    #  supersedes the first)
    last_hydrate: dict[str, RunEvent] = {}
    for ev in event_list:
        if ev.type in ("hydrate", "rehydrate"):
            last_hydrate[ev.node_id] = ev

    nodes = [_node_view_from_event(ev) for ev in last_hydrate.values()]

    return ContextView(schema_version=_SCHEMA_VERSION, run_id=run_id, nodes=nodes)
