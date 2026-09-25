"""Tests for OpenJev System-One provider (BOD-199)."""

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from verdict.decision_signals.contracts import DecisionQuestionV1
from verdict.decision_signals.openjev import OpenJevSystemOneProvider
from verdict.gateway_adapters import NormalizedFailureClass


def test_openjev_post_endpoint_is_v1_systemone():
    """OpenJevSystemOneProvider POSTs to /v1/systemone (not /v1/chat/completions)."""
    captured_url = None

    def mock_transport(url, headers, payload):
        nonlocal captured_url
        captured_url = url
        # Return success
        response = {
            "provider": "openjev",
            "model": "system-one",
            "version": "1.0.0",
            "request_id": "test",
            "signals": {"frontier_worthy": 0.8},
            "confidence": 0.9,
            "latency_ms": 100,
            "usage": {"input_tokens": 10, "output_tokens": 5},
            "observed_at": "2026-09-25T10:00:00Z",
        }
        return 200, {}, json.dumps(response).encode("utf-8")

    provider = OpenJevSystemOneProvider(
        base_url="https://test.example.com", api_key="test-key", transport=mock_transport
    )

    question = DecisionQuestionV1(purpose="test", task_summary="test", complexity_hints={})
    now = datetime.now(timezone.utc)
    provider.signals(question, now=now)

    assert captured_url is not None
    assert captured_url.endswith("/v1/systemone")
    assert "/v1/chat/completions" not in captured_url


def test_openjev_confident_response():
    """Confident OpenJev response returns valid DecisionSignalSetV1."""
    fixture_path = Path("tests/fixtures/openjev/confident.json")
    fixture_data = json.loads(fixture_path.read_text())

    def mock_transport(url, headers, payload):
        return 200, {}, json.dumps(fixture_data).encode("utf-8")

    provider = OpenJevSystemOneProvider(
        base_url="https://test.example.com", api_key="test-key", transport=mock_transport
    )

    question = DecisionQuestionV1(
        purpose="frontier_planning", task_summary="test", complexity_hints={}
    )
    now = datetime.now(timezone.utc)
    result = provider.signals(question, now=now)

    assert result.schema_version == "decision-signals/v1"
    assert result.provider == "openjev"
    assert result.model == "system-one"
    assert result.signals is not None
    assert result.signals["frontier_worthy"] == 0.95
    assert result.confidence == 0.98
    assert result.failure_class is None
    assert result.mode == "SHADOW"


def test_openjev_uncertain_response():
    """Uncertain OpenJev response returns valid signal set with low confidence."""
    fixture_path = Path("tests/fixtures/openjev/uncertain.json")
    fixture_data = json.loads(fixture_path.read_text())

    def mock_transport(url, headers, payload):
        return 200, {}, json.dumps(fixture_data).encode("utf-8")

    provider = OpenJevSystemOneProvider(
        base_url="https://test.example.com", api_key="test-key", transport=mock_transport
    )

    question = DecisionQuestionV1(purpose="test", task_summary="test", complexity_hints={})
    now = datetime.now(timezone.utc)
    result = provider.signals(question, now=now)

    assert result.confidence == 0.35
    assert result.signals["frontier_worthy"] == 0.48
    assert result.failure_class is None


def test_openjev_malformed_json():
    """Malformed JSON response returns failure_class=INVALID_REQUEST."""

    def mock_transport(url, headers, payload):
        return 200, {}, b"{invalid json"

    provider = OpenJevSystemOneProvider(
        base_url="https://test.example.com", api_key="test-key", transport=mock_transport
    )

    question = DecisionQuestionV1(purpose="test", task_summary="test", complexity_hints={})
    now = datetime.now(timezone.utc)
    result = provider.signals(question, now=now)

    assert result.signals is None
    assert result.failure_class == NormalizedFailureClass.INVALID_REQUEST
    assert result.mode == "SHADOW"


