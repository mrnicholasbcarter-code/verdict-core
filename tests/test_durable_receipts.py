"""Acceptance coverage for the durable receipt ledger (#117)."""

from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from verdict.evidence import (
    DurableEvidenceStore,
    ExplainEvidence,
    build_outcome_event,
    build_routing_decision_contract,
)
from verdict.models import RoutingDecision
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


# ---------------------------------------------------------------------------
# SB1b (#1360): one chain verification per DurableEvidenceStore._current call.
# Every read still fully verifies its scope; only the redundant walks go.
# ---------------------------------------------------------------------------

_SCOPE = "server-auth"


def _evidence(request_id: str) -> tuple[ExplainEvidence, object]:
    decision = RoutingDecision(
        model="primary/model",
        provider="omniroute",
        tier=1,
        reason="selected",
        request_id=request_id,
    )
    routing = build_routing_decision_contract(
        decision,
        task="summarize",
        criticality="low",
        features={"stream": False},
        request_id=request_id,
        occurred_at="2026-09-18T00:00:00Z",
    )
    started = build_outcome_event(routing, event_type="execution_started", outcome="unknown")
    return ExplainEvidence(routing, started), routing


def _evidence_store(tmp_path: Path, rows: int = 3) -> tuple[DurableEvidenceStore, list[str], Path]:
    path = tmp_path / "receipts.db"
    store = DurableEvidenceStore(str(path))
    keys = [store.put(_evidence(f"req-{n}")[0], scope=_SCOPE) for n in range(rows)]
    return store, keys, path


@pytest.fixture
def walks(monkeypatch: pytest.MonkeyPatch) -> list[str | None]:
    """Record the scope of every full chain walk (``ReceiptStore._iter_chain_records``)."""
    seen: list[str | None] = []
    original = ReceiptStore._iter_chain_records

    def counting(self, *, scope, include_tombstones, page_size=5_000):  # type: ignore[no-untyped-def]
        seen.append(scope)
        return original(
            self, scope=scope, include_tombstones=include_tombstones, page_size=page_size
        )

    monkeypatch.setattr(ReceiptStore, "_iter_chain_records", counting)
    return seen


def test_current_makes_exactly_one_chain_walk(tmp_path: Path, walks: list[str | None]) -> None:
    store, keys, _ = _evidence_store(tmp_path)
    walks.clear()
    found = store.find(evidence_id=keys[1], scope=_SCOPE)  # one _current call
    assert found is not None and found.evidence_id == keys[1]
    assert walks == [_SCOPE]


def test_append_event_walks_once_per_current_call(tmp_path: Path, walks: list[str | None]) -> None:
    store, keys, _ = _evidence_store(tmp_path)
    _, routing = _evidence("req-1")
    event = build_outcome_event(routing, event_type="attempt_completed", outcome="unknown")
    walks.clear()
    updated = store.append_event(keys[1], event, scope=_SCOPE)  # _current, append, _current
    assert updated is not None and len(updated.events) == 2
    assert walks == [_SCOPE, _SCOPE]


def test_find_by_selector_and_missing_key_walk_once(
    tmp_path: Path, walks: list[str | None]
) -> None:
    store, _, _ = _evidence_store(tmp_path)
    walks.clear()
    assert store.find(evidence_id="evidence-missing", scope=_SCOPE) is None
    assert walks == [_SCOPE]


def test_the_public_readers_still_verify_every_call(
    tmp_path: Path, walks: list[str | None]
) -> None:
    """Other callers of the two readers keep verifying exactly as before."""
    store, keys, _ = _evidence_store(tmp_path)
    receipts = store.receipts
    walks.clear()
    assert receipts.get_receipt(keys[0], scope=_SCOPE) is not None
    assert receipts.query_receipts(scope=_SCOPE) != []
    assert receipts.query_receipts(scope=_SCOPE) != []
    assert walks == [_SCOPE, _SCOPE, _SCOPE]


