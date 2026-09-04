"""Retention and erasure verification for the launch gate (T025)."""

from __future__ import annotations

import json
from pathlib import Path

from verdict.receipt_store import ReceiptStore

RETENTION_WINDOW_SECONDS = 30 * 24 * 60 * 60


def test_erasure_request_makes_synthetic_data_unreachable_within_30_days(tmp_path: Path) -> None:
    store = ReceiptStore(tmp_path / "receipts.db", strict_scope=True)
    scope = "synthetic-subject"
    synthetic_content = "synthetic boundary content; not real personal data"
    record = store.put_receipt(
        "context",
        scope,
        {"note": synthetic_content},
        provenance={"source": "retention-erasure-test"},
    )

    removal_time = record.timestamp + RETENTION_WINDOW_SECONDS + 1
    removed = store.apply_retention(
        scope=scope, max_age_seconds=RETENTION_WINDOW_SECONDS, now=removal_time
    )

    assert removed == [record.receipt_id]
    assert store.get_receipt(record.receipt_id, scope=scope) is None
    visible = store.query_receipts(scope=scope, include_tombstones=False)
    assert all(item.receipt_id != record.receipt_id for item in visible)
    tombstones = store.query_receipts(scope=scope, include_tombstones=True)
    assert len(tombstones) == 2
    tombstone = next(item for item in tombstones if item.is_tombstone)
    assert synthetic_content not in json.dumps(tombstone.payload)


def test_erasure_request_cannot_remove_another_scope(tmp_path: Path) -> None:
    store = ReceiptStore(tmp_path / "receipts.db", strict_scope=True)
    record = store.put_receipt("context", "subject-a", {"note": "synthetic-only"})

    try:
        store.tombstone(record.receipt_id, scope="subject-b")
    except KeyError:
        pass
    else:
        raise AssertionError("erasure crossed the authorized scope boundary")

    assert store.get_receipt(record.receipt_id, scope="subject-a") is not None
