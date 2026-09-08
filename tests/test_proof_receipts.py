"""Focused acceptance tests for E2-S4 proof receipts and independent checks."""

from __future__ import annotations

import copy
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from verdict.proof_receipts import (
    ClaimRecord,
    DropReason,
    EvidenceReference,
    ProofReceipt,
    ProofReceiptError,
    SourceReference,
    build_proof_receipt,
    build_receipt_manifest,
    claim_hash,
)
from verdict.provider_receipts import canonical_hash
from verdict.receipt_store import ReceiptStore
from verdict.receipt_verifier import verify_serialized_manifest, verify_serialized_receipt

NOW = datetime(2026, 9, 8, 7, 0, tzinfo=timezone.utc)
SCHEMA = json.loads(
    (Path(__file__).parents[1] / "verdict/schemas/proof-receipt.v1.json").read_text()
)


def _receipt() -> ProofReceipt:
    evidence = EvidenceReference("evidence-1", canonical_hash({"probe": "ok"}))
    return ProofReceipt(
        task_id="task-1",
        request_id="request-1",
        policy_version="policy-1",
        input_hash=canonical_hash({"shape": "chat", "size": 4}),
        context_hash=canonical_hash({"context": "fixture"}),
        eligible_candidates=("free/model-a", "free/model-b"),
        selected_route="free/model-a",
        drop_reasons=(DropReason("free/model-b", "higher_cost", "evidence-1"),),
        source_references=(SourceReference("catalog-1", canonical_hash({"catalog": 1})),),
        evidence=(evidence,),
        created_at=NOW,
        decision_at=NOW,
        receipt_id="receipt-1",
    )


def test_round_trip_and_manifest_verification() -> None:
    receipt = _receipt()
    restored = ProofReceipt.from_dict(json.loads(receipt.serialize()))
    assert restored == receipt
    assert verify_serialized_receipt(receipt.serialize()).valid
    assert not list(Draft202012Validator(SCHEMA).iter_errors(receipt.to_dict()))

    manifest = build_receipt_manifest([receipt])
    assert verify_serialized_manifest(manifest).valid


def test_independent_verifier_smoke_in_clean_process() -> None:
    verifier_path = Path(__file__).parents[1] / "verdict" / "receipt_verifier.py"
    script = (
        "import importlib.util,sys;"
        f"s=importlib.util.spec_from_file_location('independent',{str(verifier_path)!r});"
        "m=importlib.util.module_from_spec(s);"
        "sys.modules['independent']=m;"
        "s.loader.exec_module(m);"
        "print(m.verify_serialized_receipt(sys.stdin.buffer.read()).valid)"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-c", script],
        input=_receipt().serialize().encode(),
        capture_output=True,
        check=True,
    )
    assert result.stdout.strip() == b"True"


@pytest.mark.parametrize(
    "path",
    [
        ("receipt_id",),
        ("task_id",),
        ("request_id",),
        ("policy_version",),
        ("input_hash",),
        ("context_hash",),
        ("eligible_candidates", 0),
        ("selected_route",),
        ("drop_reasons", 0, "code"),
        ("source_references", 0, "digest"),
        ("evidence", 0, "digest"),
        ("timestamps", "decision_at"),
        ("claims",),
        ("integrity", "receipt_digest"),
    ],
)
def test_tampering_any_protected_field_fails_closed(path: tuple[object, ...]) -> None:
    payload = copy.deepcopy(_receipt().to_dict())
    current: object = payload
    for component in path[:-1]:
        current = current[component]  # type: ignore[index]
    key = path[-1]
    if isinstance(current, list):
        current[key] = "tampered"  # type: ignore[index]
    elif key == "receipt_digest":
        current[key] = canonical_hash({"tampered": True})  # type: ignore[index]
    else:
        current[key] = "tampered"  # type: ignore[index]
    assert not verify_serialized_receipt(payload).valid


