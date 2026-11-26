"""Reading back what serve recorded under one worker assignment (#1325, run-receipt waiver).

The database is built with the real ``ReceiptStore`` the way serve builds its evidence store, and
the shape of a root is the one a real relay request leaves (proved against the real app in
``tests/test_opencode_assignment_serve_path.py``).  Nothing here touches the real home.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from verdict.orchestration import serve_evidence as evidence
from verdict.receipt_store import ReceiptStore

SCOPE = "server-auth"
WAIVER = {
    "requirement": "streaming",
    "reason": "verified_worker_assignment",
    "run_id": "run-1",
    "node_id": "node-1",
}


def _digest(name: str) -> str:
    return hashlib.sha256(name.encode()).hexdigest()


#: A waiver field that is present and JSON ``null`` (``None`` leaves the field out).
JSON_NULL = object()


def _root(digest: str | None, waivers: Any = None) -> dict[str, Any]:
    receipt: dict[str, Any] = {}
    if digest is not None:
        receipt["worker_assignment"] = {"assignment_digest": digest, "route_id": "r/m"}
    if waivers is JSON_NULL:
        receipt["requirement_waivers"] = None
    elif waivers is not None:
        receipt["requirement_waivers"] = waivers
    return {"routing_decision": {"receipt": receipt}}


class Db:
    """A serve evidence database under a temp directory."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.store = ReceiptStore(path, strict_scope=True)
        self.count = 0

    def put(
        self,
        digest: str | None,
        waivers: Any = None,
        *,
        parent: str | None = None,
        kind: str | None = None,
    ) -> str:
        self.count += 1
        receipt_id = f"evidence-{self.count}"
        self.store.put_receipt(
            kind or ("decision" if parent is None else "execution"),  # type: ignore[arg-type]
            SCOPE,
            _root(digest, waivers),
            receipt_id=receipt_id,
            parent_receipt_id=parent,
        )
        return receipt_id

    def raw_row(self, payload_json: str) -> None:
        """A row whose payload is not JSON, as a damaged database might hold."""
        conn = sqlite3.connect(self.path)
        try:
            conn.execute(
                "INSERT INTO receipts(receipt_id, timestamp, receipt_type, scope, payload_json,"
                " content_hash, sensitivity, provenance_json, sequence, record_hash)"
                " VALUES (?, 0, 'decision', ?, ?, 'h', 'internal', '{}', 9999, 'h')",
                (f"damaged-{self.count}", SCOPE, payload_json),
            )
            conn.commit()
        finally:
            conn.close()


@pytest.fixture
def db(tmp_path: Path) -> Db:
    return Db(tmp_path / "serve-receipts.db")


def _read(
    db: Db,
    digest: str,
    *,
    mark: evidence.EvidenceMark | None = None,
    association_shown: bool = False,
) -> dict[str, Any]:
    # the default of the reader is "not shown"; only a test that says otherwise passes the keyword
    shown: dict[str, bool] = {"association_shown": True} if association_shown else {}
    return evidence.read_assignment_evidence(
        mark or evidence.EvidenceMark(db.path, 0), digest, **shown
    )


def test_requests_under_the_assignment_are_counted_and_the_waiver_is_named(db: Db) -> None:
    mine = _digest("mine")
    db.put(mine, [WAIVER])
    db.put(mine, [WAIVER])
    db.put(mine, [])  # a request that required no waiver
    result = _read(db, mine)
    assert result == {
        "status": "read",
        "reason": None,
        "requests": 3,
        "requests_waived": 2,
        "requirement_waivers": [
            {"requirement": "streaming", "reason": "verified_worker_assignment"}
        ],
    }


def test_an_attempt_whose_requests_waived_nothing_reports_an_empty_list_not_unavailable(
    db: Db,
) -> None:
    mine = _digest("mine")
    db.put(mine, [])
    db.put(mine)  # no waiver field at all
    result = _read(db, mine)
    assert (result["status"], result["requests"], result["requests_waived"]) == ("read", 2, 0)
    assert result["requirement_waivers"] == []


