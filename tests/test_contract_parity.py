"""Shared Python/TypeScript contract parity fixtures for the contract-parity effort."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from verdict.contracts import ExecutionEnvelope, RoutingDecisionContract
from verdict.proof_receipts import ProofReceipt, ProofReceiptError
from verdict.provider_receipts import ProviderReceipt

FIXTURE_DIR = Path(__file__).parents[1] / "test_fixtures" / "parity"


def _load(name: str) -> dict[str, object]:
    payload = json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


def test_valid_shared_fixtures_round_trip_with_python_contracts() -> None:
    routing_valid = _load("routing_decision_valid.json")
    routing_defaults = _load("routing_decision_defaults.json")
    routing_minimal = _load("routing_decision_minimal.json")
    envelope = _load("envelope_explicit.json")
    provider = _load("provider_receipt_valid.json")
    proof_valid = _load("proof_receipt_valid.json")
    proof_denial = _load("proof_receipt_denial.json")

    assert RoutingDecisionContract.from_dict(routing_valid).to_dict() == routing_valid
    assert RoutingDecisionContract.from_dict(routing_defaults).to_dict() == routing_defaults
    assert RoutingDecisionContract.from_dict(routing_minimal).to_dict() == routing_defaults
    assert ExecutionEnvelope.from_dict(envelope).to_dict() == envelope
    assert ProviderReceipt.from_dict(provider).to_dict() == provider
    assert ProofReceipt.from_dict(proof_valid).to_dict() == proof_valid
    assert ProofReceipt.from_dict(proof_denial).to_dict() == proof_denial


def test_shared_fixtures_enforce_strict_unknown_and_missing_field_policy() -> None:
    with pytest.raises(ValueError, match="missing field"):
        ProviderReceipt.from_dict(_load("provider_receipt_missing_details.json"))
    with pytest.raises(ValueError, match="unknown field"):
        ProviderReceipt.from_dict(_load("provider_receipt_unknown_field.json"))
    with pytest.raises(ProofReceiptError, match="unknown field"):
        ProofReceipt.from_dict(_load("proof_receipt_unknown_field.json"))


def test_shared_routing_fixture_defaults_are_explicit_and_stable() -> None:
    normalized = RoutingDecisionContract.from_dict(_load("routing_decision_minimal.json")).to_dict()
    expected = _load("routing_decision_defaults.json")

    assert normalized == expected
    assert normalized["candidate_snapshot"] is None
    assert normalized["correlation_id"] is None
    assert normalized["request_id"] is None
    assert normalized["receipt"] is None
