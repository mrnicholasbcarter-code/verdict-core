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
* ``dispatch``: ``attempt``, ``route_id``, ``provider``, ``capacity_class``, ``capability_tier``
  (null when the route's tier is unknown), ``fault_injected``
* ``terminal``: ``attempt``, ``ok``, ``route_id``, ``reported_model``, ``error``, ``duration_seconds``, ``fault_injected``,
  optional ``served_check`` (``verdict.served_identity.ServedCheck.to_dict()``), which then decides ``route_identity``
  optional ``worker_assignment`` (``{"assignment_digest", "serve_receipts": {"status", "reason",
  "requests", "requests_waived", "requirement_waivers"}}``, what ``verdict serve`` recorded under
  the attempt's signed assignment; projected as given when well formed, omitted otherwise)
* ``failure``: ``attempt``, ``category``          * ``node_state``: ``state``
* ``verify``: ``ok``, ``command``, ``exit_code``   * ``barrier``: ``name``, ``ok``
* ``repo_gates``: discovery summary (``gates``, ``declared``, optional ``note``)
* ``repo_gate``: ``name``, ``ok``, ``exit_code``, ``command``, ``executed_command``, ``source``, ``tail``
* ``integrate``: ``commit``                        * ``review``: ``status``, ``reviewer``,
  ``route_id``, ``blocking``                       * ``reassign``/``cooldown``/``controller``: free-form
* ``hydrate``: ``context_bytes``, ``prompt_tokens_estimated``, ``sources`` list (projected as
  ``sources_count``), ``truncated`` — per-attempt context-pack evidence (omitted when absent)

``no_change_nodes`` is a derived summary of implement nodes whose validated
attempt emitted an ok ``no_change`` barrier (zero file changes). It is omitted
when empty so existing committed receipts still verify.
"""

from __future__ import annotations

import contextlib
import fcntl
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
from verdict.outcome_records import (
    AttemptRecord,
    append_new_attempt_records,
    build_attempt_record,
    write_outcome_records,
)
from verdict.security import redact_text

RECEIPT_SCHEMA = "verdict.run-receipt/v1"
EVENTS_FILE = "events.jsonl"
GRAPH_FILE = "graph.json"
REVIEW_FILE = "review.json"
RECEIPT_FILE = "receipt.json"
# Host-wide index under verdict_home(): one absolute run dir per receipt write.
VERIFIED_RUNS_FILE = "verified-runs.txt"
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

    def __init__(
        self, path: Path, *, clock: Callable[[], datetime] | None = None, repair: bool = True
    ) -> None:
        self.path = Path(path)
        self._clock = clock or _utc_now
        self._repair = repair
        self._lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if repair:
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
        """Parse all complete events from the log.

        When constructed with ``repair=False`` (non-mutating / read-only mode),
        only the very last non-empty line of a file that lacks a trailing newline
        may be treated as a torn partial write; every other bad line raises
        ``OrchestrationError`` as usual.  A torn final line raises with the
        message ``"torn tail at line N"`` so callers can distinguish it from
        genuine mid-file corruption.  In ``repair=True`` mode all bad lines
        raise, which is unchanged from the original behaviour.
        """
        if not self.path.exists():
            return []

        if not self._repair:
            # Non-repair path: reject symlinks and read atomically so a
            # malicious symlink swap cannot leak outside content.
            raw_bytes = _read_bytes_nofollow(self.path)
            return _parse_events_bytes(raw_bytes, self.path)

        # Repair path (default): strict UTF-8, raise on any bad line.
        events = []
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


def _read_bytes_nofollow(path: Path) -> bytes:
    """Read *path* bytes without following symlinks.

    Uses ``O_NOFOLLOW`` so the check and the read are one atomic operation:
    a symlink cannot be substituted between an ``is_symlink()`` guard and the
    actual open.  Raises ``OrchestrationError`` on a symlink (ELOOP) or any
    other ``OSError`` (missing, permission denied, …).

    Exported so ``verdict.api`` can use the same helper for the list-runs
    summary path.
    """
    try:
        fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as exc:
        raise OrchestrationError(f"cannot read {path.name}: {exc}") from exc
    with os.fdopen(fd, "rb") as fh:
        return fh.read()


def _parse_events_bytes(raw: bytes, path_hint: Path) -> list[RunEvent]:
    """Parse a complete events log from raw bytes.

    Uses the same algorithm as ``EventLog.read(repair=False)``: strict
    monotonic-seq check and torn-tail detection on a file without a trailing
    newline.  Pure function; no filesystem access.  ``path_hint`` is used
    only in error messages.
    """
    has_trailing_newline = not raw or raw.endswith(b"\n")
    all_lines = raw.decode("utf-8", errors="replace").splitlines()
    last_nonempty_idx: int | None = None
    for i in range(len(all_lines) - 1, -1, -1):
        if all_lines[i].strip():
            last_nonempty_idx = i
            break
    events: list[RunEvent] = []
    last = 0
    for number, line in enumerate(all_lines, 1):
        if not line.strip():
            continue
        try:
            event = RunEvent.from_dict(json.loads(line))
        except (ValueError, KeyError, TypeError) as exc:
            if not has_trailing_newline and (number - 1) == last_nonempty_idx:
                raise OrchestrationError(f"torn tail at line {number}") from exc
            raise OrchestrationError(f"{path_hint}:{number}: corrupt event: {exc}") from exc
        if event.seq <= last:
            raise OrchestrationError(
                f"{path_hint}:{number}: seq {event.seq} not greater than {last}"
            )
        last = event.seq
        events.append(event)
    return events


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


_SERVED_FIELDS = ("provider", "model", "connection_id", "decision_strategy")
#: ``served_check.status`` -> attempt ``route_identity``. Anything else is unattested.
_SERVED_ROUTE_IDENTITY = {"verified": "match", "mismatch": "mismatch"}


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


_SHA256_HEX = re.compile(r"[0-9a-f]{64}")
_SERVE_RECEIPT_STATUSES = frozenset({"read", "unavailable"})
_MAX_REASON_CHARS = 300


def _count_or_none(value: Any) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _worker_assignment_row(raw: Any) -> dict[str, Any] | None:
    """The attempt's signed-assignment evidence, or None when ``raw`` is not that record.

    Only the known fields are kept, so nothing an executor adds (and no credential) can reach the
    receipt: the digest must be a SHA-256, the counts non-negative integers or null, and a
    waiver a pair of strings.  A record that does not have this shape is dropped whole rather
    than half-shown.
    """
    if not isinstance(raw, Mapping):
        return None
    digest, serve = raw.get("assignment_digest"), raw.get("serve_receipts")
    if not isinstance(digest, str) or not _SHA256_HEX.fullmatch(digest):
        return None
    if not isinstance(serve, Mapping):
        return None
    status = serve.get("status")
    # A string first: an unhashable value (a list, a dict) must be dropped like any malformed
    # record, not raise out of the set membership test and abort the receipt.
    if not isinstance(status, str) or status not in _SERVE_RECEIPT_STATUSES:
        return None
    waivers: list[dict[str, str]] | None = None
    raw_waivers = serve.get("requirement_waivers")
    if raw_waivers is not None:
        if not isinstance(raw_waivers, list):
            return None
        waivers = []
        for item in raw_waivers:
            if not (
                isinstance(item, Mapping)
                and isinstance(item.get("requirement"), str)
                and isinstance(item.get("reason"), str)
            ):
                return None
            waivers.append({"requirement": str(item["requirement"]), "reason": str(item["reason"])})
    reason = serve.get("reason")
    return {
        "assignment_digest": digest,
        "serve_receipts": {
            "status": status,
            "reason": scrub_secrets(reason)[:_MAX_REASON_CHARS]
            if isinstance(reason, str)
            else None,
            "requests": _count_or_none(serve.get("requests")),
            "requests_waived": _count_or_none(serve.get("requests_waived")),
            "requirement_waivers": waivers,
        },
    }


def _node_record(node_id: str, kind: str, events: list[RunEvent]) -> dict[str, Any]:
    attempts: dict[int, dict[str, Any]] = {}
    current = 0
    last_success_seq = -1
    claimed_state = ""
    claimed_seq = -1
    commit: str | None = None
    pending_probe_fields: dict[str, str] = {}
    pending_route = ""
    pending_effort: dict[str, Any] | None = None
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
            if event.type == "failure" and "_terminal_ok" in row:
                # A failure event after this attempt's terminal (e.g. a
                # pool_exhausted FAIL_CLOSED emitted while reselecting the
                # NEXT candidate) carries the revoked candidate's route_id,
                # not the route this attempt actually dispatched/executed.
                # Never let it overwrite the attempt's already-recorded route.
                route = row["route_id"]
            else:
                route = str(data.get("route_id") or row["route_id"])
                row["route_id"] = route
            row["provider"] = str(data.get("provider") or row["provider"] or route_provider(route))
            if data.get("capacity_class"):
                row["capacity_class"] = str(data["capacity_class"])
            # Tier of the route this attempt ran on (1 strongest). Omitted when the
            # dispatch did not record one: never a default tier.
            tier = data.get("capability_tier")
            if isinstance(tier, int) and not isinstance(tier, bool):
                row["capability_tier"] = tier
            # Free-first story 3: probe class and cache freshness.
            for field in ("probe_class", "cache_checked_at", "cache_freshness", "evidence_level"):
                if data.get(field):
                    row[field] = str(data[field])
            # Apply buffered selection-event probe and effort fields only to an
            # attempt on the route that selection chose. A dispatch to any other
            # route drops them, so a replaced selection can't leak its evidence.
            if pending_probe_fields or pending_effort is not None:
                # Empty route matches only an effort bind (mechanical review).
                # Probe evidence still requires the non-empty route it was chosen for.
                matched = pending_route == route and (
                    bool(pending_route) or pending_effort is not None
                )
                if matched:
                    if pending_probe_fields and pending_route:
                        row.update(pending_probe_fields)
                    if pending_effort is not None:
                        row.update(pending_effort)
                    pending_probe_fields = {}
                    pending_route = ""
                    pending_effort = None
                elif event.type == "dispatch":
                    pending_probe_fields = {}
                    pending_route = ""
                    pending_effort = None
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
                check = data.get("served_check")
                if isinstance(check, Mapping):
                    served = check.get("served")
                    served = served if isinstance(served, Mapping) else {}
                    row["served_identity"] = {
                        "status": check.get("status"),
                        "served_route": check.get("served_route"),
                        **{field: served.get(field) for field in _SERVED_FIELDS},
                        "reasons": list(check.get("reasons") or []),
                    }
                    if check.get("served_route"):
                        row["executed_model"] = str(check["served_route"])
                    row["_terminal_served_status"] = str(check.get("status") or "")
                assignment = _worker_assignment_row(data.get("worker_assignment"))
                if assignment is not None:
                    row["worker_assignment"] = assignment
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
            pending_effort = None
            for field in ("probe_class", "cache_checked_at", "cache_freshness", "evidence_level"):
                if data.get(field):
                    pending_probe_fields[field] = str(data[field])
            # UNKNOWN capacity opt-in flag.
            if data.get("unknown_capacity_opt_in"):
                pending_probe_fields["unknown_capacity_opt_in"] = "true"
            # Chosen effort is buffered with the route it was chosen for.
            # A missing key is a legacy selection and must not invent a field.
            # JSON null is preserved: reasoning disabled is an explicit omit.
            if "chosen_effort" in data:
                pending_effort = {
                    "chosen_effort": data.get("chosen_effort"),
                    "executed_effort": "unverified",
                }
                reason = data.get("effort_reason")
                if isinstance(reason, Mapping):
                    pending_effort["effort_reason"] = {
                        str(key): value
                        for key, value in reason.items()
                        if key
                        in {
                            "task_kind",
                            "default_effort",
                            "clamp",
                            "metadata_source",
                            "route_id",
                            "attempt",
                            "row_unavailable",
                        }
                    }
        elif event.type == "node_state":
            claimed_state = str(data.get("state", ""))
            claimed_seq = event.seq
            if claimed_state == NodeState.TERMINAL_SUCCESS.value:
                last_success_seq = event.seq
        elif event.type == "integrate" and data.get("commit"):
            commit = str(data["commit"])
        elif (
            event.type == "hydrate"
            and current in attempts
            and data.get("context_bytes") is not None
        ):
            # Project context-pack provenance into the attempt that dispatched
            # this hydration.  The hydrate event is emitted per-dispatch, so
            # ``current`` already points at the right attempt row.
            # Guard on context_bytes: legacy hydrate events carry
            # prompt_bytes/budget_bytes/sources without context_bytes;
            # projecting those would add a context_pack to committed receipts
            # that pre-date context-pack wiring.
            cp: dict[str, Any] = {}
            # Coerce numeric fields defensively: a malformed field must not
            # abort receipt building; record null + an omission instead.
            cp_omissions: list[str] = []
            try:
                cp["context_bytes"] = int(data["context_bytes"])
            except (ValueError, TypeError):
                cp["context_bytes"] = None
                cp_omissions.append("context_bytes_non_numeric")
            if data.get("prompt_tokens_estimated") is not None:
                try:
                    cp["prompt_tokens_estimated"] = int(data["prompt_tokens_estimated"])
                except (ValueError, TypeError):
                    cp["prompt_tokens_estimated"] = None
                    cp_omissions.append("prompt_tokens_estimated_non_numeric")
            srcs = data.get("sources")
            if isinstance(srcs, list):
                cp["sources_count"] = len(srcs)
            if data.get("truncated") is not None:
                cp["truncated"] = bool(data["truncated"])
            if cp_omissions:
                cp["omissions"] = cp_omissions
            # A later hydrate for the same attempt (e.g. in a rehydration
            # cycle where dispatch increments the attempt before the second
            # hydrate — which cannot happen today but would be silent data
            # loss if it ever did).  Record the count so the overwrite is
            # visible in the receipt.
            existing = attempts[current].get("context_pack")
            if existing is not None:
                cp["hydrate_count"] = existing.get("hydrate_count", 1) + 1
            attempts[current]["context_pack"] = cp
        elif event.type == "lint_autofix" and current in attempts:
            row = attempts[current]
            row["lint_autofix"] = {
                "files_changed": list(data.get("changed_files") or []),
                "ruff_missing": bool(data.get("ruff_missing", False)),
                "skipped": bool(data.get("skipped", False)),
            }
    # route identity tracking: compute route_identity for each attempt (after all events processed)
    for row in attempts.values():
        if "_terminal_ok" in row:
            ok = row.pop("_terminal_ok")
            error = row.pop("_terminal_error")
            reported = row.pop("_terminal_reported")
            route = row["route_id"]
            failure_cat = row.get("failure_category", "")
            served_status = row.pop("_terminal_served_status", None)
            if served_status is None:
                row["route_identity"] = _classify_route_identity(
                    route, reported if reported else None, ok, error, failure_cat
                )
            elif "model_mismatch" in (error, failure_cat):
                row["route_identity"] = "mismatch"
            else:
                row["route_identity"] = _SERVED_ROUTE_IDENTITY.get(served_status, "unattested")
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
    if any(
        event.type == "eligibility" and event.data.get("selection_accounting") for event in events
    ):
        record["selection_accounting"] = _selection_accounting_for(events)
    return record


def _selection_accounting_for(events: list[RunEvent]) -> dict[str, Any]:
    from verdict.orchestration.eligibility import selection_accounting

    supply: list[Mapping[str, Any]] = []
    sent: list[str] = []
    authority_expired = False
    for event in events:
        data = event.data
        if event.type == "eligibility" and isinstance(data.get("selection_accounting"), Mapping):
            for key in ("known_unavailable_reasons", "unqualified_reasons"):
                supply.extend(
                    {**row, "failed_stage": "TASK_ELIGIBLE"}
                    for row in data["selection_accounting"].get(key, [])
                )
        if event.type == "dispatch":
            sent.append(str(data.get("route_id") or ""))
            supply.append({**data, "admitted": True})
        if (
            data.get("termination_reason")
            in {"budget_exhausted", "deadline_exceeded", "permit_expired", "passport_expired"}
            or data.get("reason") == "pre-dispatch recheck: no_fresh_passport"
        ):
            authority_expired = True
    return selection_accounting(supply, attempted=sent, authority_expired=authority_expired)


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
                "findings_lost",
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
    # Issue #1137: dropped comment calls, recorded only when present so older
    # receipts recompute byte-for-byte.
    lost = source.get("findings_lost")
    if isinstance(lost, int) and not isinstance(lost, bool) and lost > 0:
        extra["findings_lost"] = lost
    # Token budget: recorded only when present so older receipts still verify.
    budget_used = source.get("token_budget_used")
    if isinstance(budget_used, int) and not isinstance(budget_used, bool) and budget_used > 0:
        extra["token_budget_used"] = budget_used
    budget_source = source.get("token_budget_source")
    if isinstance(budget_source, str) and budget_source:
        extra["token_budget_source"] = budget_source
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


def build_run_receipt(
    run_dir: Path,
    *,
    repair: bool = True,
    _preread_events: list[RunEvent] | None = None,
    _preread_events_digest: str | None = None,
) -> dict[str, Any]:
    run_dir = Path(run_dir)
    events_path = run_dir / EVENTS_FILE
    if _preread_events is not None:
        events = _preread_events
    else:
        if not events_path.exists():
            raise OrchestrationError(f"missing {EVENTS_FILE} in {run_dir}")
        events = EventLog(events_path, repair=repair).read() if events_path.stat().st_size else []
    # A run that ended before a plan existed (startup, admission or planning
    # BLOCKED) has no graph or an empty one. It still gets a receipt with no
    # nodes, so its cause is verifiable like any other run's.
    graph_path = run_dir / GRAPH_FILE
    graph_raw = _load_json(graph_path) if graph_path.exists() else {}
    graph = (
        WorkGraph.from_dict(
            {k: v for k, v in graph_raw.items() if k != "run_id"}, allow_merge_only_owned_files=True
        )
        if graph_raw.get("nodes")
        else None
    )
    if graph is None:
        # Only a run that never executed a node and settled BLOCKED is pre-plan;
        # a planned run whose graph is missing stays unverifiable.
        last = next((e for e in reversed(events) if e.type == "run_finished"), None)
        if (
            last is None
            or last.data.get("outcome") != RunOutcome.BLOCKED.value
            or any(e.node_id for e in events)
            or any(e.type == "plan_ready" for e in events)
        ):
            raise OrchestrationError(f"missing or empty {GRAPH_FILE} for a run that was planned")
    plan_nodes = graph.nodes if graph is not None else ()

    by_node: dict[str, list[RunEvent]] = {n.node_id: [] for n in plan_nodes}
    for event in events:
        if event.node_id in by_node:
            by_node[event.node_id].append(event)
    nodes = [_node_record(n.node_id, n.kind.value, by_node[n.node_id]) for n in plan_nodes]

    started = next((e for e in events if e.type == "run_started"), None)
    finished = [e for e in events if e.type == "run_finished"]
    barriers: dict[str, dict[str, Any]] = {}
    for event in events:
        if event.type == "barrier":
            name = str(event.data.get("name") or event.node_id or "integration")
            barriers[name] = {"name": name, "ok": event.data.get("ok") is True, "seq": event.seq}
    declared = sorted({str(n.barrier) for n in plan_nodes if n.barrier})
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
    no_change_nodes = _no_change_implement_nodes(graph, events, nodes) if graph else []

    receipt: dict[str, Any] = {
        "schema": RECEIPT_SCHEMA,
        # run_started is digest-bound and names the run dir; graph.json is not
        # and may keep a planner-input id, so it is only a pre-start fallback.
        "run_id": str(
            (started.data.get("run_id") if started else "")
            or graph_raw.get("run_id")
            or run_dir.name
        ),
        "goal": graph.goal if graph is not None else str(graph_raw.get("goal") or ""),
        "graph_digest": graph.digest() if graph is not None else None,
        "topology": graph.topology.value if graph is not None else None,
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
            if e.type
            in {"plan_started", "plan_repair_started", "plan_repair_terminal", "plan_context"}
        ],
        "decision_signals": _free("decision_signals") or None,  # SHADOW signals (optional)
        "claimed_outcome": str(finished[-1].data.get("outcome", "")) if finished else "",
        **(
            {"claimed_reason": redact_text(str(finished[-1].data.get("reason", "")))[:500]}
            if graph is None and finished
            else {}
        ),
        "event_count": len(events),
        "events_digest": (
            _preread_events_digest
            if _preread_events_digest is not None
            else _sha256_file(events_path)
        ),
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


# BOD-437: orchestration failure categories mapped onto the fixed BOD-353 classes.
_CATEGORY_CLASS: Mapping[str, str] = {
    "rate_limited": "quota_429",
    "quota_exhausted": "quota_429",
    "payment_required": "quota_429",
    "authentication": "auth_error",
    "permission": "auth_error",
    "timeout": "timeout",
    "stalled": "timeout",
    "upstream_temporary": "provider_5xx",
    "gateway_busy": "provider_5xx",
    "transport_temporary": "stream_drop",
    "no_final_answer": "stream_drop",
    "empty_output": "stream_drop",
    "model_unavailable": "model_cooldown",
    "verification_failed": "test_failed",
    "environment_error": "env_error",
    "ownership_violation": "spec_violation",
}


def _is_model_attempt(attempt: Mapping[str, Any]) -> bool:
    route = str(attempt.get("route_id") or "")
    return (
        bool(route)
        and not route.startswith("local/")
        and (attempt.get("route_identity") != "mechanical")
    )


def run_attempt_records(run_dir: Path, receipt: Mapping[str, Any]) -> list[AttemptRecord]:
    """Ledger rows for one run: a terminal row per model attempt, plus a checker row.

    The terminal row is the worker's own execution result and never carries
    quality credit. A ``checker_result`` row exists only where the runtime's
    independent verification command ran after that attempt's successful
    terminal; it carries 1 (exit 0) or 0 and names the receipt's
    ``events_digest`` as evidence. Quota, auth, transport and other operational
    failures get no checker row, so they never enter a quality denominator.
    """
    from verdict.candidate_pool import fingerprint_task

    run_id = str(receipt.get("run_id") or "")
    digest = str(receipt.get("events_digest") or "")
    goal = str(receipt.get("goal") or "")
    family = fingerprint_task(goal).task_family if goal.strip() else None
    events = EventLog(Path(run_dir) / EVENTS_FILE).read()
    records: list[AttemptRecord] = []
    for node in receipt.get("nodes") or []:
        node_id = str(node.get("node_id") or "")
        kind = str(node.get("kind") or "other")
        node_events = [e for e in events if e.node_id == node_id]
        for attempt in node.get("attempts") or []:
            if not _is_model_attempt(attempt):
                continue
            number = int(attempt["attempt"])
            outcome = str(attempt.get("outcome") or "")
            terminal = next(
                (e for e in node_events if e.type == "terminal" and _attempt_of(e, 1) == number),
                None,
            )
            if outcome == "success":
                label, failure_class = "accepted", "success"
            elif outcome == "failure":
                category = str(attempt.get("failure_category") or "")
                label, failure_class = "failed", _CATEGORY_CLASS.get(category, "other")
            else:
                label, failure_class = "abandoned", "other"
            usage = attempt.get("usage") if isinstance(attempt.get("usage"), Mapping) else {}
            common: dict[str, Any] = {
                "task_kind": kind,
                "harness": str(attempt.get("harness") or "verdict-orchestrate"),
                "model": str(attempt.get("executed_model") or attempt["route_id"]),
                "source": "verdict.orchestration.receipt",
                "run_id": run_id,
                "node_id": node_id,
                "attempt_id": str(number),
                "task_family": family,
                "evidence_level": attempt.get("evidence_level"),
            }
            records.append(
                build_attempt_record(
                    **common,
                    outcome=label,
                    failure_class=failure_class,
                    record_kind="terminal",
                    duration_s=attempt.get("duration_seconds"),
                    tokens_in=usage.get("input_tokens"),
                    tokens_out=usage.get("output_tokens"),
                    cost_usd=usage.get("cost_usd"),
                    ts=terminal.at if terminal is not None else None,
                )
            )
            # The checker judges the worker's successful terminal, even when the
            # receipt later marks the attempt failed because that check failed.
            if terminal is None or terminal.data.get("ok") is not True:
                continue
            following = [e for e in node_events if e.seq > terminal.seq]
            stop = next((e.seq for e in following if e.type == "dispatch"), None)
            checks = [
                e
                for e in following
                if e.type == "verify"
                and (stop is None or e.seq < stop)
                and not str(e.data.get("command") or "").startswith("(none")
            ]
            if not checks:
                continue
            passed = checks[-1].data.get("ok") is True
            records.append(
                build_attempt_record(
                    **common,
                    outcome="accepted" if passed else "failed",
                    failure_class="success" if passed else "test_failed",
                    record_kind="checker_result",
                    checker_digest=digest,
                    quality_credit=1.0 if passed else 0.0,
                    ts=checks[-1].at,
                )
            )
    return records


def _record_run_attempts(run_dir: Path, receipt: Mapping[str, Any]) -> None:
    """Append this run's attempt and checker rows to the shared ledger (idempotent).

    Also append the run's absolute path to the host-wide run index, so the
    outcome history reader finds runs written under any ``--runs-dir``.
    """
    from verdict.paths import verdict_home

    home = verdict_home()
    append_new_attempt_records(run_attempt_records(run_dir, receipt), store_dir=home)
    append_run_index(home, run_dir)


def append_run_index(home: Path, run_dir: Path) -> None:
    """Append one absolute run dir line to ``<home>/verified-runs.txt``.

    Same pattern as ``outcome_records._append_lines``: the whole line goes out
    in one ``os.write`` on an ``O_APPEND`` fd held under an exclusive
    ``flock``, so concurrent runs never interleave a line.
    """
    line = f"{Path(run_dir).resolve()}\n".encode()
    home.mkdir(parents=True, exist_ok=True)
    fd = os.open(home / VERIFIED_RUNS_FILE, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        if os.write(fd, line) != len(line):
            raise OSError(f"short write to {home / VERIFIED_RUNS_FILE}")
    finally:
        os.close(fd)


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
    try:
        _record_run_attempts(run_dir, receipt)
    except Exception as exc:  # the ledger must never block the authoritative receipt
        import sys

        print(
            f"warning: could not record run attempts in the outcome ledger: {exc}", file=sys.stderr
        )
    _fsync_dir(run_dir)
    return target


def verify_run_receipt(run_dir: Path, *, repair: bool = True) -> list[str]:
    """Recompute the receipt from source files; empty list means valid.

    Pass ``repair=False`` for read-only callers (e.g. the API) to prevent
    ``EventLog.__init__`` from truncating a torn tail on disk.  The digest
    check will still catch any changes to events.jsonl; a torn tail caused
    by a mid-write crash appears as an ``events_digest mismatch``.

    The ``repair=False`` path reads both files with ``O_NOFOLLOW`` and
    delegates to ``verify_run_receipt_from_bytes`` so the same bytes are
    used for every check — there is no second read of either file.
    """
    run_dir = Path(run_dir)
    path = run_dir / RECEIPT_FILE
    if not path.exists():
        return [f"missing {RECEIPT_FILE}"]
    if repair:
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
        digest = _sha256_file(events_path)
        if stored.get("events_digest") != digest:
            problems.append(
                "events_digest mismatch: events.jsonl changed after receipt was written"
            )
        try:
            fresh = build_run_receipt(run_dir, repair=repair)
        except OrchestrationError as exc:
            return [*problems, f"cannot rebuild receipt: {exc}"]
        for key in sorted(set(fresh) | set(stored)):
            if key == "events_digest":
                continue
            if fresh.get(key) != stored.get(key):
                problems.append(f"{key} mismatch: stored receipt differs from recomputed evidence")
        return problems
    else:
        # Non-repair (API) path: read both files with O_NOFOLLOW, then
        # delegate to verify_run_receipt_from_bytes so the same bytes are
        # used for the digest check and the structural comparison.
        try:
            receipt_raw = _read_bytes_nofollow(path)
        except OrchestrationError as exc:
            return [str(exc)]
        events_path = run_dir / EVENTS_FILE
        events_raw: bytes | None = None
        with contextlib.suppress(OrchestrationError):
            events_raw = _read_bytes_nofollow(events_path)
        return verify_run_receipt_from_bytes(receipt_raw, events_raw, run_dir)


def verify_run_receipt_from_bytes(
    receipt_bytes: bytes, events_bytes: bytes | None, run_dir: Path
) -> list[str]:
    """Verify a receipt using bytes already in memory; no second file read.

    Both ``receipt_bytes`` and ``events_bytes`` must have been read from
    their respective files with ``O_NOFOLLOW`` by the caller.  Pass
    ``events_bytes`` as ``None`` when the caller could not open
    ``events.jsonl`` (missing, symlink, or other OS error); a problem is
    recorded without touching the filesystem.

    Equivalent to ``verify_run_receipt(repair=False)`` but takes the bytes
    already in memory so the receipt returned to the caller and the
    verification result always describe the same content — no second read
    of either file.
    """
    run_dir = Path(run_dir)
    try:
        stored_val: Any = json.loads(receipt_bytes)
    except ValueError as exc:
        return [f"cannot read {RECEIPT_FILE}: {exc}"]
    if not isinstance(stored_val, dict):
        return [f"{RECEIPT_FILE} must contain a JSON object"]
    stored: dict[str, Any] = stored_val

    problems: list[str] = []
    if stored.get("schema") != RECEIPT_SCHEMA:
        problems.append(f"unexpected schema {stored.get('schema')!r}")

    if events_bytes is None:
        return [*problems, f"missing {EVENTS_FILE}"]

    digest = "sha256:" + hashlib.sha256(events_bytes).hexdigest()
    if stored.get("events_digest") != digest:
        problems.append("events_digest mismatch: events.jsonl changed after receipt was written")

    try:
        preread: list[RunEvent] = _parse_events_bytes(events_bytes, run_dir / EVENTS_FILE)
    except OrchestrationError as exc:
        return [*problems, f"cannot rebuild receipt: {exc}"]

    try:
        fresh = build_run_receipt(
            run_dir, repair=False, _preread_events=preread, _preread_events_digest=digest
        )
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
        # A run that ended before planning keeps its own recorded cause.
        if receipt.get("graph_digest") is None and receipt.get("claimed_reason"):
            return blocked, str(receipt["claimed_reason"])
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


def compute_run_metrics(receipt: Mapping[str, Any]) -> dict[str, Any]:
    """Compute efficiency metrics from a verified receipt.

    ``accepted_tasks`` counts implement/integrate nodes only when
    ``completion_verdict`` recomputes COMPLETE (the stored ``outcome`` is not
    trusted). ``dollars`` is None when any model attempt's cost is unknown,
    never 0. Shares are fractions of model attempts; raw counts are returned
    too so aggregation never round-trips through a float.
    """
    outcome, _reason = completion_verdict(receipt)
    gated = {NodeKind.IMPLEMENT.value, NodeKind.INTEGRATE.value}
    accepted_tasks = (
        sum(1 for n in receipt.get("nodes") or [] if n.get("kind") in gated)
        if outcome == RunOutcome.COMPLETE.value
        else 0
    )

    # 2. Dollars and cost tracking across all model attempts
    total_cost = 0.0
    has_unknown_cost = False
    total_model_attempts = 0
    free_attempts = 0
    subscription_attempts = 0
    metered_attempts = 0
    unknown_capacity_attempts = 0

    for node in receipt.get("nodes") or []:
        for attempt in node.get("attempts") or []:
            # Count as model attempt if it has a route_id and is not local/mechanical
            route_id = str(attempt.get("route_id") or "")
            if (
                not route_id
                or route_id.startswith("local/")
                or attempt.get("route_identity") == "mechanical"
            ):
                continue
            total_model_attempts += 1

            capacity = str(attempt.get("capacity_class") or "unknown").lower()
            if capacity == "free":
                free_attempts += 1
            elif capacity == "subscription":
                subscription_attempts += 1
            elif capacity == "metered":
                metered_attempts += 1
            else:
                unknown_capacity_attempts += 1

            usage = attempt.get("usage")
            if isinstance(usage, Mapping):
                cost = usage.get("cost_usd")
                if cost is None:
                    has_unknown_cost = True
                elif isinstance(cost, int | float):
                    total_cost += float(cost)
                else:
                    has_unknown_cost = True
            else:
                has_unknown_cost = True

    dollars: float | None = None if has_unknown_cost else total_cost

    # 3. Tasks per dollar
    if dollars is not None and dollars > 0:
        tasks_per_dollar: float | str = accepted_tasks / dollars
    else:
        tasks_per_dollar = "undefined"

    # 4. Free share and other capacity shares
    if total_model_attempts > 0:
        free_share = free_attempts / total_model_attempts
        subscription_share = subscription_attempts / total_model_attempts
        metered_share = metered_attempts / total_model_attempts
        unknown_capacity_share = unknown_capacity_attempts / total_model_attempts
    else:
        free_share = 0.0
        subscription_share = 0.0
        metered_share = 0.0
        unknown_capacity_share = 0.0

    return {
        "accepted_tasks": accepted_tasks,
        "dollars": dollars,
        "has_unknown_cost": has_unknown_cost,
        "tasks_per_dollar": tasks_per_dollar,
        "free_share": free_share,
        "subscription_share": subscription_share,
        "metered_share": metered_share,
        "unknown_capacity_share": unknown_capacity_share,
        "total_model_attempts": total_model_attempts,
        "free_attempts": free_attempts,
        "subscription_attempts": subscription_attempts,
        "metered_attempts": metered_attempts,
        "unknown_capacity_attempts": unknown_capacity_attempts,
        "run_id": str(receipt.get("run_id") or ""),
        "outcome": outcome,
    }


def extend_run_metrics(receipt: Mapping[str, Any]) -> dict[str, Any]:
    """Extend compute_run_metrics with per-role splits, frontier tokens, and
    first-attempt pass rate.  Returns everything compute_run_metrics returns
    plus the following keys.

    Per-role splits (``_by_role`` and ``_by_cost_and_role``):
    - ``attempts_by_role``: model attempt counts keyed by NodeKind value.
    - ``accepted_nodes_by_role``: accepted node counts by role (non-zero only
      for implement/integrate when the run is COMPLETE).
    - ``attempts_by_cost_and_role``: nested ``{role: {cost_class: count}}``.

    Frontier tokens (subscription + metered attempts on DAG nodes):
    - ``frontier_tokens``: ``{input, output, total, per_accepted_task}`` where
      ``total`` and ``per_accepted_task`` are ``None`` when any frontier attempt
      lacks token data.  Planner and reviewer individual call tokens are not
      currently recorded in receipts; ``planner_tokens_note`` documents this.

    First-attempt pass rate (``first_attempt_pass_rate``):
    - ``by_role``: fraction of nodes where attempt 1 succeeded and the node
      was validated, keyed by role.
    - ``by_cost``: the same fraction, keyed by the cost class of the first
      attempt.  A worker's ok exit alone is never a pass (issue #1315).

    Research-duplication share:
    - ``research_duplication_note``: explains it is not tracked in receipts;
      see ``verdict/orchestration/research_metrics.py`` for the per-call
      measurement tool.
    """
    base = compute_run_metrics(receipt)
    outcome = base["outcome"]
    gated = {NodeKind.IMPLEMENT.value, NodeKind.INTEGRATE.value}
    is_complete = outcome == RunOutcome.COMPLETE.value

    # Accumulators keyed by role (NodeKind value string).
    _roles = [k.value for k in NodeKind]
    attempts_by_role: dict[str, int] = {r: 0 for r in _roles}
    accepted_nodes_by_role: dict[str, int] = {r: 0 for r in _roles}
    attempts_by_cost_and_role: dict[str, dict[str, int]] = {
        r: {"free": 0, "subscription": 0, "metered": 0, "unknown": 0} for r in _roles
    }

    # Frontier-token accumulation (subscription + metered).
    frontier_input: int = 0
    frontier_output: int = 0
    frontier_tokens_known: bool = True  # becomes False on any missing count

    # First-attempt pass rate.
    first_pass_by_role: dict[str, list[bool]] = {r: [] for r in _roles}
    first_pass_by_cost: dict[str, list[bool]] = {
        "free": [],
        "subscription": [],
        "metered": [],
        "unknown": [],
    }

    for node in receipt.get("nodes") or []:
        role = str(node.get("kind") or "")
        if role not in attempts_by_role:
            continue

        attempts = node.get("attempts") or []

        if is_complete and role in gated:
            accepted_nodes_by_role[role] += 1

        # First-attempt pass rate for this node.
        model_attempts = [
            a
            for a in attempts
            if str(a.get("route_id") or "")
            and not str(a.get("route_id") or "").startswith("local/")
            and a.get("route_identity") != "mechanical"
        ]
        if model_attempts:
            first = model_attempts[0]
            # A worker that returned ok is not yet accepted work: the node must
            # also have been validated.  A rejected attempt already reads
            # "failure" (the runtime emits a failure event after _validate), but
            # one whose validation never finished (for example the node driver
            # crashed after the worker's terminal) keeps "success" on a node
            # that is not VALIDATED.
            passed = (
                first.get("outcome") == "success"
                and node.get("final_state") == NodeState.VALIDATED.value
            )
            first_pass_by_role[role].append(passed)
            cost_cls = str(first.get("capacity_class") or "unknown").lower()
            if cost_cls not in first_pass_by_cost:
                cost_cls = "unknown"
            first_pass_by_cost[cost_cls].append(passed)

        for attempt in attempts:
            route_id = str(attempt.get("route_id") or "")
            if (
                not route_id
                or route_id.startswith("local/")
                or attempt.get("route_identity") == "mechanical"
            ):
                continue

            attempts_by_role[role] += 1
            capacity = str(attempt.get("capacity_class") or "unknown").lower()
            if capacity not in ("free", "subscription", "metered"):
                capacity = "unknown"
            attempts_by_cost_and_role[role][capacity] += 1

            if capacity in ("subscription", "metered"):
                usage = attempt.get("usage")
                if isinstance(usage, Mapping):
                    tin = usage.get("input_tokens")
                    tout = usage.get("output_tokens")
                    if isinstance(tin, int) and isinstance(tout, int):
                        frontier_input += tin
                        frontier_output += tout
                    else:
                        frontier_tokens_known = False
                else:
                    frontier_tokens_known = False

    # Frontier token summary.
    if frontier_tokens_known:
        frontier_total: int | None = frontier_input + frontier_output
        accepted = base["accepted_tasks"]
        fpt: float | None = frontier_total / accepted if accepted > 0 else None
    else:
        frontier_total = None
        fpt = None

    frontier_tokens: dict[str, Any] = {
        "input": frontier_input if frontier_tokens_known else None,
        "output": frontier_output if frontier_tokens_known else None,
        "total": frontier_total,
        "per_accepted_task": fpt,
    }

    # Reviewer token budget (approximate; individual call tokens not in receipt).
    review = receipt.get("review") or {}
    reviewer_budget_used = review.get("token_budget_used")

    # First-attempt pass rate fractions and raw counts for weighted aggregation.
    def _rate(lst: list[bool]) -> float | None:
        return sum(lst) / len(lst) if lst else None

    first_attempt_pass_rate: dict[str, Any] = {
        "by_role": {r: _rate(first_pass_by_role[r]) for r in _roles},
        "by_cost": {c: _rate(first_pass_by_cost[c]) for c in first_pass_by_cost},
    }

    # Raw success/eligible counts for weighted aggregation across runs.
    # Averaging fractions gives wrong results when runs differ in node count.
    first_attempt_pass_counts: dict[str, Any] = {
        "by_role": {
            r: {"success": sum(first_pass_by_role[r]), "eligible": len(first_pass_by_role[r])}
            for r in _roles
        },
        "by_cost": {
            c: {"success": sum(v), "eligible": len(v)} for c, v in first_pass_by_cost.items()
        },
    }

    return {
        **base,
        "attempts_by_role": attempts_by_role,
        "accepted_nodes_by_role": accepted_nodes_by_role,
        "attempts_by_cost_and_role": attempts_by_cost_and_role,
        "frontier_tokens": frontier_tokens,
        "planner_attempts_count": len(receipt.get("planner_attempts") or []),
        "planner_tokens_note": (
            "Planner call tokens are not recorded in the receipt; "
            "see planner_attempts_count for call frequency."
        ),
        "reviewer_token_budget_used": reviewer_budget_used
        if isinstance(reviewer_budget_used, int)
        else None,
        "first_attempt_pass_rate": first_attempt_pass_rate,
        "first_attempt_pass_counts": first_attempt_pass_counts,
        "research_duplication_note": (
            "Repeated-research token share is not tracked in receipts. "
            "See verdict/orchestration/research_metrics.py for the per-call tool."
        ),
    }