def test_nothing_recorded_is_zero_requests_once_the_database_is_shown_to_be_serves(db: Db) -> None:
    db.put(_digest("someone else"), [WAIVER])
    result = _read(db, _digest("mine"), association_shown=True)
    assert (result["status"], result["requests"], result["requests_waived"]) == ("read", 0, 0)
    assert result["requirement_waivers"] == []
    assert result["reason"] is None


def test_nothing_recorded_in_a_database_not_shown_to_be_serves_says_nothing_about_serve(
    db: Db,
) -> None:
    """The database is found from this host's configuration, not from the serve at the address the
    worker called, so an empty read of it is not "serve recorded nothing": it is unavailable, and
    never "no waiver" (the same figures a database that is serve's would give)."""
    db.put(_digest("someone else"), [WAIVER])
    result = _read(db, _digest("mine"))
    assert result["status"] == "unavailable"
    assert result["requests"] is None
    assert result["requests_waived"] is None
    assert result["requirement_waivers"] is None
    assert "not shown to be" in result["reason"]
    assert _read(db, _digest("mine"), association_shown=False) == result


def test_a_request_under_the_assignment_is_read_with_or_without_a_shown_association(db: Db) -> None:
    """A row that carries this attempt's own digest was written by whoever received the
    assignment; that is what shows the association, so it never needs showing first."""
    mine = _digest("mine")
    db.put(mine, [WAIVER])
    unshown = _read(db, mine, association_shown=False)
    shown = _read(db, mine, association_shown=True)
    assert unshown == shown
    assert (unshown["status"], unshown["requests"], unshown["requests_waived"]) == ("read", 1, 1)


def test_another_assignments_waiver_is_never_attributed(db: Db) -> None:
    db.put(_digest("other"), [WAIVER])
    mine = _digest("mine")
    db.put(mine, [])
    result = _read(db, mine)
    assert (result["requests"], result["requests_waived"]) == (1, 0)
    assert result["requirement_waivers"] == []


def test_only_rows_after_the_mark_can_belong_to_the_attempt(db: Db) -> None:
    mine = _digest("mine")
    db.put(mine, [WAIVER])  # an earlier attempt that happened to carry the same digest
    mark = evidence.take_mark(db.path)
    assert mark.rowid > 0 and not mark.reason
    db.put(mine, [])
    result = _read(db, mine, mark=mark)
    assert (result["requests"], result["requests_waived"]) == (1, 0)


@pytest.mark.parametrize("kind", ["execution", "decision"])
def test_events_under_a_root_are_not_roots(db: Db, kind: str) -> None:
    mine = _digest("mine")
    root = db.put(mine, [])
    db.put(mine, [WAIVER], parent=root, kind=kind)  # even one typed like a root
    assert _read(db, mine)["requests"] == 1


def test_a_damaged_row_does_not_hide_the_good_ones(db: Db) -> None:
    mine = _digest("mine")
    db.put(mine, [WAIVER])
    db.raw_row("{not json")
    result = _read(db, mine)
    assert (result["status"], result["requests"], result["requests_waived"]) == ("read", 1, 1)


@pytest.mark.parametrize(
    "bad",
    [
        "streaming",
        "[]",  # the JSON string "[]", not an array: json_extract returns the same text for both
        "null",
        "",
        7,
        0,
        True,
        False,
        {"requirement": "streaming"},
        [1],
        [{"requirement": 3, "reason": "x"}],
        [{"requirement": "streaming"}],
        [["streaming", "x"]],
    ],
    ids=[
        "string",
        "string-empty-array-text",
        "string-null-text",
        "empty-string",
        "int",
        "zero",
        "true",
        "false",
        "object",
        "array-of-int",
        "array-wrong-types",
        "array-missing-reason",
        "array-of-array",
    ],
)
def test_a_waiver_record_that_is_not_a_list_of_waivers_makes_the_read_unavailable(
    db: Db, bad: Any
) -> None:
    mine = _digest("mine")
    db.put(mine, bad)
    result = _read(db, mine)
    assert result["status"] == "unavailable"
    assert result["requests"] is None and result["requirement_waivers"] is None
    assert "waiver" in result["reason"]


