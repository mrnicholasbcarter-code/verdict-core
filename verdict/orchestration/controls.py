"""File-based control channel for orchestration runs.

Each run directory can have a ``controls.jsonl`` file (append-only, one JSON
request per line).  ``request_control`` writes validated requests.
``ControlReader`` lets the runtime poll for pending requests.

Supported control kinds:
- ``cancel_run``: stop admitting new work and cancel running attempts
- ``cancel_node``: cancel a specific node's running attempt (no replacement)
- ``retry_node``: retry a TERMINAL_FAILURE/BLOCKED node via RecoveryBudget
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from verdict.orchestration.contracts import WorkGraph

CONTROLS_FILE = "controls.jsonl"

VALID_KINDS = frozenset({"cancel_run", "cancel_node", "retry_node"})
NODE_SCOPED_KINDS = frozenset({"cancel_node", "retry_node"})


class ControlError(Exception):
    """Raised when a control request is invalid."""


@dataclass(frozen=True)
class ControlRequest:
    """A validated, immutable control request."""

    id: str
    kind: str
    node_id: str | None
    requested_at: str
    requested_by: str

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "id": self.id,
            "kind": self.kind,
            "requested_at": self.requested_at,
            "requested_by": self.requested_by,
        }
        if self.node_id is not None:
            d["node_id"] = self.node_id
        return d


def request_control(
    run_dir: Path,
    kind: str,
    *,
    node_id: str | None = None,
    requested_by: str = "operator",
    graph: WorkGraph | None = None,
) -> ControlRequest:
    """Validate and append a control request to the run directory.

    Raises ``ControlError`` for unknown kinds, missing/unknown node_ids, or
    node_id supplied to a run-level control.
    """
    if kind not in VALID_KINDS:
        raise ControlError(f"unknown control kind: {kind!r}")

    if kind in NODE_SCOPED_KINDS:
        if node_id is None:
            raise ControlError(f"{kind} requires a node_id")
        if graph is not None:
            node_ids = {n.node_id for n in graph.nodes}
            if node_id not in node_ids:
                raise ControlError(f"unknown node_id: {node_id!r}")
    elif node_id is not None:
        raise ControlError(f"{kind} does not accept a node_id")

    req = ControlRequest(
        id=uuid.uuid4().hex[:12],
        kind=kind,
        node_id=node_id,
        requested_at=datetime.now(timezone.utc).isoformat(),
        requested_by=requested_by,
    )
    path = run_dir / CONTROLS_FILE
    with path.open("a") as f:
        f.write(json.dumps(req.to_dict()) + "\n")
    return req


class ControlReader:
    """Reads new control requests from the controls file.

    Tracks how many lines have been consumed so ``pending()`` only returns
    requests not yet processed.
    """

    def __init__(self, run_dir: Path) -> None:
        self._path = run_dir / CONTROLS_FILE
        self._offset = 0

    def pending(self) -> list[ControlRequest]:
        """Return control requests appended since the last call."""
        if not self._path.exists():
            return []
        with self._path.open() as f:
            lines = f.readlines()
        new_lines = lines[self._offset :]
        self._offset = len(lines)
        requests: list[ControlRequest] = []
        for line in new_lines:
            line = line.strip()
            if not line:
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                continue
            requests.append(
                ControlRequest(
                    id=raw.get("id", ""),
                    kind=raw.get("kind", ""),
                    node_id=raw.get("node_id"),
                    requested_at=raw.get("requested_at", ""),
                    requested_by=raw.get("requested_by", ""),
                )
            )
        return requests

    def seen_ids(self) -> set[str]:
        """All request ids ever read (for dedup)."""
        if not self._path.exists():
            return set()
        ids: set[str] = set()
        with self._path.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    raw = json.loads(line)
                    ids.add(raw.get("id", ""))
                except json.JSONDecodeError:
                    continue
        return ids


class RunControl:
    """Domain admission for requests to an existing live run.

    The durable log is a preflight, not a dispatch authority. The controller
    rechecks the live recovery budget and routes every retry through selection,
    admission and proof. Missing policy evidence fails closed.
    """

    def __init__(self, run_dir: Path) -> None:
        self.run_dir = Path(run_dir)

    def submit(
        self, kind: str, *, node_id: str | None = None, requested_by: str = "operator"
    ) -> ControlRequest:
        from verdict.orchestration.contracts import FailureClassification
        from verdict.orchestration.recovery import RecoveryBudget

        graph_path = self.run_dir / "graph.json"
        events_path = self.run_dir / "events.jsonl"
        if not graph_path.exists() or not events_path.exists():
            raise ControlError("run does not have a durable graph and event log")
        raw = json.loads(graph_path.read_text())
        graph = WorkGraph.from_dict({k: v for k, v in raw.items() if k != "run_id"})
        if kind not in VALID_KINDS:
            raise ControlError(f"unknown control kind: {kind!r}")
        if kind in NODE_SCOPED_KINDS and node_id not in {n.node_id for n in graph.nodes}:
            raise ControlError(f"unknown node_id: {node_id!r}")
        if kind == "cancel_run" and node_id is not None:
            raise ControlError("cancel_run does not accept a node_id")
        from verdict.orchestration.receipt import EventLog

        events = EventLog(events_path).read()
        starts = [i for i, e in enumerate(events) if e.type == "run_started"]
        if not starts:
            raise ControlError("run has not started; no active controller evidence")
        active = events[starts[-1] :]
        if any(e.type == "run_finished" for e in active):
            raise ControlError("run is terminal; start an explicit resume before sending controls")
        node_events = [e for e in events if e.node_id == node_id]
        states = [e for e in node_events if e.type == "node_state"]
        state = str(states[-1].data.get("state", "PLANNED")) if states else "PLANNED"
        if kind == "cancel_node" and state not in {"PLANNED", "ADMITTED", "DISPATCHED", "RUNNING"}:
            raise ControlError(f"node {node_id} is {state}; only active work can be cancelled")
        if kind == "retry_node":
            if state not in {"TERMINAL_FAILURE", "BLOCKED"}:
                raise ControlError(
                    f"node {node_id} is {state}; retry only for TERMINAL_FAILURE/BLOCKED"
                )
            if states and "cancelled" in str(states[-1].data.get("reason", "")):
                raise ControlError("node was cancelled; automatic replacement is prohibited")
            budget_raw = active[0].data.get("retry_budget", {})
            limit = (
                budget_raw.get("max_attempts_per_node") if isinstance(budget_raw, dict) else None
            )
            if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
                raise ControlError("recovery budget is unknown; retry refused")
            failures = [
                FailureClassification(
                    category=str(e.data.get("category", "unknown")),
                    action=str(e.data.get("action", "BLOCK")),
                    cooldown_seconds=0,
                    scope="none",
                    evidence=str(e.data.get("evidence", "")),
                )
                for e in node_events
                if e.type == "failure" and e.data.get("category") != "pool_exhausted"
            ]
            attempts = max(
                (
                    e.data["attempt"]
                    for e in node_events
                    if type(e.data.get("attempt")) is int and e.data["attempt"] >= 0
                ),
                default=0,
            )
            if not failures:
                raise ControlError("no recoverable failure evidence recorded; retry refused")
            action, reason = RecoveryBudget(max_attempts_per_node=limit).decide(
                node_id or "", failures, attempts=attempts
            )
            if action == "FAIL_CLOSED":
                raise ControlError(f"recovery budget refused: {reason}")
        return request_control(
            self.run_dir, kind, node_id=node_id, requested_by=requested_by, graph=graph
        )
