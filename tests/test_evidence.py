"""Deterministic, privacy-safe evidence tests for issue #53."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator  # type: ignore[import-untyped]
from referencing import Registry, Resource  # type: ignore[import-untyped]

from verdict.contracts import OutcomeEvent, RoutingDecisionContract
from verdict.evidence import (
    EvidenceStore,
    ExplainEvidence,
    build_outcome_event,
    build_routing_decision_contract,
)
from verdict.models import RoutingDecision

ROOT = Path(__file__).parents[1]
SCHEMAS = [
    json.loads((ROOT / "schemas" / "contracts.v1.json").read_text()),
    json.loads((ROOT / "verdict" / "schemas" / "contracts.v1.json").read_text()),
]
CASES = json.loads((ROOT / "tests" / "fixtures" / "evidence-cases.json").read_text())


def _decision() -> RoutingDecision:
    return RoutingDecision(
        model="primary/model",
        provider="omniroute",
        tier=1,
        reason="selected after eligibility gate",
        request_id="req-evidence-1",
        candidate_states=[
            {
                "model_id": "primary/model",
                "provider": "omniroute",
                "admitted": True,
                "verdict": "eligible",
                "state": "ready",
                "source": "fixture",
            },
            {
                "model_id": "backup/model",
                "provider": "backup",
                "admitted": False,
                "verdict": "runtime_truth_absent",
                "state": "unknown",
                "source": "fixture",
                "reason": "health unknown",
            },
        ],
    )


def test_decision_evidence_is_schema_valid_redacted_and_snapshot_based() -> None:
    prompt = "Deploy with api_key=sk-sensitive and completion=private-result"
    decision = _decision()
    evidence = build_routing_decision_contract(
        decision,
        task=prompt,
        criticality="high",
        features={
            "stream": True,
            "tools": True,
            "response_format": "json_schema",
            "tool_count": 1,
            "tool_names": ["lookup"],
        },
        correlation_id="workflow-evidence-1",
        occurred_at="2026-07-22T00:00:00Z",
    )
    payload = evidence.to_dict()
    for schema in SCHEMAS:
        errors = list(
            Draft202012Validator(schema["$defs"]["routing_decision"]).iter_errors(payload)
        )
        assert errors == []
    rendered = json.dumps(payload)
    assert prompt not in rendered
    assert "sk-sensitive" not in rendered
    assert "private-result" not in rendered
    assert payload["candidate_snapshot"]["captured_at"] == "2026-07-22T00:00:00Z"
    assert payload["exclusions"] == [
        {"model": "backup/model", "reason": "health unknown", "verdict": "runtime_truth_absent"}
    ]
    assert set(payload["candidate_snapshot"]["records"][0]) <= {
        "model_id",
        "provider",
        "admitted",
        "verdict",
        "state",
        "source",
    }

    # Mutating the source decision after creation cannot alter the immutable
    # decision-time evidence retained by the contract.
    decision.candidate_states[1]["reason"] = "mutable later cache state"
    assert payload["exclusions"][0]["reason"] == "health unknown"


def test_outcome_fixtures_cover_protocol_lifecycle_and_are_schema_valid() -> None:
    routing = build_routing_decision_contract(
        _decision(),
        task="fixture task",
        criticality="medium",
        features={},
        correlation_id="workflow-fixtures-1",
        occurred_at="2026-07-22T00:00:00Z",
    )

    for case in CASES:
        event_data: dict[str, Any] = case["event"]
        event = build_outcome_event(
            routing,
            event_type=event_data["event_type"],
            outcome=event_data["outcome"],
            event_id=event_data["event_id"],
            occurred_at=event_data["occurred_at"],
            status_code=event_data["status_code"],
            features=case["features"],
            streaming_phase=event_data.get("streaming_phase"),
            retries=event_data["retries"],
            fallbacks=event_data["fallbacks"],
            abort_observed=event_data.get("abort_observed", False),
        )
        payload = event.to_dict()
        for schema in SCHEMAS:
            errors = list(
                Draft202012Validator(schema["$defs"]["outcome_event"]).iter_errors(payload)
            )
            assert errors == [], case["name"]
        assert payload["request_id"] == "req-evidence-1"
        assert payload["correlation_id"] == "workflow-fixtures-1"
        assert payload["details"]["request_features"] == case["features"]
        if case["name"] == "retry-fallback":
            assert payload["retries"] == 1
            assert payload["fallbacks"][0]["runtime_id"] == "backup/model"


def test_evidence_store_updates_outcome_without_replacing_decision() -> None:
    routing = build_routing_decision_contract(
        _decision(), task="store task", criticality="low", features={}, correlation_id="corr-store"
    )
    started = build_outcome_event(
        routing, event_type="execution_started", outcome="unknown", features={}
    )
    store = EvidenceStore(max_entries=2)
    key = store.put(ExplainEvidence(routing, started), scope="default")
    assert key is not None
    completed = build_outcome_event(
        routing,
        event_type="execution_finished",
        outcome="success",
        features={},
        occurred_at="2026-07-22T00:00:05Z",
    )
    updated = store.update_outcome(key, completed)
    assert updated is not None
    assert updated.routing_decision == routing
    assert updated.outcome_event == completed
    assert store.find(correlation_id="corr-store", scope="default") == updated


def test_evidence_store_preserves_append_only_events_and_envelope_schema() -> None:
    routing = build_routing_decision_contract(
        _decision(),
        task="append-only",
        criticality="low",
        features={},
        correlation_id="corr-events",
    )
    started = build_outcome_event(routing, event_type="execution_started", outcome="unknown")
    completed = build_outcome_event(routing, event_type="execution_finished", outcome="success")
    late_abort = build_outcome_event(routing, event_type="execution_aborted", outcome="cancelled")
    store = EvidenceStore()
    key = store.put(ExplainEvidence(routing, started), scope="default")

    updated = store.append_event(key, completed)
    assert updated is not None
    payload = updated.to_dict()
    assert payload["kind"] == "execution_evidence"
    assert payload["envelope_version"] == "1"
    assert [event["event_type"] for event in payload["events"]] == [
        "execution_started",
        "execution_finished",
    ]
    assert payload["outcome_event"] == payload["events"][-1]
    for schema in SCHEMAS:
        registry = Registry().with_resource(schema["$id"], Resource.from_contents(schema))
        validator = Draft202012Validator(
            {"$ref": f"{schema['$id']}#/$defs/execution_evidence"}, registry=registry
        )
        assert list(validator.iter_errors(payload)) == []

    unchanged = store.append_event(key, late_abort)
    assert unchanged == updated
    assert [event.event_type for event in unchanged.events] == [  # type: ignore[union-attr]
        "execution_started",
        "execution_finished",
    ]


def test_explain_evidence_freezes_event_collection() -> None:
    routing = build_routing_decision_contract(
        _decision(), task="frozen events", criticality="low", features={}
    )
    started = build_outcome_event(routing, event_type="execution_started", outcome="unknown")
    event_list = [started]
    evidence = ExplainEvidence(routing, started, events=event_list)  # type: ignore[arg-type]
    event_list.append(build_outcome_event(routing, event_type="late", outcome="unknown"))

    assert len(evidence.events) == 1
    assert isinstance(evidence.events, tuple)


def test_evidence_store_terminal_outcome_is_idempotent() -> None:
    routing = build_routing_decision_contract(
        _decision(), task="terminal", criticality="low", features={}, correlation_id="corr-terminal"
    )
    store = EvidenceStore()
    key = store.put(
        ExplainEvidence(
            routing, build_outcome_event(routing, event_type="start", outcome="unknown")
        ),
        scope="default",
    )
    assert key is not None
    first = build_outcome_event(routing, event_type="done", outcome="success")
    second = build_outcome_event(routing, event_type="late-abort", outcome="cancelled")
    stored = store.update_outcome(key, first)
    assert stored is not None
    assert store.update_outcome(key, second) == stored


def test_evidence_store_lookup_is_scope_bound() -> None:
    routing = build_routing_decision_contract(
        _decision(), task="scope", criticality="low", features={}, correlation_id="corr-scope"
    )
    evidence = ExplainEvidence(
        routing, build_outcome_event(routing, event_type="start", outcome="unknown")
    )
    store = EvidenceStore()
    key = store.put(evidence, scope="tenant-a")
    assert store.find(evidence_id=key, scope="tenant-a") is not None
    assert store.find(evidence_id=key, scope="tenant-b") is None
    assert store.find(correlation_id="corr-scope", scope="tenant-b") is None


def test_runtime_evidence_does_not_claim_verification_or_quality() -> None:
    routing = build_routing_decision_contract(
        _decision(), task="truth", criticality="low", features={}
    )
    event = build_outcome_event(routing, event_type="transport_finished", outcome="success")
    payload = event.to_dict()
    assert payload["outcome"] == "success"
    assert payload["verification"]["status"] == "not_observed"
    assert payload["quality"]["outcome"] == "not_observed"
    assert payload["cost"] == {}
    assert payload["retries"] == 0
    assert payload["fallbacks"] == []
    assert payload["provider_version"] is None
    assert payload["model_version"] is None


def test_evidence_allowlists_candidate_fields_and_hashes_untrusted_tool_names() -> None:
    decision = _decision()
    decision.candidate_states[0]["prompt"] = "do not retain this"
    routing = build_routing_decision_contract(
        decision,
        task="allowlist",
        criticality="low",
        features={"tool_names": ["tool name with prompt=private"]},
    )
    payload = routing.to_dict()
    rendered = json.dumps(payload)
    assert "do not retain this" not in rendered
    assert "private" not in rendered
    assert payload["task_spec"]["tools"][0].startswith("redacted-")


def test_contract_objects_round_trip_from_evidence_payload() -> None:
    routing = build_routing_decision_contract(
        _decision(), task="round trip", criticality="medium", features={}
    )
    event = build_outcome_event(routing, event_type="done", outcome="success", features={})
    assert RoutingDecisionContract.from_dict(routing.to_dict()) == routing
    assert OutcomeEvent.from_dict(event.to_dict()) == event


def test_evidence_store_does_not_cross_link_duplicate_request_ids() -> None:
    store = EvidenceStore(max_entries=4)
    first = build_routing_decision_contract(
        _decision(), task="first", criticality="low", features={}, correlation_id="first"
    )
    second = build_routing_decision_contract(
        _decision(), task="second", criticality="low", features={}, correlation_id="second"
    )
    first_key = store.put(
        ExplainEvidence(first, build_outcome_event(first, event_type="start", outcome="unknown")),
        scope="default",
    )
    second_key = store.put(
        ExplainEvidence(second, build_outcome_event(second, event_type="start", outcome="unknown")),
        scope="default",
    )
    assert first_key and second_key and first_key != second_key

    completed = build_outcome_event(second, event_type="done", outcome="success")
    updated = store.update_outcome(second_key, completed)
    assert updated is not None
    assert store.find(correlation_id="first", scope="default").outcome_event.outcome == "unknown"  # type: ignore[union-attr]
    assert store.find(correlation_id="second", scope="default").outcome_event.outcome == "success"  # type: ignore[union-attr]


def test_evidence_lookup_requires_a_scope() -> None:
    store = EvidenceStore()
    with pytest.raises(ValueError, match="scope is required"):
        store.find(evidence_id="missing", scope="")
    with pytest.raises(ValueError, match="storage scope is required"):
        routing = build_routing_decision_contract(
            _decision(), task="scope", criticality="low", features={}
        )
        started = build_outcome_event(routing, event_type="start", outcome="unknown")
        store.put(ExplainEvidence(routing, started), scope="")
