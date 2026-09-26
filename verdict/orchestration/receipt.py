"""Run receipt / evidence chain for one orchestration run.

``EventLog`` is the append-only, fsync'd JSONL source of truth (``events.jsonl``).
``build_run_receipt`` projects it (plus ``graph.json`` and optional ``review.json``)
into a deterministic receipt. ``verify_run_receipt`` recomputes the digests so that
tampering with the event log is detected, and ``completion_verdict`` is fail-closed:
a run is COMPLETE only with validation, integration and review evidence.

Event ``data`` conventions read by the receipt (extra keys are ignored):

* ``run_started``: ``run_id``, ``goal``          * ``run_finished``: ``outcome``, ``reason``
* ``dispatch``: ``attempt``, ``route_id``, ``provider``, ``capacity_class``, ``fault_injected``
* ``terminal``: ``attempt``, ``ok``, ``route_id``, ``reported_model``, ``error``, ``duration_seconds``, ``fault_injected``
* ``failure``: ``attempt``, ``category``          * ``node_state``: ``state``
* ``verify``: ``ok``, ``command``, ``exit_code``   * ``barrier``: ``name``, ``ok``
* ``integrate``: ``commit``                        * ``review``: ``status``, ``reviewer``,
  ``route_id``, ``blocking``                       * ``reassign``/``cooldown``/``controller``: free-form
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
import threading
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from verdict.orchestration.contracts import (
    NodeKind,
    NodeState,
    OrchestrationError,
    RunEvent,
    RunOutcome,
    WorkGraph,
    route_provider,
)

RECEIPT_SCHEMA = "verdict.run-receipt/v1"
EVENTS_FILE = "events.jsonl"
GRAPH_FILE = "graph.json"
REVIEW_FILE = "review.json"
RECEIPT_FILE = "receipt.json"
REDACTED = "[REDACTED]"

_SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{8,}"), REDACTED),
    (re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=\-]{8,}"), "Bearer " + REDACTED),
    (
        re.compile(r"(?i)\b(api[_-]?key|x-api-key|access[_-]?token)(\s*[=:]\s*)[\"']?[^\s\"'&,;]+"),
        r"\1\2" + REDACTED,
    ),
)


def scrub_secrets(text: str) -> str:
    """Replace substrings that look like credentials with ``[REDACTED]``."""
    for pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def _clean(value: Any, where: str) -> Any:
    """Return a JSON-safe, secret-scrubbed copy of ``value`` or raise."""
    if value is None or isinstance(value, bool | int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise OrchestrationError(f"{where}: non-finite float is not JSON-serializable")
        return value
    if isinstance(value, str):
        return scrub_secrets(value)
    if isinstance(value, list | tuple):
        return [_clean(item, f"{where}[{i}]") for i, item in enumerate(value)]
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise OrchestrationError(f"{where}: non-string key {key!r}")
            out[key] = _clean(item, f"{where}.{key}")
        return out
    raise OrchestrationError(f"{where}: {type(value).__name__} is not JSON-serializable")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _fsync_dir(directory: Path) -> None:
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:  # pragma: no cover - platforms without directory fds
        return
    try:
        os.fsync(fd)
    except OSError:  # pragma: no cover
        pass
    finally:
        os.close(fd)


class EventLog:
    """Append-only JSONL event log with strictly increasing ``seq`` (resume-safe)."""

    def __init__(self, path: Path, *, clock: Callable[[], datetime] | None = None) -> None:
        self.path = Path(path)
        self._clock = clock or _utc_now
        self._lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._repair_torn_tail()
        events = self.read()
        self._seq = events[-1].seq if events else 0

    @property
    def last_seq(self) -> int:
        return self._seq

    def _repair_torn_tail(self) -> None:
        """Drop a trailing partial line left by a crash mid-write."""
        if not self.path.exists():
            return
        raw = self.path.read_bytes()
        if not raw or raw.endswith(b"\n"):
            return
        cut = raw.rfind(b"\n") + 1
        with self.path.open("r+b") as stream:
            stream.truncate(cut)
            stream.flush()
            os.fsync(stream.fileno())

    def emit(self, type: str, /, node_id: str = "", **data: Any) -> RunEvent:
        clean = _clean(data, f"{type}.data")
        with self._lock:
            at = self._clock().astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
            event = RunEvent(seq=self._seq + 1, at=at, type=type, node_id=node_id, data=clean)
            line = json.dumps(event.to_dict(), sort_keys=True, separators=(",", ":"))
            with self.path.open("a", encoding="utf-8") as stream:
                stream.write(line + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            self._seq = event.seq
        return event

    def read(self) -> list[RunEvent]:
        if not self.path.exists():
            return []
        events: list[RunEvent] = []
        last = 0
        for number, line in enumerate(self.path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            try:
                event = RunEvent.from_dict(json.loads(line))
            except (ValueError, KeyError, TypeError) as exc:
                raise OrchestrationError(f"{self.path}:{number}: corrupt event: {exc}") from exc
            if event.seq <= last:
                raise OrchestrationError(
                    f"{self.path}:{number}: seq {event.seq} not greater than {last}"
                )
            last = event.seq
            events.append(event)
        return events


# ---------------------------------------------------------------- receipt


def _sha256_file(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise OrchestrationError(f"cannot read {path.name}: {exc}") from exc
    if not isinstance(value, dict):
        raise OrchestrationError(f"{path.name} must contain a JSON object")
    return value


def _attempt_of(event: RunEvent, fallback: int) -> int:
    raw = event.data.get("attempt")
    return raw if isinstance(raw, int) and not isinstance(raw, bool) and raw > 0 else fallback


def _classify_route_identity(
    route_id: str, reported_model: str | None, ok: bool, error: str, failure_category: str
) -> str:
    """Classify route identity match status (route identity tracking).

    Args:
        route_id: Intended route identity from dispatch
        reported_model: Actual model reported by terminal (may be None or empty)
        ok: Terminal success flag
        error: Terminal error field (from WorkerTerminal.error)
        failure_category: Failure category from subsequent failure event (if any)

    Returns:
        One of: "match", "mismatch", "unattested", "mechanical"

    Only successful terminals (ok=True) can be classified as match/mismatch.
    Failed terminals are "unattested" UNLESS the error is "model_mismatch"
    (or failure_category is "model_mismatch"), in which case they are "mismatch".
    The comparison follows executors.py line 234: exact string equality.
    """
    if reported_model == "(mechanical merge)":
        return "mechanical"
    if not ok:
        # Failed terminals: check if it's a model_mismatch error
        if error == "model_mismatch" or failure_category == "model_mismatch":
            return "mismatch"
        return "unattested"
    # Successful terminal: compare reported_model to route_id
    if not reported_model:  # None or empty string
        return "unattested"
    if reported_model == route_id:
        return "match"
    return "mismatch"


def _node_record(node_id: str, kind: str, events: list[RunEvent]) -> dict[str, Any]:
    attempts: dict[int, dict[str, Any]] = {}
    current = 0
    last_success_seq = -1
    claimed_state = ""
    claimed_seq = -1
    commit: str | None = None
    for event in events:
        data = event.data
        if event.type == "dispatch":
            current = _attempt_of(event, current + 1)
        elif event.type in {"terminal", "failure"}:
            current = _attempt_of(event, current or 1)
        if event.type in {"dispatch", "terminal", "failure"}:
            row = attempts.setdefault(
                current,
                {
                    "attempt": current,
                    "route_id": "",
                    "provider": "",
                    "capacity_class": "unknown",
                    "outcome": "running",
                    "fault_injected": False,
                },
            )
            route = str(data.get("route_id") or row["route_id"])
            row["route_id"] = route
            row["provider"] = str(data.get("provider") or row["provider"] or route_provider(route))
            if data.get("capacity_class"):
                row["capacity_class"] = str(data["capacity_class"])
            row["fault_injected"] = bool(row["fault_injected"] or data.get("fault_injected"))
            if event.type == "terminal":
                ok = data.get("ok") is True
                row["outcome"] = "success" if ok else "failure"
                if isinstance(data.get("duration_seconds"), int | float):
                    row["duration_seconds"] = float(data["duration_seconds"])
                # route identity tracking: track route identity (intended vs executed)
                reported = str(data.get("reported_model") or "")
                error = str(data.get("error") or "")
                row["intended_route"] = route
                row["executed_model"] = reported if reported else None
                # Store terminal data for route_identity classification
                row["_terminal_ok"] = ok
                row["_terminal_error"] = error
                row["_terminal_reported"] = reported
                if ok:
                    last_success_seq = event.seq
            elif event.type == "failure":
                row["outcome"] = "failure"
                row["failure_category"] = str(data.get("category") or "unknown")
        elif event.type == "node_state":
            claimed_state = str(data.get("state", ""))
            claimed_seq = event.seq
            if claimed_state == NodeState.TERMINAL_SUCCESS.value:
                last_success_seq = event.seq
        elif event.type == "integrate" and data.get("commit"):
            commit = str(data["commit"])
    # route identity tracking: compute route_identity for each attempt (after all events processed)
    for row in attempts.values():
        if "_terminal_ok" in row:
            ok = row.pop("_terminal_ok")
            error = row.pop("_terminal_error")
            reported = row.pop("_terminal_reported")
            route = row["route_id"]
            failure_cat = row.get("failure_category", "")
            row["route_identity"] = _classify_route_identity(
                route, reported if reported else None, ok, error, failure_cat
            )
        else:
            # Dispatched but no terminal yet (shouldn't happen in a finished run)
            row["route_identity"] = "unattested"
    validated_by = [
        {"command": e.data.get("command", ""), "exit_code": e.data.get("exit_code"), "seq": e.seq}
        for e in events
        if e.type == "verify" and e.data.get("ok") is True and e.seq > last_success_seq >= 0
    ]
    revoked = {NodeState.REJECTED.value, NodeState.BLOCKED.value}
    if validated_by and not (claimed_state in revoked and claimed_seq > validated_by[-1]["seq"]):
        final_state = NodeState.VALIDATED.value
    elif claimed_state and claimed_state != NodeState.VALIDATED.value:
        final_state = claimed_state
    elif last_success_seq >= 0:
        final_state = NodeState.TERMINAL_SUCCESS.value  # success claimed, never verified
    elif attempts:
        latest = attempts[max(attempts)]["outcome"]
        final_state = {"success": "TERMINAL_SUCCESS", "failure": "TERMINAL_FAILURE"}.get(
            latest, NodeState.RUNNING.value
        )
    else:
        final_state = NodeState.PLANNED.value
    for number, row in attempts.items():
        # A dispatched attempt with no terminal that is not the node's live attempt
        # was abandoned (controller stall/crash) — never counted as success.
        if row["outcome"] == "running" and (number != max(attempts) or final_state != "RUNNING"):
            row["outcome"] = "abandoned"
    record: dict[str, Any] = {
        "node_id": node_id,
        "kind": kind,
        "final_state": final_state,
        "attempts": [attempts[k] for k in sorted(attempts)],
        "validated_by": validated_by,
    }
    if commit:
        record["commit"] = commit
    return record


def _review_block(run_dir: Path, events: list[RunEvent]) -> dict[str, Any]:
    review_path = run_dir / REVIEW_FILE
    source: Mapping[str, Any] | None = None
    if review_path.exists():
        source = _load_json(review_path)
    else:
        found = [e for e in events if e.type == "review"]
        source = found[-1].data if found else None
    if source is None:
        return {"status": "MISSING", "reviewer": "", "route_id": "", "blocking": 0}
    findings = source.get("findings")
    raw_blocking = source.get("blocking")
    if isinstance(findings, list):
        blocking = sum(
            1
            for f in findings
            if isinstance(f, Mapping) and f.get("severity") in {"critical", "high"}
        )
    elif isinstance(raw_blocking, bool):
        blocking = int(raw_blocking)
    elif isinstance(raw_blocking, int):
        blocking = raw_blocking
    else:
        blocking = 0
    return {
        "status": str(source.get("status") or "ERROR").upper(),
        "reviewer": str(source.get("reviewer", "")),
        "route_id": str(source.get("route_id", "")),
        "blocking": blocking,
    }


def build_run_receipt(run_dir: Path) -> dict[str, Any]:
    run_dir = Path(run_dir)
    events_path = run_dir / EVENTS_FILE
    if not events_path.exists():
        raise OrchestrationError(f"missing {EVENTS_FILE} in {run_dir}")
    events = EventLog(events_path).read() if events_path.stat().st_size else []
    graph_raw = _load_json(run_dir / GRAPH_FILE)
    graph = WorkGraph.from_dict({k: v for k, v in graph_raw.items() if k != "run_id"})

    by_node: dict[str, list[RunEvent]] = {n.node_id: [] for n in graph.nodes}
    for event in events:
        if event.node_id in by_node:
            by_node[event.node_id].append(event)
    nodes = [_node_record(n.node_id, n.kind.value, by_node[n.node_id]) for n in graph.nodes]

    started = next((e for e in events if e.type == "run_started"), None)
    finished = [e for e in events if e.type == "run_finished"]
    barriers: dict[str, dict[str, Any]] = {}
    for event in events:
        if event.type == "barrier":
            name = str(event.data.get("name") or event.node_id or "integration")
            barriers[name] = {"name": name, "ok": event.data.get("ok") is True, "seq": event.seq}
    declared = sorted({str(n.barrier) for n in graph.nodes if n.barrier})
    missing = [name for name in declared if name not in barriers]
    integration_ok = bool(barriers) and not missing and all(b["ok"] for b in barriers.values())

    def _free(kind: str) -> list[dict[str, Any]]:
        return [
            {"seq": e.seq, "at": e.at, "node_id": e.node_id, **dict(e.data)}
            for e in events
            if e.type == kind
        ]

    # route identity tracking: compute route identity summary and warning
    route_identity_counts = {"match": 0, "mismatch": 0, "unattested": 0, "mechanical": 0}
    total_attempts = 0
    has_successful_mismatch = False
    for node in nodes:
        for attempt in node.get("attempts", []):
            total_attempts += 1
            identity = attempt.get("route_identity", "unattested")
            route_identity_counts[identity] = route_identity_counts.get(identity, 0) + 1
            # Check if any successful attempt had a mismatch
            if attempt.get("outcome") == "success" and identity == "mismatch":
                has_successful_mismatch = True

    route_identity_summary = {
        "attempts": total_attempts,
        "match": route_identity_counts["match"],
        "mismatch": route_identity_counts["mismatch"],
        "unattested": route_identity_counts["unattested"],
        "mechanical": route_identity_counts["mechanical"],
    }

    receipt: dict[str, Any] = {
        "schema": RECEIPT_SCHEMA,
        "run_id": str(
            graph_raw.get("run_id")
            or (started.data.get("run_id") if started else "")
            or run_dir.name
        ),
        "goal": graph.goal,
        "graph_digest": graph.digest(),
        "topology": graph.topology.value,
        "nodes": nodes,
        "route_identity_summary": route_identity_summary,
        "integration": {
            "ok": integration_ok,
            "barriers": [barriers[k] for k in sorted(barriers)],
            "missing": missing,
        },
        "reassignments": _free("reassign"),
        "cooldowns": _free("cooldown"),
        "review": _review_block(run_dir, events),
        "controller_events": _free("controller"),
        "decision_signals": _free("decision_signals") or None,  # BOD-199: SHADOW signals (optional)
        "claimed_outcome": str(finished[-1].data.get("outcome", "")) if finished else "",
        "event_count": len(events),
        "events_digest": _sha256_file(events_path),
        "started_at": (started.at if started else events[0].at) if events else None,
        "finished_at": finished[-1].at if finished else None,
    }
    # route identity tracking: add route_identity_warning if any successful attempt had a mismatch
    if has_successful_mismatch:
        receipt["route_identity_warning"] = (
            "One or more successful attempts reported a model different from the intended route"
        )
    # Include optional openspec block (backward compatible)
    if "openspec" in graph_raw:
        receipt["openspec"] = graph_raw["openspec"]
    outcome, reason = completion_verdict(receipt)
    receipt["outcome"] = outcome
    receipt["reason"] = reason
    return receipt


def write_run_receipt(run_dir: Path) -> Path:
    run_dir = Path(run_dir)
    receipt = build_run_receipt(run_dir)
    target = run_dir / RECEIPT_FILE
    fd, tmp = tempfile.mkstemp(prefix=".receipt-", suffix=".tmp", dir=run_dir)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(receipt, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, target)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    _fsync_dir(run_dir)
    return target


def verify_run_receipt(run_dir: Path) -> list[str]:
    """Recompute the receipt from source files; empty list means valid."""
    run_dir = Path(run_dir)
    path = run_dir / RECEIPT_FILE
    if not path.exists():
        return [f"missing {RECEIPT_FILE}"]
    try:
        stored = _load_json(path)
    except OrchestrationError as exc:
        return [str(exc)]
    problems: list[str] = []
    if stored.get("schema") != RECEIPT_SCHEMA:
        problems.append(f"unexpected schema {stored.get('schema')!r}")
    events_path = run_dir / EVENTS_FILE
    if not events_path.exists():
        return [*problems, f"missing {EVENTS_FILE}"]
    if stored.get("events_digest") != _sha256_file(events_path):
        problems.append("events_digest mismatch: events.jsonl changed after receipt was written")
    try:
        fresh = build_run_receipt(run_dir)
    except OrchestrationError as exc:
        return [*problems, f"cannot rebuild receipt: {exc}"]
    for key in sorted(set(fresh) | set(stored)):
        if key == "events_digest":
            continue
        if fresh.get(key) != stored.get(key):
            problems.append(f"{key} mismatch: stored receipt differs from recomputed evidence")
    return problems


def completion_verdict(receipt: Mapping[str, Any]) -> tuple[str, str]:
    """COMPLETE only with validated work, an ok integration barrier and a clean PASS review."""
    blocked = RunOutcome.BLOCKED.value
    if receipt.get("schema") != RECEIPT_SCHEMA:
        return blocked, f"unknown receipt schema {receipt.get('schema')!r}"
    gated = {NodeKind.IMPLEMENT.value, NodeKind.INTEGRATE.value}
    work = [n for n in receipt.get("nodes") or [] if n.get("kind") in gated]
    if not work:
        return blocked, "no implement/integrate nodes to validate"
    for node in work:
        if node.get("final_state") != NodeState.VALIDATED.value:
            return blocked, (
                f"node {node.get('node_id')} not VALIDATED (final_state={node.get('final_state')})"
            )
    integration = receipt.get("integration") or {}
    if not integration.get("ok"):
        failed = [b["name"] for b in integration.get("barriers", []) if not b.get("ok")]
        missing = list(integration.get("missing", []))
        if failed:
            return blocked, f"integration barrier failed: {', '.join(failed)}"
        if missing:
            return blocked, f"integration barrier missing: {', '.join(missing)}"
        return blocked, "integration barrier not recorded"
    review = receipt.get("review") or {}
    if review.get("status") != "PASS":
        return blocked, f"review status {review.get('status', 'MISSING')} (PASS required)"
    if review.get("blocking"):
        return blocked, f"review has {review.get('blocking')} blocking finding(s)"
    return (
        RunOutcome.COMPLETE.value,
        "all implement/integrate nodes validated, barrier ok, review PASS",
    )
