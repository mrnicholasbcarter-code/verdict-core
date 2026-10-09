"""Run receipt / evidence chain for one orchestration run.

``EventLog`` is the append-only, fsync'd JSONL source of truth (``events.jsonl``).
``build_run_receipt`` projects it (plus ``graph.json`` and optional ``review.json``)
into a deterministic receipt. ``verify_run_receipt`` recomputes the digests so that
tampering with the event log is detected, and ``completion_verdict`` is fail-closed:
a run is COMPLETE only with validation, integration and review evidence.

Event ``data`` conventions read by the receipt (extra keys are ignored):

* ``run_started``: ``run_id``, ``goal``, optional ``producer``
  ``{verdict_version, git_sha, dirty}`` (captured once; nulls explicit)
* ``run_finished``: ``outcome``, ``reason``
* ``dispatch``: ``attempt``, ``route_id``, ``provider``, ``capacity_class``, ``fault_injected``
* ``terminal``: ``attempt``, ``ok``, ``route_id``, ``reported_model``, ``error``, ``duration_seconds``, ``fault_injected``
* ``failure``: ``attempt``, ``category``          * ``node_state``: ``state``
* ``verify``: ``ok``, ``command``, ``exit_code``   * ``barrier``: ``name``, ``ok``
* ``repo_gates``: discovery summary (``gates``, ``declared``, optional ``note``)
* ``repo_gate``: ``name``, ``ok``, ``exit_code``, ``command``, ``executed_command``, ``source``, ``tail``
* ``integrate``: ``commit``                        * ``review``: ``status``, ``reviewer``,
  ``route_id``, ``blocking``                       * ``reassign``/``cooldown``/``controller``: free-form

``no_change_nodes`` is a derived summary of implement nodes whose validated
attempt emitted an ok ``no_change`` barrier (zero file changes). It is omitted
when empty so existing committed receipts still verify.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import subprocess
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
from verdict.outcome_records import write_outcome_records

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
    """Classify route identity match status.

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
    pending_probe_fields: dict[str, str] = {}
    pending_route = ""
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
            # Free-first story 3: probe class and cache freshness.
            for field in ("probe_class", "cache_checked_at", "cache_freshness"):
                if data.get(field):
                    row[field] = str(data[field])
            # Apply buffered selection-event probe fields only to an attempt on
            # the route that selection chose. A dispatch to any other route
            # drops them, so a replaced selection can't leak its evidence.
            if pending_probe_fields:
                if pending_route and pending_route == route:
                    row.update(pending_probe_fields)
                    pending_probe_fields = {}
                    pending_route = ""
                elif event.type == "dispatch":
                    pending_probe_fields = {}
                    pending_route = ""
            row["fault_injected"] = bool(row["fault_injected"] or data.get("fault_injected"))
            if event.type == "terminal":
                ok = data.get("ok") is True
                row["outcome"] = "success" if ok else "failure"
                if isinstance(data.get("duration_seconds"), int | float):
                    row["duration_seconds"] = float(data["duration_seconds"])
                if isinstance(data.get("usage"), dict):
                    row["usage"] = data["usage"]
                # Preserve executor provenance so receipt shows which harness ran.
                if data.get("executor_kind"):
                    row["executor_kind"] = str(data["executor_kind"])
                if data.get("harness"):
                    row["harness"] = str(data["harness"])
                # track route identity (intended vs executed)
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
        elif event.type == "selection":
            # Selection events carry probe-class fields but must not
            # create new attempt rows. Buffer fields for the next
            # dispatch that creates or touches a row.
            # Every selection replaces the buffer: a selection that is replaced
            # before dispatch must not leak its evidence to the next route.
            pending_probe_fields = {}
            pending_route = str(data.get("route_id") or "")
            for field in ("probe_class", "cache_checked_at", "cache_freshness"):
                if data.get(field):
                    pending_probe_fields[field] = str(data[field])
            # UNKNOWN capacity opt-in flag.
            if data.get("unknown_capacity_opt_in"):
                pending_probe_fields["unknown_capacity_opt_in"] = "true"
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


