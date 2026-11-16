"""PR watcher durable state management.

Handles atomic JSON writes, path-safe keys, and fail-closed behaviour
for malformed or unreadable state files.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import time
from pathlib import Path
from typing import Any

MAX_LOG_EXCERPT = 4096
MAX_REDACTED = 4096
MAX_LINEAR_RETRY = 5
STALLED_THRESHOLD_MINUTES = 30

# --- Path-safe key validation -----------------------------------------------------


def _safe_key(key: str) -> str:
    """Validate a state key: no path traversal, no absolute paths, no control chars.

    Allows / within keys (e.g. ``org/repo``) but rejects ``..`` components
    and absolute paths on POSIX.
    """
    if not isinstance(key, str) or not key:
        raise ValueError(f"invalid state key: {key!r}")
    # Reject path traversal components
    parts = key.split("/")
    for part in parts:
        if part == ".." or part == "":
            raise ValueError(f"path-traversal in state key: {key!r}")
    # Reject absolute paths
    if os.path.isabs(key):
        raise ValueError(f"absolute path in state key: {key!r}")
    # Reject null bytes and control chars
    for ch in key:
        if ord(ch) < 0x21:
            raise ValueError(f"control character in state key: {key!r}")
    return key


# --- Atomic JSON helpers ----------------------------------------------------------


def _atomic_write_json(path: Path, value: dict[str, Any]) -> Path:
    """Write JSON atomically via temp-file + rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n")
    os.replace(str(tmp), str(path))
    return path


def _read_json(path: Path) -> dict[str, Any]:
    """Read a JSON file; return {} for missing files, fail closed for corrupt ones."""
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        raise MalformedStateError(f"malformed state {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise MalformedStateError(f"state {path} is not a JSON object")
    return data


class MalformedStateError(Exception):
    """Raised when durable state is corrupt or unreadable."""


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


# --- Idempotency key helpers ------------------------------------------------------


def _idempotency_key(kind: str, repo: str, pr_number: int, head: str) -> str:
    """Deterministic idempotency key for deduplication."""
    _safe_key(repo)
    return f"{kind}:{repo}:pr{pr_number}:{head}"


# --- Secret redaction -------------------------------------------------------------

_SECRET_RE = re.compile(
    r"(ghp_|gho_|github_pat_|ghs_|token=|x-oauth-basic-|Bearer\s)"
    r"[A-Za-z0-9_\-\.]{10,}"
)


def redact_secrets(text: str) -> str:
    """Replace known secret patterns with [REDACTED]."""
    return _SECRET_RE.sub("[REDACTED]", text)


def truncate_log(text: str, limit: int = MAX_LOG_EXCERPT) -> str:
    """Truncate and redact a log excerpt."""
    return redact_secrets(text[:limit])


# --- Repair / block / regression record builders ----------------------------------


class RepairRecord:
    """Durable controller repair record."""

    def __init__(
        self,
        repo: str,
        pr_number: int,
        head: str,
        failing_checks: list[str],
        log_excerpt: str = "",
        classification: str = "",
        reason: str = "",
    ) -> None:
        self.repo = _safe_key(repo)
        self.pr_number = pr_number
        self.head = head
        self.failing_checks = failing_checks
        self.log_excerpt = truncate_log(log_excerpt)
        self.classification = classification
        self.reason = reason
        self.idempotency_key = _idempotency_key("repair", repo, pr_number, head)

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": "repair",
            "repo": self.repo,
            "pr_number": self.pr_number,
            "head": self.head,
            "failing_checks": self.failing_checks,
            "log_excerpt": self.log_excerpt,
            "classification": self.classification,
            "reason": self.reason,
            "idempotency_key": self.idempotency_key,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }


class BlockRecord:
    """Durable block record."""

    def __init__(self, repo: str, pr_number: int, head: str, reason: str) -> None:
        self.repo = _safe_key(repo)
        self.pr_number = pr_number
        self.head = head
        self.reason = reason
        self.idempotency_key = _idempotency_key("block", repo, pr_number, head)

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": "block",
            "repo": self.repo,
            "pr_number": self.pr_number,
            "head": self.head,
            "reason": self.reason,
            "idempotency_key": self.idempotency_key,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
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

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": "regression",
            "repo": self.repo,
            "pr_number": self.pr_number,
            "head": self.head,
            "review_sources": self.review_sources,
            "reason": self.reason,
            "idempotency_key": self.idempotency_key,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }


# --- Watcher state ---------------------------------------------------------------


class WatcherState:
    """Durable watcher state backed by a single JSON file."""

    def __init__(self, state_dir: Path) -> None:
        self.state_dir = state_dir
        self.state_file = state_dir / "pr_watcher_state.json"
        self.lock_file = state_dir / "pr_watcher.lock"
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
        data = _read_json(self.state_file)
        if not data:
            return self._empty()
        return data

    def save(self, data: dict[str, Any]) -> None:
        """Atomically persist state."""
        _atomic_write_json(self.state_file, data)

    @staticmethod
    def _empty() -> dict[str, Any]:
        return {
            "schema_version": 1,
            "merged_cursor": None,
            "pr_states": {},
            "rerun_attempts": {},
            "records": {"repair": [], "block": [], "regression": []},
            "pending_events": [],
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
        for kind in ("repair", "block", "regression"):
            for rec in data.get("records", {}).get(kind, []):
                if rec.get("idempotency_key") == idempotency_key:
                    return True
        return False

    def add_record(self, data: dict[str, Any], record: dict[str, Any]) -> dict[str, Any]:
        """Add a record (idempotent by key)."""
        _safe_key(record["idempotency_key"])
        kind = record["type"]
        records = dict(data.get("records", {}))
        records.setdefault(kind, [])
        # Skip if duplicate
        if any(r.get("idempotency_key") == record["idempotency_key"] for r in records[kind]):
            return data
        records[kind].append(record)
        return {**data, "records": records}

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

    def clear_pending_event(self, data: dict[str, Any], key: str) -> dict[str, Any]:
        events = [e for e in data.get("pending_events", []) if e.get("idempotency_key") != key]
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

    def set_merged_cursor(self, data: dict[str, Any], cursor: str) -> dict[str, Any]:
        return {**data, "merged_cursor": cursor}

    def get_merged_cursor(self, data: dict[str, Any]) -> str | None:
        return data.get("merged_cursor")
