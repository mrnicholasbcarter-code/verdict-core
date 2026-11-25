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
  read, is not configured, is in memory (another process cannot read it), holds too many rows to
  count, or holds no request under the assignment while nothing shows that it is the database of
  the serve the worker called.  Unavailable is never reported as "no waiver".

The database is found from this host's configuration (``VERDICT_RECEIPTS_DB``, ``serve.env``),
never from the serve at the address the worker called, so with a spare or remote serve it can be
another serve's, or one nothing writes to.  A request found under the assignment's digest shows
the database is serve's (only the serve at that address received the assignment); an empty read
means "serve recorded nothing" only once the caller has such a sign for the same address and
database (``association_shown``), and is otherwise unavailable.  "The same database" means the
same file, not the same path: a path can be replaced or a link retargeted with another valid
receipt database, so what was seen is bound to the file's device and inode
(:func:`database_identity`) and :class:`SeenDatabases` keeps it.

Nothing here touches a credential: the digest is the only identifier that goes in, and a reason
names the problem, never a path's contents or a row.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = [
    "NOT_SHOWN_REASON",
    "EvidenceMark",
    "SeenDatabases",
    "database_identity",
    "read_assignment_evidence",
    "take_mark",
]

#: A read is cut off after this many seconds of work, whatever the database holds.  It runs after
#: the worker is gone and inside the settle reserve the runtime leaves an executor (at most 15 s,
#: of which the kill grace takes 5), so it is kept short.
READ_DEADLINE_SECONDS = 2.0
#: More matching roots than this and the figures would be a guess: the read is unavailable.
MAX_MATCHING_ROOTS = 10_000
#: How long a read waits for a lock another process holds.
_BUSY_TIMEOUT_SECONDS = 1.0
#: Why an empty read is not a statement about serve (see the module docstring).
NOT_SHOWN_REASON = (
    "no request under the assignment was found, and this host's receipt database is not shown "
    "to be the one the serve at the worker's address writes"
)
#: The SQLite virtual-machine steps between two looks at the deadline.
_PROGRESS_STEPS = 1000

#: Constant text, no value is ever formatted into it: the digest, the mark and the limit are bound.
_MATCHING_ROOTS_SQL = """
    SELECT json_type(payload_json, '$.routing_decision.receipt.requirement_waivers'),
           json_extract(payload_json, '$.routing_decision.receipt.requirement_waivers')
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
    the later read is unavailable for the same reason.  ``identity`` is the file's device and
    inode when it existed (``None`` when it did not yet).
    """

    db_path: Path | None
    rowid: int
    reason: str = ""
    identity: tuple[int, int] | None = None


def database_identity(path: Path | str | None) -> tuple[int, int] | None:
    """The device and inode of the file ``path`` names right now, or ``None``.

    Follows links, so a link retargeted at another database, or a file replaced by another, has
    another identity under the same path.  ``None`` for no path, a path that does not exist or
    cannot be examined.  Never creates or changes anything.
    """
    if path is None or str(path).strip() == "":
        return None
    try:
        stat = os.stat(Path(path).expanduser())
    except (OSError, ValueError):
        return None
    return (stat.st_dev, stat.st_ino)


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
    identity = database_identity(path)
    try:
        conn = _connect(path, time.monotonic() + READ_DEADLINE_SECONDS)
        try:
            row = conn.execute("SELECT max(rowid) FROM receipts").fetchone()
        finally:
            conn.close()
    except (sqlite3.Error, OSError, ValueError) as exc:
        return EvidenceMark(
            path, 0, f"serve's receipt database cannot be read ({type(exc).__name__})", identity
        )
    return EvidenceMark(path, int(row[0] or 0), identity=identity)


def _unavailable(reason: str) -> dict[str, Any]:
    return {
        "status": "unavailable",
        "reason": reason,
        "requests": None,
        "requests_waived": None,
        "requirement_waivers": None,
    }


def _distinct_waivers(kind: str | None, raw: Any) -> list[dict[str, str]] | None:
    """The waivers one root records, or ``None`` when the record is not a list of waivers.

    ``kind`` is SQLite's ``json_type`` of the stored value: ``json_extract`` hands back a JSON
    string, number or boolean as plain SQL text or a number, so the text of the string ``"[]"``
    would read as an empty list, and a number would not be text at all.  Only a stored array is
    read; a missing field or a JSON null is no waiver; every other type is malformed.
    """
    if kind is None or kind == "null":
        return []
    if kind != "array" or not isinstance(raw, str):
        return None
    try:
        decoded = json.loads(raw)
    except ValueError:
        return None
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


def read_assignment_evidence(
    mark: EvidenceMark, digest: str, *, association_shown: bool = False
) -> dict[str, Any]:
    """What serve recorded under the assignment with ``digest`` since ``mark``.

    ``association_shown`` says the caller has already seen this database hold a request under an
    assignment it issued for the same serve address.  Without it, a read that finds nothing is
    unavailable (:data:`NOT_SHOWN_REASON`), not "zero requests": the database may be another
    serve's.  A read that finds requests never needs it.

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
    if not rows and not association_shown:
        return _unavailable(NOT_SHOWN_REASON)
    distinct: list[dict[str, str]] = []
    waived = 0
    for kind, raw in rows:
        waivers = _distinct_waivers(kind, raw)
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


class SeenDatabases:
    """The receipt databases in which a caller has found requests under assignments it issued.

    A row that carries the digest of an assignment the caller issued shows that the file holding
    it is the database of the serve that received the assignment (only that serve could have
    written it).  That is what lets a later empty read mean "serve recorded nothing": the pair
    (serve address, file) is remembered, and only that pair, only while the path still names the
    same file and the file was the same when the attempt's mark was taken.  Thread-safe: attempts
    run concurrently.  Nothing is persisted; a new process starts with nothing seen.
    """

    def __init__(self) -> None:
        self._seen: set[tuple[str, str, tuple[int, int]]] = set()
        self._lock = threading.Lock()

    def read(self, mark: EvidenceMark, digest: str, *, origin: str) -> dict[str, Any]:
        """:func:`read_assignment_evidence`, told whether this file is already shown to be serve's.

        ``origin`` is the address of the serve the worker called.  A request found adds the pair;
        a file replaced while it was read is unavailable (what was read may be from either file).
        Blocking, never raises.
        """
        path = mark.db_path
        before = database_identity(path)
        key = (
            (origin, os.path.abspath(path), before)
            if path is not None and before is not None
            else None
        )
        with self._lock:
            shown = key is not None and before == mark.identity and key in self._seen
        record = read_assignment_evidence(mark, digest, association_shown=shown)
        if database_identity(path) != before:
            return _unavailable("serve's receipt database was replaced while it was read")
        if key is not None and record["status"] == "read" and record["requests"]:
            with self._lock:
                self._seen.add(key)
        return record