def test_too_many_matching_roots_is_unavailable_never_a_short_count(
    db: Db, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(evidence, "MAX_MATCHING_ROOTS", 3)
    mine = _digest("mine")
    for _ in range(4):
        db.put(mine, [WAIVER])
    result = _read(db, mine)
    assert result["status"] == "unavailable"
    assert result["requests_waived"] is None
    assert "more than 3" in result["reason"]
    # Exactly at the limit is still a read.
    other = _digest("exact")
    for _ in range(3):
        db.put(other, [])
    assert _read(db, other)["requests"] == 3


def test_a_read_that_runs_out_of_time_is_unavailable(
    db: Db, monkeypatch: pytest.MonkeyPatch
) -> None:
    mine = _digest("mine")
    for _ in range(50):
        db.put(mine, [WAIVER])
    monkeypatch.setattr(evidence, "READ_DEADLINE_SECONDS", -1.0)
    monkeypatch.setattr(evidence, "_PROGRESS_STEPS", 1)
    result = _read(db, mine)
    assert result["status"] == "unavailable"
    assert "out of time" in result["reason"]
    assert result["requests"] is None


@pytest.mark.parametrize(
    ("path", "expected"),
    [(None, "not configured"), ("", "not configured"), (":memory:", "in memory")],
)
def test_a_database_that_cannot_be_read_by_another_process_is_unavailable_with_its_reason(
    path: Any, expected: str
) -> None:
    mark = evidence.take_mark(path)
    assert expected in mark.reason
    result = evidence.read_assignment_evidence(mark, _digest("mine"))
    assert result["status"] == "unavailable" and expected in result["reason"]
    assert result["requests"] is None and result["requests_waived"] is None
    assert result["requirement_waivers"] is None


def test_a_missing_file_is_mark_zero_and_unavailable_at_read_time_and_is_never_created(
    tmp_path: Path,
) -> None:
    path = tmp_path / "later" / "serve-receipts.db"
    mark = evidence.take_mark(path)
    assert (mark.rowid, mark.reason) == (0, "")
    result = evidence.read_assignment_evidence(mark, _digest("mine"))
    assert result["status"] == "unavailable" and "does not exist" in result["reason"]
    assert not path.exists() and not path.parent.exists(), "the reader created something"


def test_a_file_that_is_not_a_database_is_unavailable_and_unchanged(tmp_path: Path) -> None:
    path = tmp_path / "junk.db"
    path.write_bytes(b"this is not a sqlite database" * 20)
    before = path.read_bytes()
    mark = evidence.take_mark(path)
    assert "cannot be read" in mark.reason
    result = evidence.read_assignment_evidence(mark, _digest("mine"))
    assert result["status"] == "unavailable"
    assert path.read_bytes() == before


def test_a_database_without_the_receipts_table_is_unavailable_and_gains_no_table(
    tmp_path: Path,
) -> None:
    path = tmp_path / "other.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE unrelated (x)")
    conn.commit()
    conn.close()
    before = path.read_bytes()
    mark = evidence.take_mark(path)
    assert "cannot be read" in mark.reason
    assert evidence.read_assignment_evidence(mark, _digest("mine"))["status"] == "unavailable"
    assert path.read_bytes() == before


def test_reading_changes_nothing_in_the_database(db: Db) -> None:
    mine = _digest("mine")
    db.put(mine, [WAIVER])
    conn = sqlite3.connect(db.path)
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()
    before = db.path.read_bytes()
    assert evidence.take_mark(db.path).rowid > 0
    result = _read(db, mine)
    assert (result["requests"], result["requests_waived"]) == (1, 1)
    assert db.path.read_bytes() == before
    # A write through the reader's connection is refused: it is open read-only.
    conn = evidence._connect(db.path, 10**12)
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("DELETE FROM receipts")
    conn.close()