def test_verified_reads_walk_once_and_are_bound_to_their_scope(
    tmp_path: Path, walks: list[str | None]
) -> None:
    store, keys, _ = _evidence_store(tmp_path)
    receipts = store.receipts
    receipts.put_receipt("decision", "other-scope", {"x": 1})
    walks.clear()
    with receipts.verified_reads(_SCOPE) as reads:
        assert reads.get_receipt(keys[0], include_tombstones=True) is not None
        assert reads.query_receipts(limit=10) != []
        assert reads.query_receipts(parent_receipt_id=keys[0], include_tombstones=True) == []
        # The reads are the scope's own: another scope's rows are not visible through them.
        assert reads.get_receipt("rcpt-other", include_tombstones=True) is None
    assert walks == [_SCOPE]
    with pytest.raises(ValueError), receipts.verified_reads(_SCOPE) as reads:
        reads.query_receipts(limit=0)


def test_verified_reads_cannot_be_kept_and_used_after_their_call(tmp_path: Path) -> None:
    store, keys, _ = _evidence_store(tmp_path)
    with store.receipts.verified_reads(_SCOPE) as reads:
        pass
    with pytest.raises(RuntimeError):
        reads.get_receipt(keys[0])
    with pytest.raises(RuntimeError):
        reads.query_receipts()


def test_public_readers_take_no_skip_argument_and_always_walk(
    tmp_path: Path, walks: list[str | None]
) -> None:
    store, keys, _ = _evidence_store(tmp_path)
    receipts = store.receipts
    with receipts.verified_reads(_SCOPE):
        walks.clear()
        receipts.get_receipt(keys[0], scope=_SCOPE)
        receipts.query_receipts(scope=_SCOPE)
        assert walks == [_SCOPE, _SCOPE]  # even inside a verified block
    with pytest.raises(TypeError):
        receipts.get_receipt(keys[0], scope=_SCOPE, verified=True)  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        receipts.query_receipts(scope=_SCOPE, verified=True)  # type: ignore[call-arg]


@pytest.mark.parametrize("victim", [0, 1, 2], ids=["first", "middle", "last"])
@pytest.mark.parametrize(
    "damage",
    [
        "payload_json = replace(payload_json, 'primary/model', 'evil/model')",
        "previous_hash = 'deadbeef'",
        "sensitivity = 'public'",
    ],
    ids=["payload", "chain_link", "metadata"],
)
def test_tamper_anywhere_in_scope_still_fails_every_read(
    tmp_path: Path, victim: int, damage: str
) -> None:
    """A tampered row in the scope fails get, query, find and append, as on main."""
    store, keys, path = _evidence_store(tmp_path)
    _, routing = _evidence("req-x")
    event = build_outcome_event(routing, event_type="attempt_completed", outcome="unknown")
    with sqlite3.connect(path) as conn:
        conn.execute(f"UPDATE receipts SET {damage} WHERE receipt_id = ?", (keys[victim],))
        conn.commit()
    other = keys[(victim + 1) % len(keys)]  # the read need not touch the damaged row
    with pytest.raises(ReceiptIntegrityError):
        store.find(evidence_id=other, scope=_SCOPE)
    with pytest.raises(ReceiptIntegrityError):
        store.find(evidence_id="evidence-missing", scope=_SCOPE)
    with pytest.raises(ReceiptIntegrityError):
        store.find(request_id="req-0", scope=_SCOPE)
    with pytest.raises(ReceiptIntegrityError):
        store.append_event(other, event, scope=_SCOPE)
    with pytest.raises(ReceiptIntegrityError):
        store.receipts.get_receipt(other, scope=_SCOPE)
    with pytest.raises(ReceiptIntegrityError):
        store.receipts.query_receipts(scope=_SCOPE)
    # The tampered scope never becomes readable: nothing was appended to it.
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == len(keys)


def test_verified_reads_refuse_a_tampered_scope_before_any_read(tmp_path: Path) -> None:
    store, keys, path = _evidence_store(tmp_path)
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE receipts SET sensitivity = 'public' WHERE receipt_id = ?", (keys[1],))
        conn.commit()
    entered = False
    with pytest.raises(ReceiptIntegrityError), store.receipts.verified_reads(_SCOPE):
        entered = True
    assert entered is False


def test_a_later_tamper_is_caught_by_the_next_call(tmp_path: Path) -> None:
    """No cache outlives its call: a row damaged after a good read fails the next."""
    store, keys, path = _evidence_store(tmp_path)
    assert store.find(evidence_id=keys[0], scope=_SCOPE) is not None
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE receipts SET sensitivity = 'public' WHERE receipt_id = ?", (keys[2],))
        conn.commit()
    with pytest.raises(ReceiptIntegrityError):
        store.find(evidence_id=keys[0], scope=_SCOPE)
