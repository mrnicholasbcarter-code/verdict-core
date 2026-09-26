"""Tests for decision signal contracts."""

import pytest

from verdict.decision_signals.contracts import (
    DecisionQuestionV1,
    DecisionSignalError,
    DecisionSignalSetV1,
    compute_input_digest,
)
from verdict.gateway_adapters import NormalizedFailureClass


def test_decision_question_v1_valid():
    """Valid DecisionQuestionV1 round-trips correctly."""
    question = DecisionQuestionV1(
        purpose="frontier_planning",
        task_summary="Implement feature X",
        complexity_hints={"lines": 500, "files": 3},
    )
    data = question.to_dict()
    restored = DecisionQuestionV1.from_dict(data)
    assert restored == question


def test_decision_question_v1_rejects_unknown_fields():
    """DecisionQuestionV1.from_dict rejects unknown fields."""
    with pytest.raises(DecisionSignalError, match="Unknown fields"):
        DecisionQuestionV1.from_dict(
            {
                "purpose": "test",
                "task_summary": "test",
                "complexity_hints": {},
                "extra_field": "bad",
            }
        )


def test_decision_signal_set_v1_valid():
    """Valid DecisionSignalSetV1 round-trips correctly."""
    signal_set = DecisionSignalSetV1(
        schema_version="decision-signals/v1",
        provider="openjev",
        model="system-one",
        version="1.0.0",
        request_id="test-001",
        purpose="frontier_planning",
        signals={
            "complexity": 0.7,
            "decomposability": 0.8,
            "ambiguity": 0.3,
            "frontier_worthy": 0.9,
            "security_sensitive": 0.2,
            "verification_strength": 0.85,
            "context_need": 0.6,
        },
        confidence=0.95,
        latency_ms=150,
        usage={"input_tokens": 100, "output_tokens": 20},
        input_digest="a" * 64,
        observed_at="2026-09-25T10:00:00Z",
        failure_class=None,
        mode="SHADOW",
    )
    data = signal_set.to_dict()
    restored = DecisionSignalSetV1.from_dict(data)
    assert restored == signal_set


def test_decision_signal_set_v1_rejects_wrong_schema_version():
    """DecisionSignalSetV1 rejects wrong schema_version."""
    with pytest.raises(DecisionSignalError, match="Wrong schema_version"):
        DecisionSignalSetV1(
            schema_version="wrong-version",
            provider="test",
            model="test",
            version="1.0",
            request_id="test",
            purpose="test",
            signals=None,
            confidence=0.5,
            latency_ms=100,
            usage={"input_tokens": 10, "output_tokens": 5},
            input_digest="a" * 64,
            observed_at="2026-09-25T10:00:00Z",
            failure_class=NormalizedFailureClass.UNKNOWN,
            mode="SHADOW",
        )


def test_decision_signal_set_v1_rejects_probability_out_of_range():
    """DecisionSignalSetV1 rejects probabilities outside [0,1]."""
    with pytest.raises(DecisionSignalError, match=r"must be in \[0,1\]"):
        DecisionSignalSetV1(
            schema_version="decision-signals/v1",
            provider="test",
            model="test",
            version="1.0",
            request_id="test",
            purpose="test",
            signals={"complexity": 1.5},  # Invalid
            confidence=0.5,
            latency_ms=100,
            usage={"input_tokens": 10, "output_tokens": 5},
            input_digest="a" * 64,
            observed_at="2026-09-25T10:00:00Z",
            failure_class=None,
            mode="SHADOW",
        )


def test_decision_signal_set_v1_rejects_nan():
    """DecisionSignalSetV1 rejects NaN in probabilities."""
    with pytest.raises(DecisionSignalError, match="must not be NaN or inf"):
        DecisionSignalSetV1(
            schema_version="decision-signals/v1",
            provider="test",
            model="test",
            version="1.0",
            request_id="test",
            purpose="test",
            signals={"complexity": float("nan")},
            confidence=0.5,
            latency_ms=100,
            usage={"input_tokens": 10, "output_tokens": 5},
            input_digest="a" * 64,
            observed_at="2026-09-25T10:00:00Z",
            failure_class=None,
            mode="SHADOW",
        )


