from __future__ import annotations

import pytest

from verdict.provider_receipts import ProviderReceipt, build_provider_receipt


def test_provider_receipt_is_deterministic_and_hashes_inputs() -> None:
    receipt = build_provider_receipt(
        run_id="run-1",
        provider="risk",
        provider_version="1.0.0",
        inputs={"value": 1, "name": "sample"},
        config={"threshold": 0.5},
        outcome="approved",
        provenance={"source": "fixture", "authority": "observed"},
        evidence_refs=("evidence-1",),
        details={"approved": True},
    )
    same = build_provider_receipt(
        run_id="run-1",
        provider="risk",
        provider_version="1.0.0",
        inputs={"name": "sample", "value": 1},
        config={"threshold": 0.5},
        outcome="approved",
        provenance={"source": "fixture", "authority": "observed"},
        evidence_refs=("evidence-1",),
        details={"approved": True},
    )

    assert receipt.inputs_hash == same.inputs_hash
    assert receipt.config_hash == same.config_hash
    assert ProviderReceipt.from_dict(receipt.to_dict()) == receipt
    assert receipt.to_dict()["inputs_hash"].startswith("sha256:")


def test_provider_receipt_rejects_sensitive_metadata() -> None:
    with pytest.raises(ValueError, match="sensitive"):
        build_provider_receipt(
            run_id="run-1",
            provider="risk",
            provider_version="1.0.0",
            inputs={},
            config={},
            outcome="unknown",
            provenance={"api_key": "must-not-persist"},
        )


def test_provider_receipt_rejects_unknown_fields() -> None:
    with pytest.raises(ValueError, match="unknown"):
        ProviderReceipt.from_dict(
            {
                "schema_version": "1",
                "run_id": "run-1",
                "provider": "risk",
                "provider_version": "1.0.0",
                "inputs_hash": "sha256:" + "a" * 64,
                "config_hash": "sha256:" + "b" * 64,
                "outcome": "unknown",
                "provenance": {},
                "evidence_refs": [],
                "details": None,
                "unexpected": True,
            }
        )
