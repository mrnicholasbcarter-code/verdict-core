"""Tests for the typed evidence chain link and the receipt chain it rides on.

The receipt store already detected tampering before this contract existed: it
hashes whatever payload it is handed.  What it could not detect was a field that
was never written down.  These tests therefore cover both halves — that a link
missing or malforming any of its recorded facts is rejected at construction, and
that a link which was accepted stays tamper-evident once stored.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

import pytest

from verdict.contracts import (
    ContractValidationError,
    EvidenceChainLink,
    contract_from_dict,
)
from verdict.receipt_store import ReceiptStore

DIGEST = "sha256:" + "a" * 64
OTHER_DIGEST = "sha256:" + "b" * 64

VALID_LINK: dict[str, Any] = {
    "decision": "route:accept",
    "policy": "hard-policy-v3",
    "envelope_hash": DIGEST,
    "runtime": "local",
    "provider": "anthropic",
    "model": "claude-opus-5",
    "tools": ["pytest", "ruff"],
    "changes": ["verdict/contracts.py"],
    "verification": [
        {"check_name": "focused", "check_type": "focused_tests", "status": "passed"}
    ],
    "outcome": "success",
    "timestamp": "2026-08-16T14:40:00Z",
    "previous_hash": "",
}

IDENTITY_FIELDS = [
    "decision",
    "policy",
    "envelope_hash",
    "runtime",
    "provider",
    "model",
    "outcome",
    "timestamp",
]


def test_valid_link_round_trips_through_to_dict() -> None:
    link = EvidenceChainLink.from_dict(VALID_LINK)
    assert EvidenceChainLink.from_dict(link.to_dict()).to_dict() == link.to_dict()


def test_link_is_resolvable_under_both_registry_spellings() -> None:
    for name in ("evidence_chain_link", "EvidenceChainLink"):
        assert contract_from_dict(name, VALID_LINK).model == "claude-opus-5"


@pytest.mark.parametrize("missing", IDENTITY_FIELDS)
def test_link_rejects_a_missing_identity_field(missing: str) -> None:
    payload = {key: value for key, value in VALID_LINK.items() if key != missing}
    with pytest.raises(ContractValidationError, match=missing):
        EvidenceChainLink.from_dict(payload)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("decision", "   "),
        ("policy", ""),
        ("runtime", " "),
        ("envelope_hash", "not-a-digest"),
        ("envelope_hash", "sha256:" + "A" * 64),
        ("previous_hash", "sha256:short"),
        ("outcome", "mostly-fine"),
        ("timestamp", "yesterday"),
        ("timestamp", "2026-13-99"),
        ("tools", ["ruff", ""]),
        ("changes", [""]),
        ("changes", [123]),
        ("verification", ["not-an-object"]),
        ("verification", [{"check_name": "x", "check_type": "bogus", "status": "passed"}]),
        ("verification", [{"check_name": "x", "check_type": "ci", "status": "maybe"}]),
    ],
)
def test_link_rejects_malformed_values(field: str, value: Any) -> None:
    with pytest.raises(ContractValidationError):
        EvidenceChainLink.from_dict({**VALID_LINK, field: value})


def test_genesis_link_may_omit_previous_hash_but_later_links_may_not_forge_one() -> None:
    genesis = EvidenceChainLink.from_dict({**VALID_LINK, "previous_hash": ""})
    assert genesis.previous_hash == ""
    linked = EvidenceChainLink.from_dict({**VALID_LINK, "previous_hash": OTHER_DIGEST})
    assert linked.previous_hash == OTHER_DIGEST
    with pytest.raises(ContractValidationError, match="previous_hash"):
        EvidenceChainLink.from_dict({**VALID_LINK, "previous_hash": "0" * 64})


def test_link_rejects_unknown_and_secret_bearing_fields() -> None:
    with pytest.raises(ContractValidationError):
        EvidenceChainLink.from_dict({**VALID_LINK, "operator_note": "hi"})
    with pytest.raises(ContractValidationError):
        EvidenceChainLink.from_dict({**VALID_LINK, "api_key": "sk-live-123"})


def _store_links(store: ReceiptStore, scope: str, count: int = 3) -> list[str]:
    ids = []
    for index in range(count):
        link = EvidenceChainLink.from_dict(
            {**VALID_LINK, "decision": f"route:accept:{index}"}
        )
        record = store.put_receipt(
            receipt_type="decision", scope=scope, payload=link.to_dict()
        )
        ids.append(record.receipt_id)
    return ids


def test_stored_chain_verifies_clean(tmp_path) -> None:
    store = ReceiptStore(tmp_path / "receipts.db")
    _store_links(store, "evidence")
    report = store.verify_integrity(scope="evidence")
    assert report["valid"] is True
    assert report["checked"] == 3


@pytest.mark.parametrize("field", sorted(VALID_LINK))
def test_mutating_any_recorded_field_breaks_the_chain(tmp_path, field: str) -> None:
    """Every field the link records must be covered by the tamper evidence.

    A field that can be edited after the fact without the chain noticing is a
    field the chain does not actually attest to, which is the failure this
    contract exists to prevent.
    """
    store = ReceiptStore(tmp_path / "receipts.db")
    receipt_ids = _store_links(store, "evidence")
    assert store.verify_integrity(scope="evidence")["valid"] is True

    target = receipt_ids[1]
    conn = sqlite3.connect(store.db_path)
    try:
        row = conn.execute(
            "SELECT payload_json FROM receipts WHERE receipt_id = ?", (target,)
        ).fetchone()
        payload = json.loads(row[0])
        original = payload[field]
        if field in ("tools", "changes", "verification"):
            payload[field] = []
        elif field == "previous_hash" or field == "envelope_hash":
            payload[field] = OTHER_DIGEST
        else:
            payload[field] = f"tampered-{original}"
        conn.execute(
            "UPDATE receipts SET payload_json = ? WHERE receipt_id = ?",
            (json.dumps(payload, sort_keys=True, separators=(",", ":")), target),
        )
        conn.commit()
    finally:
        conn.close()

    report = store.verify_integrity(scope="evidence")
    assert report["valid"] is False
    assert f"{target}:content_hash" in report["errors"]


def test_reordering_the_chain_is_detected(tmp_path) -> None:
    store = ReceiptStore(tmp_path / "receipts.db")
    receipt_ids = _store_links(store, "evidence")
    conn = sqlite3.connect(store.db_path)
    try:
        conn.execute(
            "UPDATE receipts SET sequence = ? WHERE receipt_id = ?", (99, receipt_ids[1])
        )
        conn.commit()
    finally:
        conn.close()
    report = store.verify_integrity(scope="evidence")
    assert report["valid"] is False


def test_export_import_round_trip_preserves_verification(tmp_path) -> None:
    source = ReceiptStore(tmp_path / "source.db")
    _store_links(source, "evidence")
    bundle = source.export(scope="evidence")

    target = ReceiptStore(tmp_path / "target.db")
    imported = target.import_bundle(bundle, scope="evidence")
    assert imported == 3

    report = target.verify_integrity(scope="evidence")
    assert report["valid"] is True
    assert report["checked"] == 3

    restored = sorted(
        target.query_receipts(scope="evidence"), key=lambda item: item.sequence
    )
    for record in restored:
        # The link must survive transport as a link, not as loose JSON.
        assert EvidenceChainLink.from_dict(record.payload).verification == (
            VALID_LINK["verification"]
        )


def test_chain_walk_spans_more_than_one_page(tmp_path) -> None:
    """Chain verification must follow a scope forward past any page boundary.

    Verification used to read the newest N receipts in one capped query, so the
    oldest row in that window appeared to follow nothing and a healthy store
    reported a broken link.  Because the read path gates on this result, that
    false positive locked the store.  Paging with a deliberately tiny page size
    reproduces the boundary without needing a store large enough to hit the old
    cap.
    """
    store = ReceiptStore(tmp_path / "receipts.db")
    _store_links(store, "evidence", count=25)

    walked = list(
        store._iter_chain_records(scope="evidence", include_tombstones=True, page_size=4)
    )
    assert [record.sequence for record in walked] == list(range(1, 26))
    assert store.verify_integrity(scope="evidence")["valid"] is True


def test_chain_walk_covers_every_scope_when_unscoped(tmp_path) -> None:
    store = ReceiptStore(tmp_path / "receipts.db")
    _store_links(store, "alpha", count=2)
    _store_links(store, "beta", count=2)
    report = store.verify_integrity()
    assert report["valid"] is True
    assert report["checked"] == 4