def test_the_digest_and_the_mark_are_the_only_inputs_and_no_row_is_returned(db: Db) -> None:
    mine = _digest("mine")
    db.put(mine, [WAIVER])
    result = _read(db, mine)
    assert set(result) == {"status", "reason", "requests", "requests_waived", "requirement_waivers"}
    assert "run_id" not in str(result) and "node_id" not in str(result)


def test_a_waiver_field_that_is_missing_or_json_null_is_no_waiver_not_unavailable(db: Db) -> None:
    mine = _digest("mine")
    db.put(mine)  # no field
    db.put(mine, JSON_NULL)  # the field, null
    db.put(mine, [])  # the field, an empty array
    result = _read(db, mine)
    assert (result["status"], result["requests"], result["requests_waived"]) == ("read", 3, 0)
    assert result["requirement_waivers"] == []


# --- which file a database is ---------------------------------------------------------------
#
# What was found in a database (a request under an assignment the caller issued) vouches for that
# file only: a path can be re-pointed at another database, or the file replaced, and then it
# vouches for nothing.  The file's device and inode say which file a path names right now.


def _identity_of(path: Path) -> tuple[int, int]:
    stat = os.stat(path)
    return (stat.st_dev, stat.st_ino)


def test_a_mark_names_the_file_it_was_taken_of(db: Db) -> None:
    mark = evidence.take_mark(db.path)
    assert mark.identity == _identity_of(db.path)
    assert evidence.database_identity(db.path) == mark.identity


def test_a_file_that_does_not_exist_yet_has_no_identity(tmp_path: Path) -> None:
    missing = tmp_path / "not-yet.db"
    assert evidence.database_identity(missing) is None
    assert evidence.take_mark(missing).identity is None
    assert evidence.database_identity(None) is None
    assert not missing.exists()


def test_a_replaced_file_and_a_retargeted_link_are_other_files(tmp_path: Path) -> None:
    first = Db(tmp_path / "first.db")
    second = Db(tmp_path / "second.db")
    one, other = _identity_of(first.path), _identity_of(second.path)
    assert one != other

    link = tmp_path / "serve-receipts.db"
    link.symlink_to(first.path)
    assert evidence.database_identity(link) == one
    link.unlink()
    link.symlink_to(second.path)
    assert evidence.database_identity(link) == other  # retargeted

    plain = tmp_path / "plain.db"
    Db(plain)
    before = evidence.database_identity(plain)
    os.replace(second.path, plain)  # another database moved into the path
    assert evidence.database_identity(plain) == other != before


def test_an_unreadable_path_has_no_identity(tmp_path: Path) -> None:
    assert evidence.database_identity(tmp_path / "no-such-dir" / "x.db") is None


# --- what was seen, kept per serve address and file ----------------------------------------

ORIGIN = "http://127.0.0.1:8000"


def _seen_read(
    seen: evidence.SeenDatabases, db: Db, digest: str, *, origin: str = ORIGIN
) -> dict[str, Any]:
    return seen.read(evidence.take_mark(db.path), digest, origin=origin)


def test_an_empty_read_is_zero_requests_only_after_a_request_was_found_in_the_same_file(
    db: Db,
) -> None:
    seen = evidence.SeenDatabases()
    first = _seen_read(seen, db, _digest("first"))
    assert first["status"] == "unavailable"  # nothing is known about this file yet
    db.put(_digest("second"), [WAIVER])
    mark = evidence.take_mark(db.path)
    db.put(_digest("third"), [])
    found = seen.read(mark, _digest("third"), origin=ORIGIN)
    assert (found["status"], found["requests"]) == ("read", 1)
    again = _seen_read(seen, db, _digest("fourth"))
    assert (again["status"], again["requests"], again["requests_waived"]) == ("read", 0, 0)


def test_what_was_found_for_one_serve_address_does_not_vouch_for_another(db: Db) -> None:
    seen = evidence.SeenDatabases()
    mark = evidence.take_mark(db.path)
    db.put(_digest("mine"), [])
    assert seen.read(mark, _digest("mine"), origin=ORIGIN)["requests"] == 1
    assert _seen_read(seen, db, _digest("next"))["status"] == "read"
    elsewhere = _seen_read(seen, db, _digest("next"), origin="http://127.0.0.1:18325")
    assert elsewhere["status"] == "unavailable"
    assert "not shown to be" in elsewhere["reason"]


