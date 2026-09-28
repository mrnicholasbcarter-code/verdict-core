"""Tests for OpenJev context-length failure normalization (BOD-203)."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from verdict.decision_signals.contracts import DecisionQuestionV1
from verdict.decision_signals.openjev import OpenJevSystemOneProvider, normalize_failure
from verdict.gateway_adapter_runtime import AdapterFailureSignal
from verdict.gateway_adapters import NormalizedFailureClass

NOW = datetime(2026, 9, 28, 0, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _question(**kw: object) -> DecisionQuestionV1:
    return DecisionQuestionV1(
        purpose=kw.get("purpose", "frontier_planning"),  # type: ignore[arg-type]
        task_summary=kw.get("task_summary", "test goal"),  # type: ignore[arg-type]
        complexity_hints=kw.get("complexity_hints", {}),  # type: ignore[arg-type]
    )


def _provider(transport, *, api_key: str = "sk-test") -> OpenJevSystemOneProvider:
    return OpenJevSystemOneProvider(
        base_url="https://api.codiv.ai", api_key=api_key, transport=transport
    )


def _error_body(message: str) -> bytes:
    return json.dumps(
        {"detail": {"error_type": "invalid_request_error", "message": message}}
    ).encode()


# ---------------------------------------------------------------------------
# normalize_failure unit tests
# ---------------------------------------------------------------------------


class TestNormalizeFailureContextLength:
    """Context-length detection in normalize_failure."""

    EXACT_KIRO_BODY = (
        "[kiro/claude-sonnet-5-thinking] [400]: Input is too long. (reset after 73h 30m 45s)"
    )

    def test_exact_kiro_error_string(self) -> None:
        """Exact string from production: context-length class, no cooldown, not retryable."""
        sig = AdapterFailureSignal(code="invalid_request_error", status_code=400)
        result = normalize_failure(sig, now=NOW, body_text=self.EXACT_KIRO_BODY)
        assert result.failure_class == NormalizedFailureClass.CONTEXT_LENGTH
        assert result.cooldown_seconds is None
        assert result.retryable is False

    def test_kiro_body_no_retry_after_parsed(self) -> None:
        """The 'reset after 73h 30m 45s' hint must NOT become cooldown_seconds."""
        sig = AdapterFailureSignal(
            code="invalid_request_error",
            status_code=400,
            retry_after="264645",  # hypothetical header
        )
        result = normalize_failure(sig, now=NOW, body_text=self.EXACT_KIRO_BODY)
        assert result.failure_class == NormalizedFailureClass.CONTEXT_LENGTH
        assert result.cooldown_seconds is None

    def test_http_413(self) -> None:
        """HTTP 413 with context-length marker -> CONTEXT_LENGTH."""
        sig = AdapterFailureSignal(code="http_413", status_code=413)
        result = normalize_failure(sig, now=NOW, body_text="Input is too long")
        assert result.failure_class == NormalizedFailureClass.CONTEXT_LENGTH
        assert result.cooldown_seconds is None
        assert result.retryable is False

    def test_context_length_exceeded_marker(self) -> None:
        """Body containing 'context_length_exceeded' -> CONTEXT_LENGTH."""
        sig = AdapterFailureSignal(code="context_length_exceeded", status_code=400)
        result = normalize_failure(
            sig, now=NOW, body_text="Error: context_length_exceeded for this model"
        )
        assert result.failure_class == NormalizedFailureClass.CONTEXT_LENGTH

    def test_prompt_too_long_marker(self) -> None:
        """Body containing 'prompt is too long' -> CONTEXT_LENGTH."""
        sig = AdapterFailureSignal(code="invalid_request_error", status_code=400)
        result = normalize_failure(sig, now=NOW, body_text="prompt is too long for model X")
        assert result.failure_class == NormalizedFailureClass.CONTEXT_LENGTH

    def test_normal_400_unchanged(self) -> None:
        """A plain 400 without context-length markers stays INVALID_REQUEST."""
        sig = AdapterFailureSignal(code="invalid_request_error", status_code=400)
        result = normalize_failure(sig, now=NOW, body_text="invalid JSON body")
        assert result.failure_class == NormalizedFailureClass.INVALID_REQUEST
        assert result.retryable is False

    def test_normal_400_no_body(self) -> None:
        """400 with empty body_text -> INVALID_REQUEST (no false positive)."""
        sig = AdapterFailureSignal(code="invalid_request_error", status_code=400)
        result = normalize_failure(sig, now=NOW, body_text="")
        assert result.failure_class == NormalizedFailureClass.INVALID_REQUEST

    def test_429_with_retry_after_still_rate_limit(self) -> None:
        """429 with Retry-After still gets RATE_LIMIT + cooldown (not context-length)."""
        sig = AdapterFailureSignal(code="rate_limit_error", status_code=429, retry_after="30")
        result = normalize_failure(sig, now=NOW, body_text="rate limited")
        assert result.failure_class == NormalizedFailureClass.RATE_LIMIT
        assert result.cooldown_seconds == 30.0
        assert result.retryable is True

    def test_429_with_context_body_still_rate_limit(self) -> None:
        """429 even with context-length-ish body stays RATE_LIMIT (429 branch wins)."""
        sig = AdapterFailureSignal(code="rate_limit_error", status_code=429, retry_after="10")
        result = normalize_failure(sig, now=NOW, body_text="Input is too long for rate limiting")
        assert result.failure_class == NormalizedFailureClass.RATE_LIMIT

    def test_500_unchanged(self) -> None:
        """500 stays UPSTREAM regardless of body text."""
        sig = AdapterFailureSignal(code="http_500", status_code=500)
        result = normalize_failure(sig, now=NOW, body_text="Input is too long somehow")
        assert result.failure_class == NormalizedFailureClass.UPSTREAM


# ---------------------------------------------------------------------------
# End-to-end: OpenJevSystemOneProvider.signals() with context-length error
# ---------------------------------------------------------------------------


class TestProviderContextLengthE2E:
    """Provider-level integration: transport returns a context-length error."""

    EXACT_KIRO_BODY = (
        "[kiro/claude-sonnet-5-thinking] [400]: Input is too long. (reset after 73h 30m 45s)"
    )

    def _transport_400_context(self, url, headers, payload):
        body = _error_body(self.EXACT_KIRO_BODY)
        return 400, {"content-type": "application/json"}, body

    def _transport_413_context(self, url, headers, payload):
        return 413, {}, b"Input is too long"

    def _transport_400_normal(self, url, headers, payload):
        body = _error_body("invalid model parameter")
        return 400, {"content-type": "application/json"}, body

    def _transport_429_rate(self, url, headers, payload):
        body = json.dumps(
            {"detail": {"error_type": "rate_limit_error", "message": "slow down"}}
        ).encode()
        return 429, {"retry-after": "45"}, body

    def test_400_context_length_signals_none(self) -> None:
        """Provider returns signals=None for context-length error."""
        p = _provider(self._transport_400_context)
        result = p.signals(_question(), now=NOW)
        assert result.failure_class == NormalizedFailureClass.CONTEXT_LENGTH
        assert result.signals is None

    def test_413_context_length(self) -> None:
        """Provider handles HTTP 413 context-length."""
        p = _provider(self._transport_413_context)
        result = p.signals(_question(), now=NOW)
        assert result.failure_class == NormalizedFailureClass.CONTEXT_LENGTH
        assert result.signals is None

    def test_normal_400_still_invalid_request(self) -> None:
        """Non-context 400 still returns INVALID_REQUEST."""
        p = _provider(self._transport_400_normal)
        result = p.signals(_question(), now=NOW)
        assert result.failure_class == NormalizedFailureClass.INVALID_REQUEST

    def test_429_still_rate_limit_with_cooldown(self) -> None:
        """429 with Retry-After still returns RATE_LIMIT (not context-length)."""
        p = _provider(self._transport_429_rate)
        result = p.signals(_question(), now=NOW)
        # Provider returns failure_class but _fail doesn't propagate cooldown directly.
        # The key invariant: it's NOT classified as CONTEXT_LENGTH.
        assert result.failure_class == NormalizedFailureClass.RATE_LIMIT