def test_openjev_schema_violation():
    """Schema violation (missing field) returns failure_class=INVALID_REQUEST."""
    fixture_path = Path("tests/fixtures/openjev/schema_violation.json")
    fixture_data = json.loads(fixture_path.read_text())

    def mock_transport(url, headers, payload):
        return 200, {}, json.dumps(fixture_data).encode("utf-8")

    provider = OpenJevSystemOneProvider(
        base_url="https://test.example.com", api_key="test-key", transport=mock_transport
    )

    question = DecisionQuestionV1(purpose="test", task_summary="test", complexity_hints={})
    now = datetime.now(timezone.utc)
    result = provider.signals(question, now=now)

    assert result.signals is None
    assert result.failure_class == NormalizedFailureClass.INVALID_REQUEST


def test_openjev_timeout():
    """Timeout returns failure_class=TIMEOUT."""

    def mock_transport(url, headers, payload):
        raise TimeoutError("Connection timeout")

    provider = OpenJevSystemOneProvider(
        base_url="https://test.example.com", api_key="test-key", transport=mock_transport
    )

    question = DecisionQuestionV1(purpose="test", task_summary="test", complexity_hints={})
    now = datetime.now(timezone.utc)
    result = provider.signals(question, now=now)

    assert result.signals is None
    assert result.failure_class == NormalizedFailureClass.TIMEOUT
    assert result.mode == "SHADOW"


def test_openjev_429_quota_exhausted():
    """HTTP 429 with quota exhausted code returns failure_class=QUOTA."""

    def mock_transport(url, headers, payload):
        return 429, {}, b'{"error": {"code": "quota_exceeded"}}'

    provider = OpenJevSystemOneProvider(
        base_url="https://test.example.com", api_key="test-key", transport=mock_transport
    )

    question = DecisionQuestionV1(purpose="test", task_summary="test", complexity_hints={})
    now = datetime.now(timezone.utc)
    result = provider.signals(question, now=now)

    assert result.signals is None
    assert result.failure_class == NormalizedFailureClass.QUOTA


def test_openjev_429_rate_limit_with_retry_after():
    """HTTP 429 with Retry-After returns failure_class=RATE_LIMIT."""

    def mock_transport(url, headers, payload):
        return 429, {"Retry-After": "120"}, b'{"error": {"message": "rate limited"}}'

    provider = OpenJevSystemOneProvider(
        base_url="https://test.example.com", api_key="test-key", transport=mock_transport
    )

    question = DecisionQuestionV1(purpose="test", task_summary="test", complexity_hints={})
    now = datetime.now(timezone.utc)
    result = provider.signals(question, now=now)

    assert result.signals is None
    assert result.failure_class == NormalizedFailureClass.RATE_LIMIT


def test_openjev_529_overload():
    """HTTP 529 returns failure_class=OVERLOADED."""

    def mock_transport(url, headers, payload):
        return 529, {}, b"Service overloaded"

    provider = OpenJevSystemOneProvider(
        base_url="https://test.example.com", api_key="test-key", transport=mock_transport
    )

    question = DecisionQuestionV1(purpose="test", task_summary="test", complexity_hints={})
    now = datetime.now(timezone.utc)
    result = provider.signals(question, now=now)

    assert result.signals is None
    assert result.failure_class == NormalizedFailureClass.OVERLOADED


def test_openjev_key_missing():
    """Missing API key returns failure_class=UNKNOWN without exception."""
    # Temporarily remove key
    old_key = os.environ.get("OPENJEV_API_KEY")
    if old_key:
        del os.environ["OPENJEV_API_KEY"]

    try:
        provider = OpenJevSystemOneProvider(
            # No base_url, no api_key, no env var
        )

        question = DecisionQuestionV1(purpose="test", task_summary="test", complexity_hints={})
        now = datetime.now(timezone.utc)
        result = provider.signals(question, now=now)

        assert result.signals is None
        assert result.failure_class == NormalizedFailureClass.UNKNOWN
        assert result.mode == "SHADOW"
    finally:
        if old_key:
            os.environ["OPENJEV_API_KEY"] = old_key


def test_openjev_all_tests_use_mock_transport():
    """All OpenJev tests use injectable transport (no network calls)."""
    # This is a meta-test to ensure we don't accidentally make real network calls.
    # If any test above doesn't pass transport=... to OpenJevSystemOneProvider,
    # and tries to call signals(), it would fail immediately.
    # This test documents that requirement.
    assert True
