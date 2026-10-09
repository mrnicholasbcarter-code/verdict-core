"""Controller-verified worker-session outcomes, independent of scripted probes.

Only verified evidence affects summaries. False success claims count as two
failures. Repeated imports share a route/timestamp/outcome identity.
"""

from __future__ import annotations

import fcntl
import json
import os
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal, TextIO

from verdict.orchestration.health_cache import format_datetime, parse_datetime

FAILURE_CLASSES = frozenset(
    {
        "narrates_tool_calls",
        "gave_up",
        "false_success_claim",
        "transport_error",
        "timeout",
        "wrong_result",
    }
)


@dataclass(frozen=True)
class SessionOutcome:
    """One observed worker session, with controller-verification provenance."""

    route_id: str
    outcome: Literal["pass", "fail"]
    kind: str
    failure_class: str | None
    verified_by_controller: bool
    at: datetime
    source: str

    def __post_init__(self) -> None:
        if not self.route_id.strip() or self.outcome not in ("pass", "fail"):
            raise ValueError("session route_id and pass/fail outcome are required")
        if self.failure_class is not None and self.failure_class not in FAILURE_CLASSES:
            raise ValueError("unknown session failure_class")
        if self.outcome == "pass" and self.failure_class is not None:
            raise ValueError("a session pass cannot have a failure_class")
        if not isinstance(self.verified_by_controller, bool):
            raise ValueError("verified_by_controller must be a bool")
        format_datetime(self.at)  # Reuse the health cache's aware timestamp contract.


@dataclass(frozen=True)
class SessionStats:
    """Windowed counts; fails includes the extra penalty for false claims."""

    passes: int
    fails: int
    false_claims: int
    last_pass_at: datetime | None
    last_fail_at: datetime | None
    score: float


def _identity(item: SessionOutcome) -> tuple[str, datetime, str]:
    return item.route_id, item.at, item.outcome


def _read(stream: TextIO) -> tuple[SessionOutcome, ...]:
    items: dict[tuple[str, datetime, str], SessionOutcome] = {}
    for line in stream:
        if not line.strip():
            continue
        raw: dict[str, Any] = json.loads(line)
        item = SessionOutcome(
            route_id=raw["route_id"],
            outcome=raw["outcome"],
            kind=raw["kind"],
            failure_class=raw["failure_class"],
            verified_by_controller=raw["verified_by_controller"],
            at=parse_datetime(raw["at"], "at"),
            source=raw["source"],
        )
        items.setdefault(_identity(item), item)
    return tuple(items.values())


class SessionLedger:
    """Append-only JSONL store; readers and deduplicating writers share a lock."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(
            path
            if path is not None
            else os.environ.get(
                "VERDICT_SESSION_EVIDENCE", str(Path.home() / ".verdict" / "session-evidence.jsonl")
            )
        ).expanduser()

    def append(self, outcome: SessionOutcome) -> None:
        """Append one complete line under an exclusive fcntl lock, once."""
        payload = asdict(outcome)
        payload["at"] = format_datetime(outcome.at)
        line = json.dumps(payload, separators=(",", ":")) + "\n"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a+", encoding="utf-8") as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            try:
                stream.seek(0)
                if any(_identity(item) == _identity(outcome) for item in _read(stream)):
                    return
                stream.write(line)
                stream.flush()
                os.fsync(stream.fileno())
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    def load(self) -> tuple[SessionOutcome, ...]:
        """Return a locked snapshot; an absent file is an empty ledger."""
        try:
            stream = self.path.open(encoding="utf-8")
        except FileNotFoundError:
            return ()
        with stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_SH)
            try:
                return _read(stream)
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    def summarize(self, route: str, now: datetime, window_days: int = 14) -> SessionStats:
        """Summarize verified outcomes inside the inclusive, non-future window."""
        format_datetime(now)
        if window_days < 0:
            raise ValueError("window_days must be non-negative")
        return _summarize(
            item
            for item in self.load()
            if item.route_id == route
            and item.verified_by_controller
            and now - timedelta(days=window_days) <= item.at <= now
        )


def _summarize(items: Iterable[SessionOutcome]) -> SessionStats:
    passes = fails = false_claims = 0
    last_pass: datetime | None = None
    last_fail: datetime | None = None
    for item in items:
        if item.outcome == "pass":
            passes += 1
            last_pass = max(last_pass, item.at) if last_pass is not None else item.at
        else:
            false_claim = item.failure_class == "false_success_claim"
            false_claims += int(false_claim)
            fails += 2 if false_claim else 1
            last_fail = max(last_fail, item.at) if last_fail is not None else item.at
    return SessionStats(
        passes, fails, false_claims, last_pass, last_fail, (passes + 1) / (passes + fails + 2)
    )


def _failure_class(error: str, message: str) -> str:
    text = f"{error} {message}".lower()
    for failure in ("false_success_claim", "narrates_tool_calls", "gave_up", "timeout"):
        if failure in text or failure.replace("_", " ") in text:
            return failure
    if "timed out" in text:
        return "timeout"
    if any(token in text for token in ("transport", "upstream", "http", "429", "504")):
        return "transport_error"
    return "wrong_result"


def import_worker_outcomes(jsonl_path: str | Path) -> list[SessionOutcome]:
    """Read the controller's canary/real-task ledger, ignoring other kinds.

    This is a pure importer: it writes nothing. Call ``SessionLedger.append``
    with its results to persist them idempotently. Input must be the trusted
    controller ledger, not a worker's self-reported result.
    """
    source = Path(jsonl_path)
    items: dict[tuple[str, datetime, str], SessionOutcome] = {}
    with source.open(encoding="utf-8") as stream:
        for line in stream:
            if not line.strip():
                continue
            raw: dict[str, Any] = json.loads(line)
            kind = raw.get("kind")
            message = str(raw.get("outcome") or "")
            outcome: Literal["pass", "fail"]
            failure: str | None = None
            if kind == "session_canary_v1" and message.startswith("SESSION_CANARY_PASS"):
                outcome = "pass"
            elif kind == "session_canary_v1" and message.startswith("SESSION_CANARY_FAIL"):
                outcome = "fail"
                failure = _failure_class(str(raw.get("error") or ""), message)
            elif kind == "free_real_task" and message.startswith(
                ("REAL_TASK_PASS", "REAL_TASK_FIX_PASS")
            ):
                outcome = "pass"
            elif kind == "free_real_task" and message.startswith("FALSE_CLAIM"):
                outcome, failure = "fail", "false_success_claim"
            else:
                continue
            item = SessionOutcome(
                route_id=raw["route"],
                outcome=outcome,
                kind=str(kind),
                failure_class=failure,
                verified_by_controller=True,
                at=parse_datetime(raw["ts"], "ts"),
                source=str(source),
            )
            items.setdefault(_identity(item), item)
    return list(items.values())