def _no_change_implement_nodes(
    graph: WorkGraph, events: list[RunEvent], nodes: list[dict[str, Any]]
) -> list[str]:
    """Implement node ids whose validated attempt changed no files.

    The runtime emits an ok ``no_change`` barrier when an implement node's
    validated attempt has an empty diff. Listed in graph order.
    """
    validated = {
        n["node_id"]
        for n in nodes
        if n.get("kind") == NodeKind.IMPLEMENT.value
        and n.get("final_state") == NodeState.VALIDATED.value
    }
    last_ownership: dict[str, int] = {}
    last_no_change: dict[str, int] = {}
    for event in events:
        if event.type != "barrier" or event.node_id not in validated:
            continue
        name = event.data.get("name")
        if name == "ownership":
            last_ownership[event.node_id] = event.seq
        elif name == "no_change" and event.data.get("ok") is True:
            last_no_change[event.node_id] = event.seq
    seen = {
        node_id for node_id, seq in last_no_change.items() if seq > last_ownership.get(node_id, -1)
    }
    return [n.node_id for n in graph.nodes if n.node_id in seen]


def _review_block(run_dir: Path, events: list[RunEvent]) -> dict[str, Any]:
    review_path = run_dir / REVIEW_FILE
    source: Mapping[str, Any] | None = None
    if review_path.exists():
        source = _load_json(review_path)
    else:
        found = [e for e in events if e.type == "review"]
        source = found[-1].data if found else None
    # BOD-224: every reviewer attempt, not only the final one. Additive key;
    # older runs without review_attempt events get an empty list.
    attempts = [
        {
            key: e.data.get(key)
            for key in (
                "attempt",
                "route_id",
                "status",
                "category",
                "scope",
                "cooldown_seconds",
                "duration_seconds",
                "detail",
            )
            if e.data.get(key) is not None
        }
        for e in events
        if e.type == "review_attempt"
    ]
    # Only present when attempts were recorded, so receipts written before this
    # field existed recompute byte-for-byte and still verify.
    extra: dict[str, Any] = {"attempts": attempts} if attempts else {}
    if source is None:
        return {"status": "MISSING", "reviewer": "", "route_id": "", "blocking": 0, **extra}
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
        **extra,
    }


def capture_producer() -> dict[str, Any]:
    """Snapshot the imported Verdict package, not the repository being worked on.

    A wheel can live inside an unrelated checkout (for example in its .venv).
    Require the imported source file to be tracked before attributing its Git
    identity to that checkout. Unavailable lookups remain explicit nulls.
    """
    import verdict

    producer: dict[str, Any] = {
        "verdict_version": _producer_verdict_version(),
        "git_sha": None,
        "dirty": None,
    }
    try:
        source = Path(verdict.__file__).resolve()
        package_dir = source.parent
        root_result = _producer_git(["rev-parse", "--show-toplevel"], package_dir)
        if root_result is None or root_result.returncode != 0:
            return producer
        root = Path(root_result.stdout.strip()).resolve()
        relative_source = source.relative_to(root)
        tracked = _producer_git(["ls-files", "--error-unmatch", "--", str(relative_source)], root)
        if tracked is None or tracked.returncode != 0:
            return producer
        head = _producer_git(["rev-parse", "HEAD"], package_dir)
        if head is not None and head.returncode == 0:
            sha = head.stdout.strip()
            if re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", sha):
                producer["git_sha"] = sha
        status = _producer_git(["status", "--porcelain"], root)
        if status is not None and status.returncode == 0:
            producer["dirty"] = bool(status.stdout.strip())
    except (OSError, TypeError, ValueError):
        # Missing package paths or an unresolvable checkout cannot stop a run.
        pass
    return producer


def _producer_verdict_version() -> str | None:
    # Distribution metadata can belong to a different editable install when
    # PYTHONPATH selects this checkout. Use the same imported source as Git.
    import verdict

    if hasattr(verdict, "__version__"):
        source_version = verdict.__version__
        return source_version if isinstance(source_version, str) and source_version else None
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("verdict-core")
    except PackageNotFoundError:
        return None


def _producer_git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=5, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None


