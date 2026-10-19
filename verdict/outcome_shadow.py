"""BOD-304 shadow outcome observations for candidate-pool receipts.

Shadow-only: never mutates live shortlist/scores. Counterfactual would_rank is
a live-shortlist rerank diagnostic (ADR-018: counterfactual ≠ promotion).

Metric (normative for this slice): ``implement_validated_accept_reject_v1``.
Acceptance requires a matching VALIDATED/proof event strictly before ``as_of``.

Persistence (BOD-426): when ``VERDICT_OUTCOME_SHADOW_ROOTS`` is set, every
shadow block attached to an admission receipt is also appended as one JSON
line to ``outcome_shadow.jsonl`` next to the first configured roots entry (see
:func:`resolve_outcome_shadow_sink`). ``would_rank`` is only ever a computed
rerank; when no counterfactual was computed (no roots, shadow error) it is
``null``, never a copy of ``actual_rank``.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from verdict.candidate_pool import OutcomeObservation, ShortlistEntry, _fuse_score
from verdict.orchestration.receipt import (
    EVENTS_FILE,
    RECEIPT_FILE,
    run_attempt_records,
    verify_run_receipt,
)
from verdict.outcome_records import attempt_records_path
from verdict.paths import verdict_home

METRIC_NAME = "implement_validated_accept_reject_v1"
WOULD_RANK_SCOPE = "live_shortlist_only"
ENV_ROOTS = "VERDICT_OUTCOME_SHADOW_ROOTS"
SINK_FILENAME = "outcome_shadow.jsonl"
SINK_SCHEMA = "verdict.outcome_shadow.sink/v1"

_LOG = logging.getLogger(__name__)
_SINK_LOCK = threading.Lock()


class OutcomeShadowNotConfiguredError(ValueError):
    """Raised when a counterfactual is requested without any shadow roots.

    ``build_outcome_shadow`` refuses to fabricate ``would_rank`` from
    ``actual_rank``; :func:`attach_outcome_shadow` records the disabled state
    with ``would_rank: null`` instead.
    """


# Bounds applied BEFORE verify_run_receipt / receipt rebuild.
DEFAULT_MAX_ROOTS = 8
DEFAULT_MAX_EVENTS_BYTES = 2_000_000
DEFAULT_MAX_EVENTS_LINES = 20_000
DEFAULT_MAX_RECEIPT_BYTES = 512_000

_VALIDATED_STATES = frozenset({"VALIDATED"})
_REJECT_STATES = frozenset({"TERMINAL_FAILURE", "REJECTED", "BLOCKED"})
_SUCCESS_TERMINAL = frozenset({"TERMINAL_SUCCESS", "VALIDATED"})
_FAILURE_TERMINAL = frozenset({"TERMINAL_FAILURE", "REJECTED", "BLOCKED"})


def _parse_ts(raw: Any) -> datetime | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = raw.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def resolve_outcome_shadow_roots(
    configured: Sequence[Path | str] | None = None, *, env: Mapping[str, str] | None = None
) -> tuple[Path, ...]:
    """Exact run directories only. None/empty → disabled (default empty).

    When ``configured`` is None, read ``VERDICT_OUTCOME_SHADOW_ROOTS`` (OS
    path-sep list, or a single manifest file of one path per line). Explicit
    empty sequence disables even if env is set.
    """
    if configured is not None:
        return _canonicalize_roots(configured)
    source = os.environ if env is None else env
    raw = (source.get(ENV_ROOTS) or "").strip()
    if not raw:
        return ()
    parts: list[str] = []
    for chunk in raw.split(os.pathsep):
        chunk = chunk.strip()
        if not chunk:
            continue
        path = Path(chunk).expanduser()
        if path.is_file():
            try:
                for line in path.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if line and not line.startswith("#"):
                        parts.append(line)
            except OSError:
                continue
        else:
            parts.append(chunk)
    return _canonicalize_roots(parts)


def resolve_outcome_shadow_sink(*, env: Mapping[str, str] | None = None) -> Path | None:
    """Return the jsonl sink path derived from ``VERDICT_OUTCOME_SHADOW_ROOTS``.

    Unset or blank → ``None`` (no sink, matching the default-empty roots). When
    set, the sink is ``outcome_shadow.jsonl`` in the directory that contains
    the first entry: the runs directory for a run-dir entry, or the manifest's
    directory for a manifest file. It is never written inside a verified run
    directory, so run evidence is left untouched.
    """
    source = os.environ if env is None else env
    raw = (source.get(ENV_ROOTS) or "").strip()
    for chunk in raw.split(os.pathsep):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            first = Path(chunk).expanduser().resolve()
        except OSError:
            return None
        return first.parent / SINK_FILENAME
    return None


def append_outcome_shadow_record(
    sink: Path, shadow: Mapping[str, Any], *, recorded_at: datetime | None = None
) -> bool:
    """Append one shadow record as a single JSON line. Never raises.

    Append-only and atomic-ish: the file is opened ``O_APPEND`` (and
    ``O_NOFOLLOW`` where available) and each record is one complete line
    written with one ``write`` call under a process lock, so concurrent
    writers never interleave partial lines. The sink directory is never
    created; a missing directory is logged and the record is skipped.

    Every sink interaction (serialization, the directory check, open and
    write) runs inside this error boundary: any failure is logged as a
    warning and returns ``False``, so the caller keeps its in-memory shadow
    block (D34 fail-safe).
    """
    try:
        return _append_outcome_shadow_record(sink, shadow, recorded_at=recorded_at)
    except Exception as exc:
        _LOG.warning(
            "outcome_shadow sink: record not appended to %s (%s)", sink, type(exc).__name__
        )
        return False


def _append_outcome_shadow_record(
    sink: Path, shadow: Mapping[str, Any], *, recorded_at: datetime | None
) -> bool:
    when = recorded_at or datetime.now(timezone.utc)
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    record = {
        "schema": SINK_SCHEMA,
        "recorded_at": when.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "outcome_shadow": dict(shadow),
    }
    try:
        line = (
            json.dumps(
                record, ensure_ascii=True, sort_keys=True, separators=(",", ":"), default=str
            )
            + "\n"
        ).encode("ascii")
    except (TypeError, ValueError) as exc:
        _LOG.warning("outcome_shadow sink: record not serializable (%s)", type(exc).__name__)
        return False
    if not sink.parent.is_dir():
        _LOG.warning(
            "outcome_shadow sink: directory %s does not exist; record skipped", sink.parent
        )
        return False
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    try:
        with _SINK_LOCK:
            fd = os.open(sink, flags, 0o600)
            try:
                written = os.write(fd, line)
            finally:
                os.close(fd)
    except OSError as exc:
        _LOG.warning("outcome_shadow sink: cannot append to %s (%s)", sink, type(exc).__name__)
        return False
    if written != len(line):
        _LOG.warning("outcome_shadow sink: short write to %s (%d/%d)", sink, written, len(line))
        return False
    return True


def _canonicalize_roots(items: Sequence[Path | str]) -> tuple[Path, ...]:
    seen: set[Path] = set()
    out: list[Path] = []
    for item in items:
        try:
            path = Path(item).expanduser().resolve()
        except OSError:
            continue
        if path in seen:
            continue
        seen.add(path)
        out.append(path)
    return tuple(out)


@dataclass(frozen=True)
class ShadowAttemptRow:
    run_id: str
    node_id: str
    attempt: int
    route_id: str
    role: str
    task_family: str
    fingerprint_digest: str
    classification: str
    outcome_ts: datetime | None
    events_digest: str
    source_id: str


def _preverify_bounds(
    run_dir: Path, *, max_events_bytes: int, max_events_lines: int, max_receipt_bytes: int
) -> str | None:
    """Return skip reason if bounds fail; None if OK to call verifier."""
    if not run_dir.is_dir():
        return "not_a_directory"
    receipt = run_dir / RECEIPT_FILE
    events = run_dir / EVENTS_FILE
    try:
        if not receipt.is_file() or not events.is_file():
            return "missing_receipt_or_events"
        r_size = receipt.stat().st_size
        e_size = events.stat().st_size
    except OSError:
        return "inaccessible"
    if r_size > max_receipt_bytes:
        return "receipt_bytes_exceeded"
    if e_size > max_events_bytes:
        return "events_bytes_exceeded"
    # Line cap without full parse: count newlines up to limit+1.
    try:
        nlines = 0
        with events.open("rb") as handle:
            for _ in handle:
                nlines += 1
                if nlines > max_events_lines:
                    return "events_lines_exceeded"
    except OSError:
        return "inaccessible"
    return None


def _load_json(path: Path) -> dict[str, Any] | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    return raw if isinstance(raw, dict) else None


def _iter_events(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except ValueError:
                continue
            if isinstance(item, dict):
                rows.append(item)
    except OSError:
        return []
    return rows


def _family_from_receipt(receipt: Mapping[str, Any]) -> tuple[str, str] | None:
    from verdict.candidate_pool import fingerprint_task

    goal = receipt.get("goal")
    if not isinstance(goal, str) or not goal.strip():
        return None
    fp = fingerprint_task(goal)
    return fp.task_family, fp.digest


def _event_state_times(
    events: Sequence[Mapping[str, Any]], *, node_id: str, attempt: int
) -> dict[str, datetime]:
    """Map state name -> earliest event at for this node/attempt."""
    out: dict[str, datetime] = {}
    for ev in events:
        if str(ev.get("node_id") or "") != node_id:
            continue
        if str(ev.get("type") or "") != "node_state":
            continue
        raw_data = ev.get("data")
        data: dict[str, Any] = raw_data if isinstance(raw_data, dict) else {}
        try:
            ev_attempt = int(data.get("attempt", -1))
        except (TypeError, ValueError):
            continue
        if ev_attempt != attempt:
            continue
        state = str(data.get("state") or "")
        ts = _parse_ts(ev.get("at"))
        if not state or ts is None:
            continue
        prev = out.get(state)
        if prev is None or ts < prev:
            out[state] = ts
    return out


def _classify_attempt(
    *,
    role: str,
    route_id: str,
    attempt_outcome: str,
    node_final_state: str,
    state_times: Mapping[str, datetime],
    as_of: datetime,
) -> tuple[str, datetime | None]:
    """Return (classification, outcome_ts) using only events before as_of."""
    if not route_id.strip():
        return "mechanical_empty_route", None

    validated_at = state_times.get("VALIDATED")
    # Cutoff-qualified acceptance: VALIDATED/proof must itself be before as_of.
    if (
        validated_at is not None
        and validated_at < as_of
        and (
            attempt_outcome == "success"
            or any(st in state_times and state_times[st] < as_of for st in _SUCCESS_TERMINAL)
        )
    ):
        return "accepted_attempt", validated_at

    # Rejected node attempts (failure terminals before as_of).
    if node_final_state in _REJECT_STATES:
        fail_at = None
        for st in _FAILURE_TERMINAL:
            ts = state_times.get(st)
            if ts is not None and ts < as_of:
                fail_at = ts if fail_at is None else min(fail_at, ts)
        # Also accept attempt-level failure without state map if node already rejected
        # and we have any pre-as_of failure-ish signal; prefer event time.
        if fail_at is not None and attempt_outcome in {"failure", "fail", "error"}:
            return "rejected_attempt", fail_at
        if fail_at is not None and attempt_outcome == "success":
            # success attempt on rejected node is incomplete for metric
            return "incomplete", fail_at
        if attempt_outcome in {"failure", "fail", "error"}:
            # Node rejected in current receipt but no pre-as_of failure event → incomplete
            return "incomplete", None

    # Running / planned style
    if node_final_state == "RUNNING" or attempt_outcome == "running":
        return "timeout", state_times.get("RUNNING")
    if node_final_state in {"PLANNED", "ADMITTED", "DISPATCHED"}:
        return "cancelled", None

    # Failed before eventual success (node validated in current receipt, but this
    # attempt failed). Audit only — never metric accept/reject.
    if attempt_outcome in {"failure", "fail", "error"}:
        # Prefer failure terminal time before as_of if present
        fail_at = None
        for st in _FAILURE_TERMINAL:
            ts = state_times.get(st)
            if ts is not None and ts < as_of:
                fail_at = ts if fail_at is None else min(fail_at, ts)
        # TERMINAL_SUCCESS later on another attempt does not upgrade this one.
        return "first_attempt_failed", fail_at

    # TERMINAL_SUCCESS before as_of but VALIDATED after/missing → not accepted
    term_at = state_times.get("TERMINAL_SUCCESS")
    if term_at is not None and term_at < as_of:
        return "execution_terminal_unproven", term_at

    return "incomplete", None


def load_shadow_rows_from_run(
    run_dir: Path,
    *,
    as_of: datetime,
    max_events_bytes: int = DEFAULT_MAX_EVENTS_BYTES,
    max_events_lines: int = DEFAULT_MAX_EVENTS_LINES,
    max_receipt_bytes: int = DEFAULT_MAX_RECEIPT_BYTES,
) -> tuple[tuple[ShadowAttemptRow, ...], str | None]:
    """Load per-attempt shadow rows from one run_dir. Returns (rows, skip_reason)."""
    bound = _preverify_bounds(
        run_dir,
        max_events_bytes=max_events_bytes,
        max_events_lines=max_events_lines,
        max_receipt_bytes=max_receipt_bytes,
    )
    if bound is not None:
        return (), bound

    problems = verify_run_receipt(run_dir)
    if problems:
        return (), "verify_failed"

    receipt = _load_json(run_dir / RECEIPT_FILE)
    if receipt is None:
        return (), "receipt_unreadable"
    family_info = _family_from_receipt(receipt)
    if family_info is None:
        return (), "missing_goal"
    task_family, fingerprint_digest = family_info
    run_id = str(receipt.get("run_id") or run_dir.name)
    events_digest = str(receipt.get("events_digest") or "")
    events = _iter_events(run_dir / EVENTS_FILE)

    rows: list[ShadowAttemptRow] = []
    seen: set[tuple[str, str, int, str]] = set()
    for node in receipt.get("nodes") or []:
        if not isinstance(node, dict):
            continue
        node_id = str(node.get("node_id") or "")
        role = str(node.get("kind") or node.get("role") or "")
        final_state = str(node.get("final_state") or "")
        for attempt_obj in node.get("attempts") or []:
            if not isinstance(attempt_obj, dict):
                continue
            try:
                attempt_n = int(attempt_obj.get("attempt", 0))
            except (TypeError, ValueError):
                continue
            route_id = str(attempt_obj.get("route_id") or "").strip()
            key = (run_id, node_id, attempt_n, route_id)
            if key in seen:
                continue
            seen.add(key)
            attempt_outcome = str(attempt_obj.get("outcome") or "")
            state_times = _event_state_times(events, node_id=node_id, attempt=attempt_n)
            classification, outcome_ts = _classify_attempt(
                role=role,
                route_id=route_id,
                attempt_outcome=attempt_outcome,
                node_final_state=final_state,
                state_times=state_times,
                as_of=as_of,
            )
            rows.append(
                ShadowAttemptRow(
                    run_id=run_id,
                    node_id=node_id,
                    attempt=attempt_n,
                    route_id=route_id,
                    role=role,
                    task_family=task_family,
                    fingerprint_digest=fingerprint_digest,
                    classification=classification,
                    outcome_ts=outcome_ts,
                    events_digest=events_digest,
                    source_id=f"{run_id}:{node_id}:{attempt_n}:{route_id}",
                )
            )
    return tuple(rows), None


@dataclass(frozen=True)
class CheckedAttempt:
    """One ledger checker_result that matches the verified run it names (BOD-437)."""

    run_id: str
    node_id: str
    attempt_id: str
    model: str
    task_family: str | None
    quality_credit: float
    checked_at: datetime
    checker_digest: str


def load_checked_attempts(
    run_dir: Path, *, store_dir: Path | str, as_of: datetime
) -> tuple[tuple[CheckedAttempt, ...], tuple[dict[str, str], ...]]:
    """Join the ledger's checker_result rows for one run, verified against its bundle.

    The run bundle is only read. A row joins only when the receipt verifies,
    the row's ``checker_digest`` equals the receipt ``events_digest``, the
    attempt key exists in the run, the whole row equals the checker row
    recomputed from the verified events, the verified check time is not after
    ``as_of`` and it is the first row for that key. Everything else is returned as a rejection with a reason and
    never becomes learning input.
    """
    cutoff = _utc(as_of)
    run_dir = Path(run_dir)
    if verify_run_receipt(run_dir):
        return (), ({"run": str(run_dir), "reason": "verify_failed"},)
    receipt = _load_json(run_dir / RECEIPT_FILE)
    if receipt is None:
        return (), ({"run": str(run_dir), "reason": "receipt_unreadable"},)
    run_id = str(receipt.get("run_id") or "")
    digest = str(receipt.get("events_digest") or "")
    expected = {
        (r.node_id, r.attempt_id): r
        for r in run_attempt_records(run_dir, receipt)
        if r.record_kind == "checker_result"
    }
    joined: dict[tuple[str | None, str | None], CheckedAttempt] = {}
    rejected: list[dict[str, str]] = []
    path = attempt_records_path(store_dir)
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict) or row.get("record_kind") != "checker_result":
            continue
        if row.get("run_id") != run_id:
            continue
        key = (row.get("node_id"), row.get("attempt_id"))
        source = f"{run_id}:{key[0]}:{key[1]}"
        want = expected.get((str(key[0]), str(key[1])))
        # Time comes from the verified events, never from the ledger row itself.
        checked_at = _parse_ts(want.ts) if want is not None else None
        reason = None
        if row.get("checker_digest") != digest:
            reason = "checker_digest_mismatch"
        elif want is None:
            reason = "scope_mismatch"
        elif row != want.to_dict():
            reason = "tampered"
        elif checked_at is None or checked_at > cutoff:
            reason = "future"
        elif key in joined:
            reason = "duplicate"
        if reason is not None or want is None or checked_at is None:
            rejected.append({"source": source, "reason": reason or "invalid"})
            continue
        assert want.quality_credit is not None
        joined[key] = CheckedAttempt(
            run_id=run_id,
            node_id=want.node_id or "",
            attempt_id=want.attempt_id or "",
            model=want.model,
            task_family=want.task_family,
            quality_credit=want.quality_credit,
            checked_at=checked_at,
            checker_digest=digest,
        )
    return tuple(joined.values()), tuple(rejected)


@dataclass(frozen=True)
class VerifiedOutcomeHistory:
    """Bounded, verified task-family history ready for :func:`score_task_history` (BOD-438)."""

    observations: tuple[OutcomeObservation, ...]
    omissions: tuple[str, ...]
    cutoff: datetime


def load_verified_outcome_observations(
    roots: Sequence[Path | str],
    *,
    task_family: str,
    as_of: datetime,
    max_roots: int = DEFAULT_MAX_ROOTS,
    max_events_bytes: int = DEFAULT_MAX_EVENTS_BYTES,
    max_events_lines: int = DEFAULT_MAX_EVENTS_LINES,
    max_receipt_bytes: int = DEFAULT_MAX_RECEIPT_BYTES,
) -> VerifiedOutcomeHistory:
    """Verified, checker-scored observations for one task family (BOD-438).

    Discovers ``implement``-role attempts per root with
    :func:`load_shadow_rows_from_run` (same bounds as the shadow reader: caps
    8 roots / 2MB events / 20000 lines / 512KB receipt), then joins only
    independently verified ``checker_result`` rows via
    :func:`load_checked_attempts`. Only a joined row supplies
    ``observed_at``/``quality_credit``; nothing here fabricates or falls back
    to a classification label. A skipped root, a rejected checker row or a
    checker row without a matching implement attempt is disclosed in
    ``omissions`` and never enters ``observations``. Rows from a different
    task family are silently irrelevant to this query, not omissions.
    """
    cutoff = _utc(as_of)
    canonical = _canonicalize_roots(roots)
    limited = canonical[: max(0, max_roots)]
    omissions: list[str] = [f"{path}:max_roots_exceeded" for path in canonical[len(limited) :]]

    observations: list[OutcomeObservation] = []
    for root in limited:
        rows, reason = load_shadow_rows_from_run(
            root,
            as_of=cutoff,
            max_events_bytes=max_events_bytes,
            max_events_lines=max_events_lines,
            max_receipt_bytes=max_receipt_bytes,
        )
        if reason is not None:
            omissions.append(f"{root}:{reason}")
            continue
        implement_by_key = {
            (row.run_id, row.node_id, str(row.attempt)): row
            for row in rows
            if row.role == "implement"
        }
        joined, rejected = load_checked_attempts(root, store_dir=verdict_home(), as_of=cutoff)
        for item in rejected:
            omissions.append(f"{root}:{item.get('source', '?')}:{item.get('reason', 'invalid')}")
        for checked in joined:
            key = (checked.run_id, checked.node_id, checked.attempt_id)
            row = implement_by_key.get(key)
            if row is None:
                omissions.append(
                    f"{root}:{checked.run_id}:{checked.node_id}:{checked.attempt_id}:not_implement_role"
                )
                continue
            if row.task_family != task_family:
                continue
            observations.append(
                OutcomeObservation(
                    route_id=row.route_id,
                    task_family=row.task_family,
                    success=checked.quality_credit == 1.0,
                    fingerprint_digest=row.fingerprint_digest,
                    observed_at=checked.checked_at,
                    quality_credit=checked.quality_credit,
                    observation_id=f"{checked.run_id}:{checked.node_id}:{checked.attempt_id}",
                )
            )
    return VerifiedOutcomeHistory(
        observations=tuple(observations), omissions=tuple(omissions), cutoff=cutoff
    )


def _metric_observations(
    rows: Sequence[ShadowAttemptRow], *, task_family: str
) -> tuple[OutcomeObservation, ...]:
    """Restricted metric → OutcomeObservation for counterfactual fuse only."""
    obs: list[OutcomeObservation] = []
    for row in rows:
        if row.role != "implement":
            continue
        if row.task_family != task_family:
            continue
        if row.classification == "accepted_attempt":
            obs.append(
                OutcomeObservation(
                    route_id=row.route_id,
                    task_family=row.task_family,
                    success=True,
                    fingerprint_digest=row.fingerprint_digest,
                )
            )
        elif row.classification == "rejected_attempt":
            obs.append(
                OutcomeObservation(
                    route_id=row.route_id,
                    task_family=row.task_family,
                    success=False,
                    fingerprint_digest=row.fingerprint_digest,
                )
            )
    return tuple(obs)


def shortlist_would_rank(
    shortlist: Sequence[ShortlistEntry] | Sequence[Mapping[str, Any]],
    observations: Sequence[OutcomeObservation],
) -> list[str]:
    """Rerank the existing live shortlist only; same route set."""
    scored: list[tuple[float, int, str]] = []
    for index, item in enumerate(shortlist):
        if isinstance(item, ShortlistEntry):
            route_id = item.route_id
            features = dict(item.score_features)
        elif isinstance(item, Mapping):
            route_id = str(item.get("route_id") or "")
            raw_features = item.get("score_features") or {}
            features = dict(raw_features) if isinstance(raw_features, Mapping) else {}
        else:
            continue
        matched = [o for o in observations if o.route_id == route_id]
        if matched:
            successes = sum(1 for o in matched if o.success)
            features["task_similarity"] = successes / len(matched)
            # Reduce uncertainty when history present (mirrors _score_features intent)
            features["uncertainty_penalty"] = min(
                float(features.get("uncertainty_penalty", 0.0)), 0.15
            )
        # else: leave features as live copy (missing history ≠ invent 0.0)
        score = _fuse_score(features)
        scored.append((-score, index, route_id))  # stable: score desc, then original index
    scored.sort()
    return [route_id for _, _, route_id in scored]


def _utc(as_of: datetime | None) -> datetime:
    as_of_dt = as_of or datetime.now(timezone.utc)
    if as_of_dt.tzinfo is None:
        as_of_dt = as_of_dt.replace(tzinfo=timezone.utc)
    return as_of_dt.astimezone(timezone.utc)


def _actual_rank(shortlist: Sequence[ShortlistEntry] | Sequence[Mapping[str, Any]]) -> list[str]:
    return [
        (item.route_id if isinstance(item, ShortlistEntry) else str(item.get("route_id") or ""))
        for item in shortlist
    ]


def _not_computed_shadow(
    *,
    as_of: datetime | None,
    task_family: str,
    fingerprint_digest: str,
    actual_rank: list[str],
    missingness: str,
) -> dict[str, Any]:
    """Shadow block for receipts where no counterfactual was computed.

    ``would_rank`` is ``None`` (JSON ``null``): a sentinel meaning "not
    computed", never an identity copy of ``actual_rank``.
    """
    return {
        "as_of": _utc(as_of).isoformat().replace("+00:00", "Z"),
        "task_family": task_family,
        "fingerprint_digest": fingerprint_digest,
        "metric": METRIC_NAME,
        "would_rank_scope": WOULD_RANK_SCOPE,
        "roots": [],
        "source_run_ids": [],
        "source_events_digests": [],
        "metric_missingness": missingness,
        "n_attempts_matched": 0,
        "n_nodes_matched": 0,
        "metric_n_accept": 0,
        "metric_n_reject": 0,
        "metric_denominator": 0,
        "audit": {},
        "per_route": {},
        "actual_rank": actual_rank,
        "would_rank": None,
        "rank_delta": [],
        "shadow_skip_reasons": [],
    }


def build_outcome_shadow(
    *,
    roots: Sequence[Path | str],
    task_family: str,
    fingerprint_digest: str,
    shortlist: Sequence[ShortlistEntry] | Sequence[Mapping[str, Any]],
    as_of: datetime | None = None,
    max_roots: int = DEFAULT_MAX_ROOTS,
    max_events_bytes: int = DEFAULT_MAX_EVENTS_BYTES,
    max_events_lines: int = DEFAULT_MAX_EVENTS_LINES,
    max_receipt_bytes: int = DEFAULT_MAX_RECEIPT_BYTES,
) -> dict[str, Any]:
    """Build the serialized outcome_shadow block.

    Raises :class:`OutcomeShadowNotConfiguredError` when ``roots`` is empty:
    without roots there is no counterfactual, and copying ``actual_rank`` into
    ``would_rank`` would read as 100% agreement. ``attach_outcome_shadow``
    handles that case with an explicit disabled block.
    """
    as_of_dt = _utc(as_of)

    actual_rank = _actual_rank(shortlist)

    canonical = _canonicalize_roots(roots)
    if not canonical:
        raise OutcomeShadowNotConfiguredError(
            f"no outcome shadow roots configured ({ENV_ROOTS} unset or empty)"
        )

    limited = canonical[: max(0, max_roots)]
    skipped_over_cap = [
        {"root": str(path), "reason": "max_roots_exceeded"} for path in canonical[len(limited) :]
    ]

    all_rows: list[ShadowAttemptRow] = []
    skip_reasons: list[dict[str, str]] = list(skipped_over_cap)
    source_run_ids: list[str] = []
    source_digests: list[str] = []
    opened: list[str] = []
    checker_joined = 0
    checker_rejected: list[dict[str, str]] = []

    for root in limited:
        opened.append(str(root))
        rows, reason = load_shadow_rows_from_run(
            root,
            as_of=as_of_dt,
            max_events_bytes=max_events_bytes,
            max_events_lines=max_events_lines,
            max_receipt_bytes=max_receipt_bytes,
        )
        if reason is not None:
            skip_reasons.append({"root": str(root), "reason": reason})
            continue
        if rows:
            source_run_ids.append(rows[0].run_id)
            source_digests.append(rows[0].events_digest)
            all_rows.extend(rows)
            joined, refused = load_checked_attempts(root, store_dir=verdict_home(), as_of=as_of_dt)
            checker_joined += len(joined)
            checker_rejected.extend(refused)

    # Dedupe across roots
    deduped: dict[tuple[str, str, int, str], ShadowAttemptRow] = {}
    for row in all_rows:
        deduped[(row.run_id, row.node_id, row.attempt, row.route_id)] = row
    rows_tuple = tuple(deduped.values())

    family_rows = [r for r in rows_tuple if r.task_family == task_family]
    implement_rows = [r for r in family_rows if r.role == "implement"]

    metric_obs = _metric_observations(rows_tuple, task_family=task_family)
    metric_accept = sum(1 for o in metric_obs if o.success)
    metric_reject = sum(1 for o in metric_obs if not o.success)

    audit = {
        "accepted_attempt": 0,
        "rejected_attempt": 0,
        "first_attempt_failed": 0,
        "timeout": 0,
        "cancelled": 0,
        "execution_terminal_unproven": 0,
        "mechanical_empty_route": 0,
        "incomplete": 0,
        "unsupported_role": 0,
    }
    for row in family_rows:
        if row.role != "implement":
            audit["unsupported_role"] += 1
            continue
        audit[row.classification] = audit.get(row.classification, 0) + 1

    per_route: dict[str, dict[str, Any]] = {}
    for row in implement_rows:
        bucket = per_route.setdefault(
            row.route_id,
            {
                "n": 0,
                "n_accepted_attempt": 0,
                "n_rejected_attempt": 0,
                "n_first_attempt_failed": 0,
                "n_timeout": 0,
                "n_cancelled": 0,
                "metric_n_accept": 0,
                "metric_n_reject": 0,
                "source_ids": [],
            },
        )
        bucket["n"] += 1
        key = f"n_{row.classification}"
        if key in bucket:
            bucket[key] += 1
        elif row.classification == "accepted_attempt":
            bucket["n_accepted_attempt"] += 1
        elif row.classification == "rejected_attempt":
            bucket["n_rejected_attempt"] += 1
        elif row.classification == "first_attempt_failed":
            bucket["n_first_attempt_failed"] += 1
        elif row.classification == "timeout":
            bucket["n_timeout"] += 1
        elif row.classification == "cancelled":
            bucket["n_cancelled"] += 1
        if row.classification == "accepted_attempt":
            bucket["metric_n_accept"] += 1
        elif row.classification == "rejected_attempt":
            bucket["metric_n_reject"] += 1
        bucket["source_ids"].append(row.source_id)

    would = shortlist_would_rank(shortlist, metric_obs)
    rank_delta = []
    actual_index = {route: i for i, route in enumerate(actual_rank)}
    would_index = {route: i for i, route in enumerate(would)}
    for route in actual_rank:
        rank_delta.append(
            {
                "route_id": route,
                "actual_index": actual_index[route],
                "would_index": would_index.get(route, actual_index[route]),
            }
        )

    missingness = None
    if not source_run_ids:
        missingness = "no_verified_runs" if skip_reasons else "no_family_matches"
    elif not implement_rows and family_rows:
        missingness = "unsupported_role"
    elif not metric_obs and implement_rows:
        missingness = "no_metric_rows_before_as_of"

    nodes_matched = {(r.run_id, r.node_id) for r in implement_rows}

    return {
        "as_of": as_of_dt.isoformat().replace("+00:00", "Z"),
        "task_family": task_family,
        "fingerprint_digest": fingerprint_digest,
        "metric": METRIC_NAME,
        "would_rank_scope": WOULD_RANK_SCOPE,
        "roots": opened,
        "source_run_ids": source_run_ids,
        "source_events_digests": source_digests,
        "metric_missingness": missingness,
        "n_attempts_matched": len(implement_rows),
        "n_nodes_matched": len(nodes_matched),
        "metric_n_accept": metric_accept,
        "metric_n_reject": metric_reject,
        "metric_denominator": metric_accept + metric_reject,
        "audit": audit,
        "per_route": per_route,
        "actual_rank": actual_rank,
        "would_rank": would,
        "rank_delta": rank_delta,
        "shadow_skip_reasons": skip_reasons,
        # BOD-437: append-only checker rows joined against these verified runs.
        "checker_join": {"joined": checker_joined, "rejected": checker_rejected},
    }


def attach_outcome_shadow(
    candidate_pool: dict[str, Any],
    *,
    roots: Sequence[Path | str],
    task_family: str,
    fingerprint_digest: str,
    as_of: datetime | None = None,
    sink: Path | None = None,
) -> dict[str, Any]:
    """Return a new candidate_pool dict with outcome_shadow attached; never mutates live scores.

    With ``sink`` set, the attached block is also appended to that jsonl file
    (one line per receipt). Sink failures are logged and never raised.
    """
    shortlist = candidate_pool.get("shortlist") or []
    if not isinstance(shortlist, list):
        shortlist = []
    mapping_rank = [
        str(item.get("route_id") or "") for item in shortlist if isinstance(item, Mapping)
    ]
    shadow: dict[str, Any]
    if not _canonicalize_roots(roots):
        shadow = _not_computed_shadow(
            as_of=as_of,
            task_family=task_family,
            fingerprint_digest=fingerprint_digest,
            actual_rank=mapping_rank,
            missingness="no_roots_configured",
        )
    else:
        try:
            shadow = build_outcome_shadow(
                roots=roots,
                task_family=task_family,
                fingerprint_digest=fingerprint_digest,
                shortlist=shortlist,
                as_of=as_of,
            )
        except Exception as exc:
            shadow = _not_computed_shadow(
                as_of=as_of,
                task_family=task_family,
                fingerprint_digest=fingerprint_digest,
                actual_rank=mapping_rank,
                missingness="shadow_error",
            )
            shadow["shadow_error"] = type(exc).__name__
            shadow["shadow_skip_reasons"] = [{"root": "*", "reason": "shadow_error"}]
    if sink is not None:
        append_outcome_shadow_record(sink, shadow)
    out = dict(candidate_pool)
    out["outcome_shadow"] = shadow
    return out


__all__ = [
    "ENV_ROOTS",
    "METRIC_NAME",
    "SINK_FILENAME",
    "SINK_SCHEMA",
    "WOULD_RANK_SCOPE",
    "CheckedAttempt",
    "OutcomeShadowNotConfiguredError",
    "ShadowAttemptRow",
    "VerifiedOutcomeHistory",
    "append_outcome_shadow_record",
    "attach_outcome_shadow",
    "build_outcome_shadow",
    "load_checked_attempts",
    "load_shadow_rows_from_run",
    "load_verified_outcome_observations",
    "resolve_outcome_shadow_roots",
    "resolve_outcome_shadow_sink",
    "shortlist_would_rank",
]
