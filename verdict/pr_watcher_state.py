"""PR watcher durable state management.

Handles atomic JSON writes, path-safe keys, and fail-closed behaviour
for malformed or unreadable state files.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
RECORD_KINDS = ("repair", "block", "regression")
MAX_LINEAR_RETRY = 5
STALLED_THRESHOLD_MINUTES = 30

# --- Path-safe key validation -----------------------------------------------------


class UnsafeKeyError(ValueError):
    """Raised when a durable state key could escape or corrupt the state store."""


def _safe_key(key: str) -> str:
    """Validate a state key: no traversal, no absolute path, no control chars.

    ``/`` is allowed inside a key (``org/repo``, ``osv-security / osv-scan``), as
    are spaces and parentheses, which real check names contain.  ``..`` is
    rejected anywhere in the key, not only as a whole path component.
    """
    if not isinstance(key, str) or not key:
        raise UnsafeKeyError(f"invalid state key: {key!r}")
    if key.startswith("/") or os.path.isabs(key):
        raise UnsafeKeyError(f"absolute path in state key: {key!r}")
    if ".." in key or "\\" in key or any(not part for part in key.split("/")):
        raise UnsafeKeyError(f"path traversal in state key: {key!r}")
    if any(ord(ch) < 0x20 for ch in key):
        raise UnsafeKeyError(f"control character in state key: {key!r}")
    return key


# --- Atomic JSON helpers ----------------------------------------------------------


def _atomic_write_json(path: Path, value: dict[str, Any]) -> Path:
    """Write JSON atomically via temp-file + rename; the old file survives a failure."""
    tmp = path.with_suffix(".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n")
        with tmp.open("rb") as handle:
            os.fsync(handle.fileno())
        os.replace(str(tmp), str(path))
        dir_fd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError as exc:
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)
        raise StateWriteError(f"cannot write state {path}: {exc}") from exc
    return path


def _read_json(path: Path) -> dict[str, Any]:
    """Read a JSON object; anything unreadable or unparseable is malformed state."""
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise MalformedStateError(f"malformed state {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise MalformedStateError(f"malformed state {path}: not a JSON object")
    return data


class MalformedStateError(Exception):
    """Raised when durable state is corrupt or unreadable."""


class StateWriteError(Exception):
    """Raised when durable state cannot be written."""


# --- Timestamps -------------------------------------------------------------------


def format_timestamp(moment: datetime) -> str:
    """Render a timezone-aware instant as the UTC form GitHub search accepts."""
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_timestamp(value: object) -> datetime:
    """Parse a persisted timestamp; anything else is malformed state."""
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise MalformedStateError(f"malformed timestamp in state: {value!r}") from exc
    if not isinstance(value, str) or parsed.tzinfo is None or parsed.utcoffset() is None:
        raise MalformedStateError(f"malformed timestamp in state: {value!r}")
    return parsed


# --- State shape ------------------------------------------------------------------

_STATE_CONTAINERS: dict[str, type] = {
    "merged_cursors": dict,
    "pr_states": dict,
    "rerun_attempts": dict,
    "records": dict,
    "pending_events": list,
    "digest_events": dict,
    "digest_unwritten": list,
}


def _validate_state(data: dict[str, Any], path: Path) -> dict[str, Any]:
    """Return ``data`` only if it has the full expected shape; otherwise fail closed."""

    def malformed(why: str) -> MalformedStateError:
        return MalformedStateError(f"malformed state {path}: {why}")

    if data.get("schema_version") != SCHEMA_VERSION:
        raise malformed(f"unsupported schema_version {data.get('schema_version')!r}")
    for field, kind in _STATE_CONTAINERS.items():
        if not isinstance(data.get(field), kind):
            raise malformed(f"field {field!r} is missing or not a {kind.__name__}")
    for record_kind in RECORD_KINDS:
        if not isinstance(data["records"].get(record_kind), list):
            raise malformed(f"records[{record_kind!r}] is missing or not a list")
    for key, count in data["rerun_attempts"].items():
        # A negative or non-integer count would hand back a spent rerun budget.
        if type(count) is not int or count < 0:
            raise malformed(f"rerun attempt count for {key!r} is not a non-negative integer")
    for key in data["digest_unwritten"]:
        if key not in data["digest_events"]:
            raise malformed(f"unwritten digest event {key!r} is not a recorded event")
    if "last_run_at" not in data:
        raise malformed("field 'last_run_at' is missing")
    stamps = {"last_run_at": data["last_run_at"]}
    stamps.update({f"merged_cursors[{repo!r}]": at for repo, at in data["merged_cursors"].items()})
    for name, stamp in stamps.items():
        if stamp is None and name == "last_run_at":
            continue
        try:
            parse_timestamp(stamp)
        except MalformedStateError as exc:
            raise malformed(f"{name}: {exc}") from exc
    return data


# --- Lock helpers -----------------------------------------------------------------


class _NonBlockingLock:
    """Non-blocking flock context manager."""

    def __init__(self, lock_path: Path) -> None:
        self._path = lock_path
        self._fd: int | None = None

    def __enter__(self) -> _NonBlockingLock:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._fd = os.open(str(self._path), os.O_CREAT | os.O_RDWR)
        try:
            fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError) as exc:
            os.close(self._fd)
            raise LockAcquisitionError("another watcher invocation already holds the lock") from exc
        return self

    def __exit__(self, *exc: object) -> None:
        if self._fd is not None:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
            os.close(self._fd)
            self._fd = None


class LockAcquisitionError(RuntimeError):
    """Raised when the non-blocking lock cannot be acquired."""


# --- Key helpers ------------------------------------------------------------------


def rerun_key(
    repo: str, pr_number: int, head: str, check_name: str, workflow_job_id: object
) -> str:
    """Stable rerun identity: repository, PR, exact head, check name, workflow/job id."""
    return _safe_key(f"{repo}:pr{pr_number}:{head}:{check_name}:{workflow_job_id}")


def _idempotency_key(kind: str, repo: str, pr_number: int, head: str) -> str:
    """Deterministic idempotency key for deduplication."""
    return _safe_key(f"{kind}:{repo}:pr{pr_number}:{head}")


# --- Repair / block / regression record builders ----------------------------------


class RepairRecord:
    """Durable controller repair record."""

    def __init__(
        self,
        repo: str,
        pr_number: int,
        head: str,
        failing_checks: list[str],
        failing_check_details: list[dict[str, Any]] | None = None,
        classification: str = "",
        reason: str = "",
    ) -> None:
        self.repo = _safe_key(repo)
        self.pr_number = pr_number
        self.head = head
        self.failing_checks = failing_checks
        # Name, conclusion, job id and details URL per failing check: what a
        # repair worker needs to fetch the log itself. No log text is stored.
        self.failing_check_details = failing_check_details or []
        self.classification = classification
        self.reason = reason
        # One record per classification on a head: a later, different failure
        # on the same head is not a duplicate of the first one.
        base = _idempotency_key("repair", repo, pr_number, head)
        self.idempotency_key = _safe_key(f"{base}:{classification}") if classification else base

    def to_dict(self, created_at: str) -> dict[str, Any]:
        return {
            "type": "repair",
            "repo": self.repo,
            "pr_number": self.pr_number,
            "head": self.head,
            "failing_checks": self.failing_checks,
            "failing_check_details": self.failing_check_details,
            "classification": self.classification,
            "reason": self.reason,
            "idempotency_key": self.idempotency_key,
            "created_at": created_at,
        }


class BlockRecord:
    """Durable block record."""

    def __init__(self, repo: str, pr_number: int, head: str, reason: str) -> None:
        self.repo = _safe_key(repo)
        self.pr_number = pr_number
        self.head = head
        self.reason = reason
        self.idempotency_key = _idempotency_key("block", repo, pr_number, head)

    def to_dict(self, created_at: str) -> dict[str, Any]:
        return {
            "type": "block",
            "repo": self.repo,
            "pr_number": self.pr_number,
            "head": self.head,
            "reason": self.reason,
            "idempotency_key": self.idempotency_key,
            "created_at": created_at,
        }


class RegressionRecord:
    """Durable merged-PR regression record."""

    def __init__(
        self, repo: str, pr_number: int, head: str, review_sources: list[str], reason: str
    ) -> None:
        self.repo = _safe_key(repo)
        self.pr_number = pr_number
        self.head = head
        self.review_sources = review_sources
        self.reason = reason
        self.idempotency_key = _idempotency_key("regression", repo, pr_number, head)

    def to_dict(self, created_at: str) -> dict[str, Any]:
        return {
            "type": "regression",
            "repo": self.repo,
            "pr_number": self.pr_number,
            "head": self.head,
            "review_sources": self.review_sources,
            "reason": self.reason,
            "idempotency_key": self.idempotency_key,
            "created_at": created_at,
        }


# --- Watcher state ---------------------------------------------------------------


class WatcherState:
    """Durable watcher state backed by a single JSON file."""

    def __init__(self, state_dir: Path) -> None:
        self.state_dir = state_dir
        self.state_file = state_dir / "pr_watcher_state.json"
        self.lock_file = state_dir / "pr_watcher.lock"
        self.digest_file = state_dir / "pr_watcher_digest.jsonl"
        self._lock: _NonBlockingLock | None = None

    # -- lock --------------------------------------------------------------------

    def acquire_lock(self) -> None:
        """Acquire a non-blocking single-writer lock."""
        self._lock = _NonBlockingLock(self.lock_file)
        self._lock.__enter__()

    def release_lock(self) -> None:
        """Release the single-writer lock."""
        if self._lock is not None:
            self._lock.__exit__(None, None, None)
            self._lock = None

    # -- load / save -------------------------------------------------------------

    def load(self) -> dict[str, Any]:
        """Load state; fail closed on corruption."""
        if not self.state_file.exists():
            return self._empty()
        return _validate_state(_read_json(self.state_file), self.state_file)

    def save(self, data: dict[str, Any]) -> None:
        """Atomically persist state."""
        _atomic_write_json(self.state_file, data)

    @staticmethod
    def _empty() -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "merged_cursors": {},
            "pr_states": {},
            "rerun_attempts": {},
            "records": {"repair": [], "block": [], "regression": []},
            "pending_events": [],
            "digest_events": {},
            "digest_unwritten": [],
            "last_run_at": None,
        }

    # -- helpers -----------------------------------------------------------------

    def get_rerun_attempt(self, data: dict[str, Any], key: str) -> int:
        return int(data.get("rerun_attempts", {}).get(key, 0))

    def set_rerun_attempt(self, data: dict[str, Any], key: str, value: int) -> dict[str, Any]:
        _safe_key(key)
        attempts = dict(data.get("rerun_attempts", {}))
        attempts[key] = value
        return {**data, "rerun_attempts": attempts}

    def has_record(self, data: dict[str, Any], idempotency_key: str) -> bool:
        """Check if a record with this key already exists."""
        for kind in RECORD_KINDS:
            for rec in data.get("records", {}).get(kind, []):
                if rec.get("idempotency_key") == idempotency_key:
                    return True
        return False

    def add_record(self, data: dict[str, Any], record: dict[str, Any]) -> dict[str, Any]:
        """Add a record (idempotent by key)."""
        _safe_key(record["idempotency_key"])
        kind = record["type"]
        records = dict(data.get("records", {}))
        existing = records.get(kind, [])
        if any(r.get("idempotency_key") == record["idempotency_key"] for r in existing):
            return data
        # A new list, never an append: the caller's ``data`` must stay as it
        # was so that a repository's work can be discarded as a whole.
        records[kind] = [*existing, record]
        return {**data, "records": records}

    def has_digest_event(self, data: dict[str, Any], idempotency_key: str) -> bool:
        return idempotency_key in data.get("digest_events", {})

    def add_digest_event(self, data: dict[str, Any], event: dict[str, Any]) -> dict[str, Any]:
        """Record a digest event under its key and mark it as owed to the digest file."""
        key = _safe_key(event["idempotency_key"])
        events = dict(data.get("digest_events", {}))
        if key in events:
            return data
        events[key] = event
        unwritten = [*data.get("digest_unwritten", []), key]
        return {**data, "digest_events": events, "digest_unwritten": unwritten}

    def unwritten_digest_events(self, data: dict[str, Any]) -> list[dict[str, Any]]:
        """Recorded events whose digest line is not yet known to be on disk."""
        events = data.get("digest_events", {})
        return [events[key] for key in data.get("digest_unwritten", [])]

    def append_digest(self, events: list[dict[str, Any]]) -> None:
        """Append one JSON line per event to the digest file, skipping lines already there.

        A run can die after appending and before the state records it, so the
        same event may be offered again; its key is looked up in the file first.
        """
        if not events:
            return
        try:
            existing = self.digest_file.read_text() if self.digest_file.exists() else ""
            written = set()
            for line in existing.splitlines():
                # A torn last line from an interrupted write is not a written event.
                with contextlib.suppress(ValueError, AttributeError):
                    written.add(json.loads(line).get("idempotency_key"))
            lines = [
                json.dumps(event, sort_keys=True)
                for event in events
                if event["idempotency_key"] not in written
            ]
            if not lines:
                return
            self.state_dir.mkdir(parents=True, exist_ok=True)
            with self.digest_file.open("a") as handle:
                if existing and not existing.endswith("\n"):
                    handle.write("\n")
                handle.write("\n".join(lines) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as exc:
            raise StateWriteError(f"cannot write digest {self.digest_file}: {exc}") from exc

    def get_pending_events(self, data: dict[str, Any]) -> list[dict[str, Any]]:
        raw = data.get("pending_events", [])
        return list(raw) if isinstance(raw, list) else []

    def add_pending_event(self, data: dict[str, Any], event: dict[str, Any]) -> dict[str, Any]:
        """Add a pending notification event (idempotent)."""
        _safe_key(event["idempotency_key"])
        events = list(data.get("pending_events", []))
        if not any(e.get("idempotency_key") == event["idempotency_key"] for e in events):
            events.append(event)
        return {**data, "pending_events": events}

    def get_pr_state(
        self, data: dict[str, Any], repo: str, pr_number: int, head: str
    ) -> dict[str, Any] | None:
        key = _idempotency_key("state", repo, pr_number, head)
        raw = data.get("pr_states", {}).get(key)
        return dict(raw) if isinstance(raw, dict) else None

    def set_pr_state(
        self,
        data: dict[str, Any],
        repo: str,
        pr_number: int,
        head: str,
        classification: str,
        **extra: Any,
    ) -> dict[str, Any]:
        key = _idempotency_key("state", repo, pr_number, head)
        pr_states = dict(data.get("pr_states", {}))
        pr_states[key] = {
            "repo": repo,
            "pr_number": pr_number,
            "head": head,
            "classification": classification,
            **extra,
        }
        return {**data, "pr_states": pr_states}

    def set_merged_cursor(
        self, data: dict[str, Any], repo: str, cursor: datetime
    ) -> dict[str, Any]:
        """Advance one repository's merged-PR cursor."""
        cursors = {**data.get("merged_cursors", {}), _safe_key(repo): format_timestamp(cursor)}
        return {**data, "merged_cursors": cursors}

    def get_merged_cursor(self, data: dict[str, Any], repo: str) -> datetime | None:
        raw = data.get("merged_cursors", {}).get(repo)
        return None if raw is None else parse_timestamp(raw)

    def set_last_run(self, data: dict[str, Any], moment: datetime) -> dict[str, Any]:
        return {**data, "last_run_at": format_timestamp(moment)}