def test_raw_prompt_secrets_and_personal_data_never_enter_receipt() -> None:
    with pytest.raises(ProofReceiptError):
        ProofReceipt(
            **{
                **_receipt().__dict__,
                "request_id": "raw_prompt",
            }
        )
    serialized = _receipt().serialize()
    assert "raw_prompt" not in serialized
    assert "api_key" not in serialized
    assert "alice@example.com" not in serialized
    hashed = _receipt()
    built = build_proof_receipt(
        task_id=hashed.task_id,
        request_id=hashed.request_id,
        policy_version=hashed.policy_version,
        input={"prompt": "secret"},
        context_hash=hashed.context_hash,
        eligible_candidates=hashed.eligible_candidates,
        selected_route=hashed.selected_route,
        drop_reasons=hashed.drop_reasons,
        source_references=hashed.source_references,
        evidence=hashed.evidence,
        created_at=NOW,
        decision_at=NOW,
        receipt_id="receipt-hashed",
    )
    assert "secret" not in built.serialize()


@pytest.mark.parametrize("status", ["missing", "malformed", "skipped", "unavailable"])
def test_missing_or_unavailable_evidence_never_passes(status: str) -> None:
    payload = _receipt().to_dict()
    payload["evidence"][0]["status"] = status
    assert not verify_serialized_receipt(payload).valid


def test_claims_reference_evidence_and_preserve_superseded_history() -> None:
    evidence = EvidenceReference("evidence-1", canonical_hash({"probe": "ok"}))
    old = ClaimRecord(
        "claim-old",
        "superseded",
        claim_hash("old route claim"),
        evidence_refs=("evidence-1",),
    )
    current = ClaimRecord(
        "claim-current",
        "active",
        claim_hash("current route claim"),
        evidence_refs=("evidence-1",),
        supersedes="claim-old",
    )
    receipt = ProofReceipt(
        **{
            **_receipt().__dict__,
            "evidence": (evidence,),
            "claims": (old, current),
        }
    )
    assert [claim["claim_id"] for claim in receipt.to_dict()["claims"]] == [
        "claim-old",
        "claim-current",
    ]
    assert verify_serialized_receipt(receipt.to_dict()).valid


def test_claims_cannot_reference_missing_evidence_or_future_history() -> None:
    evidence = EvidenceReference("evidence-1", canonical_hash({"probe": "ok"}))
    with pytest.raises(ProofReceiptError, match="unavailable evidence"):
        ProofReceipt(
            **{
                **_receipt().__dict__,
                "evidence": (evidence,),
                "claims": (
                    ClaimRecord(
                        "claim-current",
                        "active",
                        claim_hash("claim"),
                        evidence_refs=("missing-evidence",),
                    ),
                ),
            }
        )

    with pytest.raises(ProofReceiptError, match="unavailable claim"):
        ProofReceipt(
            **{
                **_receipt().__dict__,
                "evidence": (evidence,),
                "claims": (
                    ClaimRecord(
                        "claim-current",
                        "active",
                        claim_hash("claim"),
                        evidence_refs=("evidence-1",),
                        supersedes="claim-future",
                    ),
                ),
            }
        )


def test_schema_and_verifier_both_reject_incomplete_denial() -> None:
    payload = _receipt().to_dict()
    payload["selected_route"] = None
    payload["drop_reasons"] = []
    payload["integrity"]["receipt_digest"] = canonical_hash(
        {key: value for key, value in payload.items() if key != "integrity"}
    )
    assert list(Draft202012Validator(SCHEMA).iter_errors(payload))
    assert not verify_serialized_receipt(payload).valid


def test_receipt_store_uses_append_only_integrity_boundary() -> None:
    store = ReceiptStore(":memory:", strict_scope=True)
    record = store.put_proof_receipt(_receipt(), scope="project-1")
    assert record.receipt_id == "receipt-1"
    assert store.verify_integrity(scope="project-1")["valid"]
