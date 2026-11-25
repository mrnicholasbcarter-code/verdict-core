"""What ``verdict serve`` recorded for one worker assignment, read back for the run receipt.

``verdict serve`` writes one evidence root per relay request into its receipt database
(``VERDICT_RECEIPTS_DB``).  A request served under a signed worker assignment carries the
assignment's digest (``routing_decision.receipt.worker_assignment.assignment_digest``, the SHA-256
of the payload text, never the credential) and, when serve waived a requirement for it, the waiver
(``routing_decision.receipt.requirement_waivers``, today ``streaming`` only).  An executor that
signed the assignment knows the digest, so after the attempt it can say how many requests serve
saw under it and whether any ran with a requirement waived.  The orchestrator's run receipt shows
that, so a reader of ``verdict run-receipt`` sees which attempts ran with streaming waived.

Reading is strictly read-only: the database is opened with ``mode=ro`` and ``query_only``, never
created, migrated or written, and only rows added since a mark taken before the worker started
are looked at (the database grows with every request and holds large payloads, so a scan of all
of it per attempt would not do).  The result is one of three things, never a guess:

* ``status: "read"`` (``reason: None``) with the counts and the distinct waivers (an empty list
  when serve saw the assignment and waived nothing, ``requests: 0`` when it recorded nothing
  under it);
* ``status: "unavailable"`` with a ``reason`` and every figure ``None`` when the database cannot be
  read, is not configured, is in memory (another process cannot read it) or holds too many rows to
  count.  Unavailable is never reported as "no waiver".

Nothing here touches a credential: the digest is the only identifier that goes in, and a reason
names the problem, never a path's contents or a row.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = ["EvidenceMark", "read_assignment_evidence", "take_mark"]

#: A read is cut off after this many seconds of work, whatever the database holds.  It runs after
#: the worker is gone and inside the settle reserve the runtime leaves an executor (at most 15 s,
#: of which the kill grace takes 5), so it is kept short.
READ_DEADLINE_SECONDS = 2.0
#: More matching roots than this and the figures would be a guess: the read is unavailable.
MAX_MATCHING_ROOTS = 10_000
#: How long a read waits for a lock another process holds.
_BUSY_TIMEOUT_SECONDS = 1.0
#: The SQLite virtual-machine steps between two looks at the deadline.
_PROGRESS_STEPS = 1000

#: Constant text, no value is ever formatted into it: the digest, the mark and the limit are bound.
_MATCHING_ROOTS_SQL = """
    SELECT json_extract(payload_json, '$.routing_decision.receipt.requirement_waivers')
    FROM receipts
    WHERE rowid > ? AND receipt_type = 'decision' AND parent_receipt_id IS NULL
      AND CASE WHEN json_valid(payload_json)
               THEN json_extract(
                   payload_json, '$.routing_decision.receipt.worker_assignment.assignment_digest'
               ) END = ?
    LIMIT ?
"""


@dataclass(frozen=True)
class EvidenceMark:
    """Where serve's receipt database stood before the worker started.

    ``rowid`` is the highest row there was (0 for no rows or no file yet); only later rows can
    belong to the attempt.  ``reason`` is set when the database cannot be read at all, and then
    the later read is unavailable for the same reason.
    """

    db_path: Path | None
    rowid: int
    reason: str = ""


def _connect(path: Path, deadline: float) -> sqlite3.Connection:
    """A read-only connection that gives up at ``deadline`` (``time.monotonic()``)."""
    conn = sqlite3.connect(
        f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=_BUSY_TIMEOUT_SECONDS
    )
    conn.execute("PRAGMA query_only = ON")
    conn.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, _PROGRESS_STEPS)
    return conn


def take_mark(db_path: Path | str | None) -> EvidenceMark:
    """Note the newest row of serve's receipt database; never creates or changes anything.

    A path that does not exist yet is mark 0 (serve creates it when it starts, so every row it
    holds later is new).  ``None``, an in-memory database or a file that cannot be read give a
    mark whose ``reason`` the later read repeats.
    """
    if db_path is None or str(db_path).strip() == "":
        return EvidenceMark(
            None, 0, "serve's receipt database is not configured (VERDICT_RECEIPTS_DB)"
        )
    if str(db_path) == ":memory:":
        return EvidenceMark(
            None, 0, "serve's receipt database is in memory, which another process cannot read"
        )
    path = Path(db_path).expanduser()
    if not path.exists():
        return EvidenceMark(path, 0)
    try:
        conn = _connect(path, time.monotonic() + READ_DEADLINE_SECONDS)
        try:
            row = conn.execute("SELECT max(rowid) FROM receipts").fetchone()
        finally:
            conn.close()
    except (sqlite3.Error, OSError, ValueError) as exc:
        return EvidenceMark(
            path, 0, f"serve's receipt database cannot be read ({type(exc).__name__})"
        )
    return EvidenceMark(path, int(row[0] or 0))


def _unavailable(reason: str) -> dict[str, Any]:
    return {
        "status": "unavailable",
        "reason": reason,
        "requests": None,
        "requests_waived": None,
        "requirement_waivers": None,
    }


def _distinct_waivers(raw: str | None) -> list[dict[str, str]] | None:
    """The waivers one root records, or ``None`` when the record is not a list of waivers."""
    if raw is None:
        return []
    try:
        decoded = json.loads(raw)
    except ValueError:
        return None
    if decoded is None:
        return []
    if not isinstance(decoded, list):
        return None
    found: list[dict[str, str]] = []
    for item in decoded:
        if not isinstance(item, dict):
            return None
        requirement, reason = item.get("requirement"), item.get("reason")
        if not isinstance(requirement, str) or not isinstance(reason, str):
            return None
        found.append({"requirement": requirement, "reason": reason})
    return found


def read_assignment_evidence(mark: EvidenceMark, digest: str) -> dict[str, Any]:
    """What serve recorded under the assignment with ``digest`` since ``mark``.

    Blocking (it reads a file); run it off the event loop.  Never raises.
    """
    if mark.reason:
        return _unavailable(mark.reason)
    if mark.db_path is None:
        return _unavailable("serve's receipt database is not configured (VERDICT_RECEIPTS_DB)")
    if not mark.db_path.exists():
        return _unavailable("serve's receipt database does not exist")
    deadline = time.monotonic() + READ_DEADLINE_SECONDS
    try:
        conn = _connect(mark.db_path, deadline)
        try:
            rows = conn.execute(
                _MATCHING_ROOTS_SQL, (mark.rowid, digest, MAX_MATCHING_ROOTS + 1)
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.OperationalError as exc:
        if "interrupted" in str(exc).lower():
            return _unavailable("reading serve's receipt database ran out of time")
        return _unavailable(f"serve's receipt database cannot be read ({type(exc).__name__})")
    except (sqlite3.Error, OSError, ValueError) as exc:
        return _unavailable(f"serve's receipt database cannot be read ({type(exc).__name__})")
    if len(rows) > MAX_MATCHING_ROOTS:
        return _unavailable(
            f"serve recorded more than {MAX_MATCHING_ROOTS} requests under one assignment"
        )
    distinct: list[dict[str, str]] = []
    waived = 0
    for (raw,) in rows:
        waivers = _distinct_waivers(raw)
        if waivers is None:
            return _unavailable(
                "a serve receipt holds a waiver record that is not a list of waivers"
            )
        if waivers:
            waived += 1
        for waiver in waivers:
            if waiver not in distinct:
                distinct.append(waiver)
    return {
        "status": "read",
        "reason": None,
        "requests": len(rows),
        "requests_waived": waived,
        "requirement_waivers": distinct,
    }
