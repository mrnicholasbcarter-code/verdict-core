"""Acceptance coverage for the durable receipt ledger (#117)."""

from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from verdict.receipt_store import (
    ReceiptConflictError,
    ReceiptIntegrityError,
    ReceiptStore,
    ReceiptStoreError,
)


def test_file_store_survives_reopen_and_scope_isolation(tmp_path) -> None:
    path = tmp_path / "receipts.db"
    first = ReceiptStore(path, strict_scope=True)
    record = first.put_receipt("decision", "tenant-a/project/session", {"ok": True})
    first.put_receipt("decision", "tenant-b/project/session", {"ok": False})

    second = ReceiptStore(path, strict_scope=True)
    assert second.get_receipt(record.receipt_id, scope="tenant-a/project/session") is not None
    assert second.get_receipt(record.receipt_id, scope="tenant-b/project/session") is None
    with pytest.raises(ValueError):
        second.query_receipts()


def test_wal_and_integrity_chain(tmp_path) -> None:
    path = tmp_path / "receipts.db"
    store = ReceiptStore(path, strict_scope=True)
    store.put_receipt("context", "scope", {"one": 1})
    store.put_receipt("outcome", "scope", {"two": 2})
    with sqlite3.connect(path) as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert store.verify_integrity(scope="scope")["valid"] is True


def test_concurrent_writers_and_idempotency(tmp_path) -> None:
    path = tmp_path / "receipts.db"

    def write(index: int):
        return ReceiptStore(path, strict_scope=True).put_receipt(
            "execution", "scope", {"index": index}, idempotency_key=f"id-{index}"
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        records = list(pool.map(write, range(24)))
    assert len({record.receipt_id for record in records}) == 24
    store = ReceiptStore(path, strict_scope=True)
    duplicate = store.put_receipt("execution", "scope", {"index": 1}, idempotency_key="id-1")
    assert duplicate.receipt_id == records[1].receipt_id
    with pytest.raises(ReceiptConflictError):
        store.put_receipt("execution", "scope", {"index": 999}, idempotency_key="id-1")


def test_terminal_events_are_idempotent_and_conflicts_are_rejected() -> None:
    store = ReceiptStore(":memory:", strict_scope=True)
    root = store.put_receipt("decision", "scope", {"route": "a"})
    first = store.append_event(
        root.receipt_id,
        scope="scope",
        payload={"event": "success"},
        event_id="terminal-1",
        event_type="completed",
        terminal_outcome="success",
    )
    assert (
        store.append_event(
            root.receipt_id,
            scope="scope",
            payload={"event": "success"},
            event_id="terminal-1",
            event_type="completed",
            terminal_outcome="success",
        ).receipt_id
        == first.receipt_id
    )
    with pytest.raises(ReceiptConflictError):
        store.append_event(
            root.receipt_id,
            scope="scope",
            payload={"event": "failure"},
            event_id="terminal-2",
            event_type="failed",
            terminal_outcome="failure",
        )


def test_redaction_retention_tombstone_and_replay() -> None:
    store = ReceiptStore(":memory:", strict_scope=True)
    record = store.put_receipt(
        "context",
        "scope",
        {"prompt": "private", "tool_arguments": {"password": "secret"}, "safe": 1},
    )
    assert record.payload["prompt"] == "[REDACTED]"
    assert record.payload["tool_arguments"] == "[REDACTED]"
    assert store.replay(scope="scope")["receipt_count"] == 1
    removed = store.apply_retention(scope="scope", max_age_seconds=0, now=record.timestamp + 1)
    assert removed == [record.receipt_id]
    assert store.get_receipt(record.receipt_id, scope="scope") is None
    assert len(store.query_receipts(scope="scope", include_tombstones=True)) == 2


def test_export_import_is_deterministic_and_tamper_evident() -> None:
    source = ReceiptStore(":memory:", strict_scope=True)
    source.put_receipt("decision", "scope", {"x": 1})
    manifest = source.export_manifest(scope="scope")
    target = ReceiptStore(":memory:", strict_scope=True)
    assert target.import_manifest(manifest) == 1
    assert target.import_manifest(manifest) == 0
    manifest["receipts"][0]["payload"]["x"] = 2
    with pytest.raises(ReceiptIntegrityError):
        target.import_manifest(manifest)


def test_tampering_metadata_breaks_integrity(tmp_path) -> None:
    path = tmp_path / "receipts.db"
    store = ReceiptStore(path, strict_scope=True)
    record = store.put_receipt("decision", "scope", {"x": 1}, provenance={"source": "test"})
    with sqlite3.connect(path) as conn:
        conn.execute(
            "UPDATE receipts SET sensitivity = ? WHERE receipt_id = ?",
            ("public", record.receipt_id),
        )
        conn.commit()
    assert store.verify_integrity(scope="scope")["valid"] is False


def test_import_rolls_back_on_partial_failure() -> None:
    source = ReceiptStore(":memory:", strict_scope=True)
    source.put_receipt("decision", "scope", {"x": 1})
    manifest = source.export_manifest(scope="scope")
    manifest["receipts"].append({"receipt_id": "bad", "scope": "scope"})
    import hashlib
    import json

    manifest["manifest_digest"] = hashlib.sha256(
        json.dumps(manifest["receipts"], sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    target = ReceiptStore(":memory:", strict_scope=True)
    with pytest.raises(ReceiptStoreError):
        target.import_manifest(manifest)
    assert target.query_receipts(scope="scope") == []
