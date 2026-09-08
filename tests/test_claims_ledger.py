"""Lifecycle, freshness, privacy, and hydration tests for E2-S5."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from verdict.claims_ledger import Claim, ClaimsLedger, ClaimTransitionError, FreshnessPolicy
from verdict.proof_receipts import EvidenceReference, SourceReference, claim_hash
from verdict.provider_receipts import canonical_hash
from verdict.receipt_store import ReceiptStore

NOW = datetime(2026, 9, 8, 7, 0, tzinfo=timezone.utc)
SOURCE = SourceReference("source-fixture", canonical_hash({"source": "fixture"}))
EVIDENCE = EvidenceReference("evidence-fixture", canonical_hash({"evidence": "fixture"}))
POLICIES = {"fixture": FreshnessPolicy("fixture", ttl_seconds=60, clock_skew_seconds=5)}


def _claim(
    text: str,
    *,
    claim_id: str,
    status: str = "active",
    observed_at: datetime = NOW,
    verified_at: datetime | None = NOW,
    confidence: float = 0.8,
    supersedes: str | None = None,
) -> Claim:
    return Claim.create(
        claim_id=claim_id,
        subject="route",
        text=text,
        status=status,  # type: ignore[arg-type]
        observed_at=observed_at,
        verified_at=verified_at,
        confidence=confidence,
        source_type="fixture",
        source_refs=(SOURCE,),
        evidence_refs=(EVIDENCE,),
        supersedes=supersedes,
    )


def test_claim_identity_hash_and_serialization_are_privacy_safe() -> None:
    claim = _claim("private route assertion", claim_id="claim-private")
    payload = claim.to_dict()
    assert payload["claim_id"] == "claim-private"
    assert payload["text_hash"] == claim.text_hash
    assert "private route assertion" not in json.dumps(payload)
    assert "text" not in payload


def test_active_selection_excludes_superseded_and_preserves_history() -> None:
    ledger = ClaimsLedger(POLICIES)
    old = _claim("route-old", claim_id="claim-old", confidence=0.99)
    current = _claim(
        "route-current", claim_id="claim-current", confidence=0.5, supersedes=old.claim_id
    )
    ledger.add(old)
    ledger.add(current)

    selected = ledger.select_active(subject="route", now=NOW)
    assert [item.claim_id for item in selected] == ["claim-current"]
    assert ledger.get("claim-old").status == "superseded"
    assert [item.claim_id for item in ledger.history(subject="route")] == [
        "claim-current",
        "claim-old",
    ]
    assert ledger.transitions("claim-old")[0].to_status == "superseded"


def test_disputed_claim_is_surfaced_but_cannot_satisfy_required_fact() -> None:
    ledger = ClaimsLedger(POLICIES)
    disputed = _claim(
        "unsafe route", claim_id="claim-disputed", status="disputed", verified_at=None
    )
    ledger.add(disputed)

    result = ledger.hydrate(required_facts=("unsafe route",), now=NOW)
    assert result.blocked is True
    assert result.units == ()
    assert result.omissions[0].status == "disputed"
    assert result.omissions[0].claim_id == disputed.claim_id


def test_exact_expiry_and_clock_skew_are_fail_closed() -> None:
    claim = _claim("fresh route", claim_id="claim-fresh")
    policy = POLICIES["fixture"]
    assert policy.evaluate(claim.observed_at, now=NOW) == "fresh"
    assert policy.evaluate(claim.observed_at, now=NOW.replace(second=59)) == "fresh"
    assert policy.evaluate(claim.observed_at, now=NOW.replace(second=0, minute=1)) == "expired"
    assert policy.evaluate(claim.observed_at, now=NOW - timedelta(seconds=6)) == "future_skew"
    assert policy.evaluate(claim.observed_at, now=NOW - timedelta(seconds=4)) == "fresh"


def test_status_transitions_are_audited_in_receipt_store() -> None:
    store = ReceiptStore(":memory:", strict_scope=True)
    ledger = ClaimsLedger(POLICIES, receipt_store=store, scope="project-1")
    claim = _claim("audited route", claim_id="claim-audited")
    ledger.add(claim)
    ledger.transition(claim.claim_id, "disputed", reason="evidence conflict", occurred_at=NOW)

    records = store.query_receipts(scope="project-1", limit=10)
    assert any(record.payload.get("kind") == "claim" for record in records)
    transition_records = [
        record for record in records if record.event_type == "claim_status_transition"
    ]
    assert transition_records
    assert transition_records[0].payload["transition"]["reason"] == claim_hash("evidence conflict")
    assert store.verify_integrity(scope="project-1")["valid"]


def test_failed_transition_persistence_does_not_mutate_memory() -> None:
    ledger = ClaimsLedger(POLICIES)
    claim = _claim("rollback route", claim_id="claim-rollback")
    ledger.add(claim)

    def fail(_transition) -> None:
        raise RuntimeError("receipt unavailable")

    ledger._persist_transition = fail  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="receipt unavailable"):
        ledger.transition(claim.claim_id, "disputed", reason="receipt_unavailable")
    assert ledger.get(claim.claim_id).status == "active"
    assert ledger.transitions(claim.claim_id) == ()


def test_receipt_store_reload_preserves_current_and_historical_state() -> None:
    store = ReceiptStore(":memory:", strict_scope=True)
    ledger = ClaimsLedger(POLICIES, receipt_store=store, scope="project-2")
    old = _claim("old route", claim_id="claim-reload-old")
    ledger.add(old)
    ledger.add(_claim("new route", claim_id="claim-reload-new", supersedes=old.claim_id))

    restored = ClaimsLedger.from_receipt_store(
        store, policies=POLICIES, scope="project-2", clock=lambda: NOW
    )
    assert restored.select_active(subject="route", now=NOW)[0].claim_id == "claim-reload-new"
    assert restored.get("claim-reload-old").status == "superseded"
    assert len(restored.history(subject="route")) == 2
    assert restored.transitions("claim-reload-old")


def test_artifact_round_trip_keeps_hashes_and_transitions_without_text() -> None:
    ledger = ClaimsLedger(POLICIES)
    claim = _claim("round trip route", claim_id="claim-round-trip")
    ledger.add(claim)
    ledger.transition(claim.claim_id, "disputed", reason="review pending")
    restored = ClaimsLedger.from_dict(
        ledger.to_dict(), claim_texts={claim.claim_id: claim.text or ""}
    )

    assert restored.to_dict() == ledger.to_dict()
    assert restored.get(claim.claim_id).text == claim.text


def test_invalid_transition_and_missing_freshness_policy_fail_closed() -> None:
    ledger = ClaimsLedger(POLICIES)
    claim = _claim("route", claim_id="claim-transition")
    ledger.add(claim)
    ledger.transition(claim.claim_id, "superseded", reason="replaced")
    with pytest.raises(ClaimTransitionError):
        ledger.transition(claim.claim_id, "active", reason="cannot resurrect")

    no_policy = ClaimsLedger({"other": FreshnessPolicy("other", 10)})
    no_policy.add(claim)
    assert no_policy.freshness(claim.claim_id, now=NOW).status == "policy_missing"
    assert no_policy.select_active(subject="route", now=NOW) == ()


def test_claim_schema_fixture_is_strict() -> None:
    schema_path = Path(__file__).parents[1] / "verdict/schemas/claims-ledger.v1.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    fixture = json.loads(
        (Path(__file__).parent / "fixtures/claims_ledger/active_superseded.json").read_text(
            encoding="utf-8"
        )
    )
    assert not list(Draft202012Validator(schema).iter_errors(fixture))
    restored = ClaimsLedger.from_dict(
        fixture,
        claim_texts={"claim-old": "route-old", "claim-current": "route-current"},
        clock=lambda: datetime(2026, 9, 8, 7, 2, 30, tzinfo=timezone.utc),
    )
    assert restored.select_active(
        subject="route", now=datetime(2026, 9, 8, 7, 2, 30, tzinfo=timezone.utc)
    )[0].claim_id == "claim-current"

    disputed = json.loads(
        (Path(__file__).parent / "fixtures/claims_ledger/disputed.json").read_text(encoding="utf-8")
    )
    disputed_ledger = ClaimsLedger.from_dict(
        disputed,
        claim_texts={"claim-disputed": "unsafe route"},
        clock=lambda: datetime(2026, 9, 8, 7, 0, 30, tzinfo=timezone.utc),
    )
    assert disputed_ledger.hydrate(
        required_facts=("unsafe route",),
        now=datetime(2026, 9, 8, 7, 0, 30, tzinfo=timezone.utc),
    ).blocked
