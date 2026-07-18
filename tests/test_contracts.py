"""Contract compatibility and fixture tests for issue #21."""

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from llm_gate.contracts import (
    AvailabilitySnapshot,
    ContractValidationError,
    RoutingDecisionContract,
    RuntimeCandidate,
    TaskSpec,
    WorkflowPlan,
    contract_from_dict,
    contract_from_legacy_dict,
    redact_contract_secrets,
)

FIXTURE_DIR = Path(__file__).parent / "fixtures"
SCHEMA = json.loads((Path(__file__).parents[1] / "schemas" / "contracts.v1.json").read_text())


def test_fixture_is_valid_against_versioned_schema() -> None:
    payload = json.loads((FIXTURE_DIR / "contract-v1.json").read_text())
    errors = list(Draft202012Validator(SCHEMA).iter_errors(payload))
    assert errors == []


def test_all_contracts_round_trip_fixture() -> None:
    payload = json.loads((FIXTURE_DIR / "contract-v1.json").read_text())
    for name, value in payload.items():
        restored = contract_from_dict(name, value)
        assert restored.to_dict() == value


def test_unknown_fields_are_rejected() -> None:
    payload = {"objective": "ship", "task_type": "coding", "compatibility": {"future": True}}
    with pytest.raises(ContractValidationError, match="unknown field"):
        TaskSpec.from_dict(payload)


def test_secret_bearing_fields_are_rejected() -> None:
    with pytest.raises(ContractValidationError, match="secret-bearing"):
        TaskSpec.from_dict({"objective": "ship", "context": {"api_key": "do-not-store"}})


def test_schema_version_defaults_to_v1_when_omitted() -> None:
    task = TaskSpec.from_dict({"objective": "ship", "task_type": "coding"})

    assert task.schema_version == "1"


def test_schema_version_mismatch_is_rejected() -> None:
    with pytest.raises(ContractValidationError, match=r"schema_version must be '1', got '2'"):
        TaskSpec.from_dict({"objective": "ship", "task_type": "coding", "schema_version": "2"})


def test_fixture_contracts_all_declare_schema_version_v1() -> None:
    payload = json.loads((FIXTURE_DIR / "contract-v1.json").read_text())

    for name, value in payload.items():
        assert value["schema_version"] == "1", name


def test_catalog_presence_is_distinct_from_live_eligibility() -> None:
    candidate = RuntimeCandidate.from_dict(
        {
            "runtime_id": "ollama/llama3",
            "catalog_present": True,
            "live_eligible": False,
            "availability": "unknown",
            "signals": {"catalog": {"source": "catalog", "freshness_seconds": 30}},
        }
    )
    assert candidate.catalog_present is True
    assert candidate.live_eligible is False
    assert candidate.signals["catalog"]["source"] == "catalog"


def test_existing_routing_decision_remains_importable() -> None:
    decision = RoutingDecisionContract.from_dict(
        {"selected_route": {"runtime_id": "x"}, "policy_floor": "protected"}
    )
    assert decision.selected_route["runtime_id"] == "x"


def test_nested_contracts_round_trip_to_objects_and_dicts() -> None:
    workflow = WorkflowPlan.from_dict(
        {
            "steps": [{"id": "plan", "action": "implement"}],
            "verification": {"checks": ["pytest -q"], "on_failure": "deny"},
        }
    )
    snapshot = AvailabilitySnapshot.from_dict(
        {
            "observed_at": "2026-07-16T12:00:00Z",
            "candidates": [
                {
                    "runtime_id": "demo/frontier-tools",
                    "catalog_present": True,
                    "live_eligible": True,
                    "availability": "healthy",
                    "signals": {"catalog": {"source": "fixture"}},
                }
            ],
        }
    )

    verification = workflow.verification
    assert not isinstance(verification, dict)
    assert verification.on_failure == "deny"
    assert workflow.to_dict()["verification"] == {"checks": ["pytest -q"], "on_failure": "deny", "schema_version": "1"}
    assert isinstance(snapshot.candidates[0], RuntimeCandidate)
    assert snapshot.to_dict()["candidates"][0]["runtime_id"] == "demo/frontier-tools"


def test_legacy_task_contract_migrates_to_task_spec() -> None:
    migrated = contract_from_legacy_dict(
        "task_spec",
        {
            "task": "Ship the contracts migration",
            "criticality": "high",
            "context": {"repo": "llm-gate"},
            "custom_hint": "preserve under metadata",
        },
    )

    assert isinstance(migrated, TaskSpec)
    assert migrated.objective == "Ship the contracts migration"
    assert migrated.criticality == "high"
    assert migrated.context == {"repo": "llm-gate"}
    assert migrated.metadata["legacy"] == {"custom_hint": "preserve under metadata"}


def test_legacy_routing_decision_migrates_tier_and_alternatives() -> None:
    migrated = contract_from_legacy_dict(
        "routing_decision",
        {
            "provider": "anthropic",
            "model": "claude-sonnet-4",
            "tier": 1,
            "reason": "protected route",
            "alternatives": ["openai/gpt-4o"],
            "request_id": "req-123",
        },
    )

    assert isinstance(migrated, RoutingDecisionContract)
    assert migrated.selected_route["runtime_id"] == "anthropic/claude-sonnet-4"
    assert migrated.policy_floor == "protected"
    assert migrated.exclusions == [{"model": "openai/gpt-4o", "reason": "legacy alternative"}]
    assert migrated.request_id == "req-123"


def test_redaction_scrubs_contract_dicts_without_rejecting_them() -> None:
    payload = {
        "metadata": {"api_key": "provider-secret"},
        "context": {
            "url": "https://user:password@example.com/path?api_key=provider-secret",
            "authorization": "Bearer caller-secret",
        },
    }

    redacted = redact_contract_secrets(payload)

    assert redacted["metadata"]["api_key"] == "[redacted]"
    assert redacted["context"]["authorization"] == "[redacted]"
    assert "provider-secret" not in redacted["context"]["url"]
    assert "password" not in redacted["context"]["url"]


def test_invalid_fixture_is_rejected_by_python_contract() -> None:
    payload = json.loads((FIXTURE_DIR / "invalid-unknown-field.json").read_text())
    with pytest.raises(ContractValidationError):
        TaskSpec.from_dict(payload)


def test_invalid_schema_version_fixture_is_rejected_by_schema_and_python_contract() -> None:
    payload = json.loads((FIXTURE_DIR / "invalid-schema-version.json").read_text())

    errors = list(Draft202012Validator(SCHEMA).iter_errors({"task_spec": payload}))
    assert any(error.json_path == "$.task_spec.schema_version" for error in errors)

    with pytest.raises(ContractValidationError, match=r"schema_version must be '1', got '2'"):
        TaskSpec.from_dict(payload)