def test_decision_signal_set_v1_rejects_inf():
    """DecisionSignalSetV1 rejects inf in probabilities."""
    with pytest.raises(DecisionSignalError, match="must not be NaN or inf"):
        DecisionSignalSetV1(
            schema_version="decision-signals/v1",
            provider="test",
            model="test",
            version="1.0",
            request_id="test",
            purpose="test",
            signals=None,
            confidence=float("inf"),
            latency_ms=100,
            usage={"input_tokens": 10, "output_tokens": 5},
            input_digest="a" * 64,
            observed_at="2026-09-25T10:00:00Z",
            failure_class=None,
            mode="SHADOW",
        )


def test_decision_signal_set_v1_from_dict_rejects_unknown_fields():
    """DecisionSignalSetV1.from_dict rejects unknown fields."""
    with pytest.raises(DecisionSignalError, match="Unknown fields"):
        DecisionSignalSetV1.from_dict(
            {
                "schema_version": "decision-signals/v1",
                "provider": "test",
                "model": "test",
                "version": "1.0",
                "request_id": "test",
                "purpose": "test",
                "signals": None,
                "confidence": 0.5,
                "latency_ms": 100,
                "usage": {"input_tokens": 10, "output_tokens": 5},
                "input_digest": "a" * 64,
                "observed_at": "2026-09-25T10:00:00Z",
                "failure_class": None,
                "mode": "SHADOW",
                "extra_field": "bad",
            }
        )


def test_decision_signal_set_v1_from_dict_rejects_missing_fields():
    """DecisionSignalSetV1.from_dict rejects missing required fields."""
    with pytest.raises(DecisionSignalError, match="Missing required fields"):
        DecisionSignalSetV1.from_dict(
            {
                "schema_version": "decision-signals/v1",
                "provider": "test",
                # Missing "model"
                "version": "1.0",
                "request_id": "test",
                "purpose": "test",
                "signals": None,
                "confidence": 0.5,
                "latency_ms": 100,
                "usage": {"input_tokens": 10, "output_tokens": 5},
                "input_digest": "a" * 64,
                "observed_at": "2026-09-25T10:00:00Z",
                "failure_class": None,
                "mode": "SHADOW",
            }
        )


def test_decision_signal_set_v1_rejects_unknown_signal_keys():
    """DecisionSignalSetV1 rejects unknown signal keys."""
    with pytest.raises(DecisionSignalError, match="Unknown signal keys"):
        DecisionSignalSetV1(
            schema_version="decision-signals/v1",
            provider="test",
            model="test",
            version="1.0",
            request_id="test",
            purpose="test",
            signals={"unknown_key": 0.5},
            confidence=0.5,
            latency_ms=100,
            usage={"input_tokens": 10, "output_tokens": 5},
            input_digest="a" * 64,
            observed_at="2026-09-25T10:00:00Z",
            failure_class=None,
            mode="SHADOW",
        )


def test_compute_input_digest():
    """compute_input_digest produces consistent sha256 hex."""
    question = DecisionQuestionV1(
        purpose="test", task_summary="test task", complexity_hints={"key": "value"}
    )
    digest1 = compute_input_digest(question)
    digest2 = compute_input_digest(question)
    assert digest1 == digest2
    assert len(digest1) == 64
    assert all(c in "0123456789abcdef" for c in digest1)


def test_json_schema_parity():
    """JSON Schema validation matches Python dataclass validation."""
    import json
    from pathlib import Path

    import jsonschema

    schema_path = Path("schemas/decision-signals-v1.json")
    schema = json.loads(schema_path.read_text())

    # Valid signal set
    valid_data = {
        "schema_version": "decision-signals/v1",
        "provider": "test",
        "model": "test",
        "version": "1.0",
        "request_id": "test",
        "purpose": "test",
        "signals": {"complexity": 0.5},
        "confidence": 0.8,
        "latency_ms": 100,
        "usage": {"input_tokens": 10, "output_tokens": 5},
        "input_digest": "a" * 64,
        "observed_at": "2026-09-25T10:00:00Z",
        "failure_class": None,
        "mode": "SHADOW",
    }

    # Should pass JSON Schema validation
    jsonschema.validate(valid_data, schema)

    # Should also pass Python validation
    DecisionSignalSetV1.from_dict(valid_data)

    # Invalid: probability > 1
    invalid_data = {**valid_data, "signals": {"complexity": 1.5}}

    # Should fail JSON Schema validation
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(invalid_data, schema)

    # Should also fail Python validation
    with pytest.raises(DecisionSignalError):
        DecisionSignalSetV1.from_dict(invalid_data)