def test_what_was_found_in_a_file_is_not_carried_to_the_file_that_replaces_it(
    tmp_path: Path,
) -> None:
    seen = evidence.SeenDatabases()
    first = Db(tmp_path / "first.db")
    spare = Db(tmp_path / "spare.db")
    mark = evidence.take_mark(first.path)
    first.put(_digest("mine"), [])
    assert seen.read(mark, _digest("mine"), origin=ORIGIN)["requests"] == 1
    path = first.path
    os.replace(spare.path, path)  # another valid receipt database now sits at the path
    replaced = seen.read(evidence.take_mark(path), _digest("next"), origin=ORIGIN)
    assert replaced["status"] == "unavailable"
    assert replaced["requests"] is None


def test_a_file_that_was_not_the_one_marked_is_not_vouched_for(db: Db) -> None:
    """The mark was taken of another file than the one read (the path was re-pointed in between)."""
    seen = evidence.SeenDatabases()
    mark = evidence.take_mark(db.path)
    db.put(_digest("mine"), [])
    assert seen.read(mark, _digest("mine"), origin=ORIGIN)["requests"] == 1
    stale = evidence.EvidenceMark(mark.db_path, mark.rowid, identity=(mark.identity[0], 1))  # type: ignore[index]
    result = seen.read(stale, _digest("next"), origin=ORIGIN)
    assert result["status"] == "unavailable"


