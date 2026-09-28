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