def producer_from_started(started: RunEvent | None) -> dict[str, Any] | None:
    """Project optional producer provenance from the first run_started only.

    Legacy runs stay without a producer. Present mappings retain explicit
    nulls and always project the three supported keys, without fresh lookups.
    """
    if started is None:
        return None
    raw = started.data.get("producer")
    if not isinstance(raw, Mapping):
        return None
    return {
        "verdict_version": raw.get("verdict_version"),
        "git_sha": raw.get("git_sha"),
        "dirty": raw.get("dirty"),
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
    no_change_nodes = _no_change_implement_nodes(graph, events, nodes)

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
        "planner_attempts": [
            {"seq": e.seq, "at": e.at, **dict(e.data)}
            for e in events
            if e.type in {"plan_started", "plan_repair_started", "plan_repair_terminal"}
        ],
        "decision_signals": _free("decision_signals") or None,  # SHADOW signals (optional)
        "claimed_outcome": str(finished[-1].data.get("outcome", "")) if finished else "",
        "event_count": len(events),
        "events_digest": _sha256_file(events_path),
        "started_at": (started.at if started else events[0].at) if events else None,
        "finished_at": finished[-1].at if finished else None,
    }
    if started and isinstance(started.data.get("retry_budget"), Mapping):
        receipt["retry_budget"] = dict(started.data["retry_budget"])
    # BOD-225: never capture fresh producer values during receipt replay.
    producer = producer_from_started(started)
    if producer is not None:
        receipt["producer"] = producer
    # BOD-289: project gate evidence only for runs that emit these new events.
    # Legacy receipts retain their exact field set during replay verification.
    repo_gates = _free("repo_gates")
    repo_gate_results = _free("repo_gate")
    if repo_gates or repo_gate_results:
        receipt["repo_gates"] = repo_gates
        receipt["repo_gate_results"] = repo_gate_results
    # Derived, omitted when empty so committed proof receipts still verify.
    if no_change_nodes:
        receipt["no_change_nodes"] = no_change_nodes
    # route identity tracking: add route_identity_warning if any successful attempt had a mismatch
    if has_successful_mismatch:
        receipt["route_identity_warning"] = (
            "One or more successful attempts reported a model different from the intended route"
        )
    # Keep historical blocks intact.  New conformance evidence is projected
    # from the event log, never trusted from mutable graph.json alone.
    if "openspec" in graph_raw:
        receipt["openspec"] = (
            dict(graph_raw["openspec"])
            if isinstance(graph_raw["openspec"], dict)
            else graph_raw["openspec"]
        )
        checks = [e for e in events if e.type == "openspec_conformance"]
        if isinstance(receipt["openspec"], dict):
            if checks:
                receipt["openspec"]["conformance_result"] = checks[-1].data.get("result")
            else:
                receipt["openspec"]["conformance_result"] = None
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
    try:
        write_outcome_records(run_dir, receipt)
    except OSError as exc:
        import sys

        print(
            f"warning: could not write outcome records to"
            f" {run_dir / 'outcome-records.jsonl'}: {exc}",
            file=sys.stderr,
        )
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
    # Cancelled runs are never COMPLETE — preserve the operator's intent.
    if receipt.get("claimed_outcome") == RunOutcome.CANCELLED.value:
        return RunOutcome.CANCELLED.value, "run cancelled by operator"
    if receipt.get("schema") != RECEIPT_SCHEMA:
        return blocked, f"unknown receipt schema {receipt.get('schema')!r}"
    if "openspec" in receipt:
        block = receipt["openspec"]
        if not isinstance(block, Mapping):
            return blocked, "OpenSpec block malformed"
        check = block.get("conformance_result")
        if not isinstance(check, Mapping) or check.get("status") != "PASS":
            reason = (
                check.get("reason", "missing conformance result")
                if isinstance(check, Mapping)
                else "missing conformance result"
            )
            return blocked, f"OpenSpec conformance required: {reason}"
        if (
            not block.get("spec_revision_digest")
            or check.get("spec_revision_digest") != block["spec_revision_digest"]
        ):
            return blocked, "OpenSpec spec_changed: conformance revision differs"
        if (
            not check.get("output_sha256")
            or check.get("valid") is not True
            or check.get("issues")
            or check.get("exit_code") != 0
        ):
            return blocked, "OpenSpec conformance evidence invalid"
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