def test_a_file_replaced_while_it_is_read_is_unavailable(
    db: Db, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = evidence.SeenDatabases()
    db.put(_digest("mine"), [])
    real = evidence.database_identity(db.path)
    # SeenDatabases looks, the reader looks before and after its query (the same file both times),
    # then the path names another file by the time SeenDatabases looks again: the swap came after
    # the reader's own check, which is the window only SeenDatabases' check still covers.
    answers = iter([real, real, real, (1, 2)])
    monkeypatch.setattr(evidence, "database_identity", lambda path: next(answers))
    result = seen.read(evidence.EvidenceMark(db.path, 0), _digest("mine"), origin=ORIGIN)
    assert result["status"] == "unavailable"
    assert "replaced while" in result["reason"]
    assert result["requests"] is None


# --- a file replaced after the mark is another file, whatever rows it holds -----------------
#
# The mark's row watermark counts the rows of the file the mark was taken of.  Applied to another
# file it would hide the requests at or below it (the ones that may hold a waiver) and still give
# a "read" answer from the rows above it, so a mark of one file is never applied to another.


def _replaced_after_the_mark(tmp_path: Path) -> tuple[Db, evidence.EvidenceMark]:
    """A mark taken of one file (watermark 3), then another file moved into the path.

    The other file holds a waived request under ``mine`` at or below the old watermark (rows 1-3)
    and an unwaived one above it (row 4): applying the old watermark counts one request, waived
    nothing, and reads as authoritative.
    """
    mine = _digest("mine")
    first = Db(tmp_path / "first.db")
    for _ in range(3):
        first.put(_digest("an earlier attempt"), [])
    mark = evidence.take_mark(first.path)
    assert mark.rowid == 3 and mark.identity is not None

    spare = Db(tmp_path / "spare.db")
    for _ in range(3):
        spare.put(mine, [WAIVER])
    spare.put(mine, [])
    assert evidence.take_mark(spare.path).rowid == 4
    os.replace(spare.path, first.path)
    assert evidence.database_identity(first.path) != mark.identity
    return first, mark


@pytest.mark.parametrize("reader", ["the reader", "SeenDatabases"])
def test_a_file_replaced_after_the_mark_is_unavailable_with_rows_either_side_of_its_watermark(
    tmp_path: Path, reader: str
) -> None:
    replaced, mark = _replaced_after_the_mark(tmp_path)
    if reader == "the reader":
        result = evidence.read_assignment_evidence(mark, _digest("mine"))
    else:
        result = evidence.SeenDatabases().read(mark, _digest("mine"), origin=ORIGIN)
    assert result["status"] == "unavailable"
    assert "replaced" in result["reason"]
    assert result["requests"] is None
    assert result["requests_waived"] is None
    assert result["requirement_waivers"] is None
    assert replaced.path.exists()


@pytest.mark.parametrize("marked_file", ["a file that existed", "no file yet"])
def test_a_file_replaced_between_the_check_and_the_connection_is_unavailable_from_the_bare_reader(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, marked_file: str
) -> None:
    """The identity check passes, then the path is replaced just before the connection opens it.

    The connection opens whatever the path names by then: with a mark of a file the old watermark
    would be applied to the other file, and read as authoritative.  Only a caller that goes through
    ``SeenDatabases`` had a check after the read; the reader itself must have one.
    """
    mine = _digest("mine")
    path = tmp_path / "serve-receipts.db"
    if marked_file == "a file that existed":
        first = Db(path)
        for _ in range(3):
            first.put(_digest("an earlier attempt"), [])
        mark = evidence.take_mark(path)
        assert mark.rowid == 3 and mark.identity is not None
    else:
        mark = evidence.take_mark(path)
        assert (mark.rowid, mark.identity) == (0, None)
        first = Db(path)
        first.put(mine, [])
    spare = Db(tmp_path / "spare.db")
    for _ in range(3):
        spare.put(mine, [WAIVER])
    spare.put(mine, [])

    real_connect = evidence._connect
    connects: list[Path] = []

    def replaced_just_before_the_connection(db_path: Path, deadline: float) -> Any:
        assert evidence.database_identity(db_path) == mark.identity or mark.identity is None
        connects.append(db_path)
        os.replace(spare.path, path)  # another valid database moves in after the check passed
        return real_connect(db_path, deadline)

    monkeypatch.setattr(evidence, "_connect", replaced_just_before_the_connection)
    result = evidence.read_assignment_evidence(mark, mine, association_shown=True)

    assert connects == [path]  # the replacement happened after the first check, on the real path
    assert result["status"] == "unavailable"
    assert "replaced while it was read" in result["reason"]
    assert result["requests"] is None
    assert result["requests_waived"] is None
    assert result["requirement_waivers"] is None


def test_a_file_replaced_after_the_mark_is_not_associated_with_the_serve_it_was_read_for(
    tmp_path: Path,
) -> None:
    """The request found above the old watermark is in a file the attempt never marked, so it
    cannot show that file is the database of the serve the worker called: a later empty read of
    it is still not "serve recorded nothing"."""
    replaced, mark = _replaced_after_the_mark(tmp_path)
    seen = evidence.SeenDatabases()
    assert seen.read(mark, _digest("mine"), origin=ORIGIN)["status"] == "unavailable"
    later = _seen_read(seen, replaced, _digest("a later attempt"))
    assert later["status"] == "unavailable"
    assert "not shown to be" in later["reason"]


def test_a_database_first_created_after_the_mark_is_read_from_its_first_row(tmp_path: Path) -> None:
    """Serve creates its database when it starts: the mark of a path with no file is watermark 0
    with no identity, and every row the file holds later is the attempt's."""
    path = tmp_path / "serve-receipts.db"
    mark = evidence.take_mark(path)
    assert (mark.rowid, mark.identity) == (0, None)
    db = Db(path)
    mine = _digest("mine")
    db.put(mine, [WAIVER])
    db.put(mine, [])

    direct = evidence.read_assignment_evidence(mark, mine)
    assert (direct["status"], direct["requests"], direct["requests_waived"]) == ("read", 2, 1)

    seen = evidence.SeenDatabases()
    found = seen.read(mark, mine, origin=ORIGIN)
    assert found == direct
    # the request found shows the file is serve's: a later attempt's empty read is then zero
    later = _seen_read(seen, db, _digest("a later attempt"))
    assert (later["status"], later["requests"]) == ("read", 0)
