"""Reading back what serve recorded under one worker assignment (#1325, run-receipt waiver).

The database is built with the real ``ReceiptStore`` the way serve builds its evidence store, and
the shape of a root is the one a real relay request leaves (proved against the real app in
``tests/test_opencode_assignment_serve_path.py``).  Nothing here touches the real home.
"""

from __future__ import annotations

import hashlib
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


def _read(db: Db, digest: str, *, mark: evidence.EvidenceMark | None = None) -> dict[str, Any]:
    return evidence.read_assignment_evidence(mark or evidence.EvidenceMark(db.path, 0), digest)


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


def test_nothing_recorded_under_the_assignment_is_zero_requests_and_not_a_waiver(db: Db) -> None:
    db.put(_digest("someone else"), [WAIVER])
    result = _read(db, _digest("mine"))
    assert (result["status"], result["requests"], result["requests_waived"]) == ("read", 0, 0)
    assert result["requirement_waivers"] == []


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
