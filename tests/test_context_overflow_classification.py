"""Tests for context-length overflow classification across relay, autodev_routing,
and bounded_recovery paths.

Covers BOD-203 invariant: context-length errors are request-scoped, never produce
a provider/model cooldown, and never become MODEL_CAPABILITY_DEFICIT.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from verdict.autodev_routing import OpenAICompatibleEvidenceAdapter
from verdict.bounded_recovery import (
    BoundedRecoveryController,
    CostLedger,
    ExecutionRoute,
    FailureEvidence,
    RecoveryAction,
    RecoveryBounds,
    RecoveryFailureClass,
    classify_failure,
)
from verdict.gateway_adapter_runtime import AdapterFailureSignal
from verdict.gateway_adapters import NormalizedFailureClass
from verdict.relay import failure_class, retryable_response_status
from verdict.subagent_selection import is_context_length_error

# ---------------------------------------------------------------------------
# Shared fixtures / constants
# ---------------------------------------------------------------------------

KIRO_REAL_ERROR = (
    "[kiro/claude-sonnet-5-thinking] [400]: Input is too long. (reset after 73h 30m 45s)"
)

OPENAI_CTX_BODY = (
    '{"error": {"message": "This model\'s maximum context length is 128000 tokens. '
    'However, your messages resulted in 200451 tokens.", '
    '"type": "invalid_request_error", "code": "context_length_exceeded"}}'
)

STATUS_413_BODY = "Request body too large: input is too long for this model"


# ---------------------------------------------------------------------------
# 1. is_context_length_error (shared marker detection)
# ---------------------------------------------------------------------------


class TestIsContextLengthError:
    def test_kiro_real_string(self) -> None:
        assert is_context_length_error(KIRO_REAL_ERROR)

    def test_openai_context_length_exceeded(self) -> None:
        assert is_context_length_error(OPENAI_CTX_BODY)

    def test_413_body(self) -> None:
        assert is_context_length_error(STATUS_413_BODY)

    def test_normal_400_no_match(self) -> None:
        assert not is_context_length_error("invalid parameter: temperature must be between 0 and 2")

    def test_empty_string(self) -> None:
        assert not is_context_length_error("")


# ---------------------------------------------------------------------------
# 2. relay.py failure_class
# ---------------------------------------------------------------------------


class TestRelayFailureClass:
    def test_context_length_400_with_body(self) -> None:
        assert failure_class(400, body=KIRO_REAL_ERROR) == "context_length_overflow"

    def test_context_length_413_with_body(self) -> None:
        assert failure_class(413, body=STATUS_413_BODY) == "context_length_overflow"

    def test_openai_context_length_body(self) -> None:
        assert failure_class(400, body=OPENAI_CTX_BODY) == "context_length_overflow"

    def test_normal_400_without_markers(self) -> None:
        """A normal 400 (e.g. invalid parameter) keeps today's classification."""
        assert failure_class(400) == "capability_or_request_failure"

    def test_normal_400_with_unrelated_body(self) -> None:
        assert (
            failure_class(400, body="invalid parameter: temperature must be > 0")
            == "capability_or_request_failure"
        )

    def test_429_unchanged(self) -> None:
        """429 keeps its retryable transport classification."""
        assert failure_class(429) == "transport_retryable"

    def test_exception_with_context_length_message(self) -> None:
        exc = RuntimeError(KIRO_REAL_ERROR)
        assert failure_class(error=exc) == "context_length_overflow"

    def test_exception_without_context_length(self) -> None:
        exc = RuntimeError("some other error")
        assert failure_class(error=exc) == "transport_error"


# ---------------------------------------------------------------------------
# 3. relay.py retryable_response_status (context-length retryable to different model)
# ---------------------------------------------------------------------------


class TestRetryableResponseStatus:
    def test_context_length_400_retryable_with_body(self) -> None:
        """Context-length 400 is retryable when body is provided."""
        assert retryable_response_status(400, compatibility_applied=False, body=KIRO_REAL_ERROR)

    def test_context_length_413_retryable_with_body(self) -> None:
        assert retryable_response_status(413, compatibility_applied=False, body=STATUS_413_BODY)

    def test_normal_400_not_retryable(self) -> None:
        """Normal 400 without context-length markers is NOT retryable."""
        assert not retryable_response_status(400, compatibility_applied=False)

    def test_normal_400_not_retryable_with_body(self) -> None:
        assert not retryable_response_status(400, compatibility_applied=False, body="bad parameter")

    def test_compatibility_400_not_retryable(self) -> None:
        """Compatibility-applied 400 is not retryable even without context-length."""
        assert not retryable_response_status(400, compatibility_applied=True)

    def test_429_still_retryable(self) -> None:
        assert retryable_response_status(429, compatibility_applied=False)


# ---------------------------------------------------------------------------
# 4. autodev_routing.py normalize_failure
# ---------------------------------------------------------------------------


class TestAutodevNormalizeFailure:
    @pytest.fixture()
    def adapter(self) -> OpenAICompatibleEvidenceAdapter:
        # Only normalize_failure is needed; no adapter init required
        return OpenAICompatibleEvidenceAdapter.__new__(OpenAICompatibleEvidenceAdapter)

    def test_context_length_400(self, adapter: OpenAICompatibleEvidenceAdapter) -> None:
        signal = AdapterFailureSignal(code="Input is too long", status_code=400)
        result = adapter.normalize_failure(signal)
        assert result.failure_class is NormalizedFailureClass.CONTEXT_LENGTH
        assert result.cooldown_seconds is None
        assert not result.retryable  # not in _TRANSIENT

    def test_context_length_413(self, adapter: OpenAICompatibleEvidenceAdapter) -> None:
        signal = AdapterFailureSignal(code="context_length_exceeded", status_code=413)
        result = adapter.normalize_failure(signal)
        assert result.failure_class is NormalizedFailureClass.CONTEXT_LENGTH
        assert result.cooldown_seconds is None

    def test_kiro_real_error_string(self, adapter: OpenAICompatibleEvidenceAdapter) -> None:
        """The exact real error string must classify as context-length, not capability."""
        signal = AdapterFailureSignal(code=KIRO_REAL_ERROR, status_code=400)
        result = adapter.normalize_failure(signal)
        assert result.failure_class is NormalizedFailureClass.CONTEXT_LENGTH
        assert result.cooldown_seconds is None

    def test_normal_400_still_capability(self, adapter: OpenAICompatibleEvidenceAdapter) -> None:
        """A normal 400 keeps CAPABILITY classification."""
        signal = AdapterFailureSignal(code="invalid_parameter", status_code=400)
        result = adapter.normalize_failure(signal)
        assert result.failure_class is NormalizedFailureClass.CAPABILITY

    def test_429_with_reset_still_rate_limit(
        self, adapter: OpenAICompatibleEvidenceAdapter
    ) -> None:
        """429 with reset hint still produces a cooldown (negative test)."""
        signal = AdapterFailureSignal(code="rate_limited", status_code=429, retry_after="30")
        result = adapter.normalize_failure(signal)
        assert result.failure_class is NormalizedFailureClass.RATE_LIMIT
        assert result.cooldown_seconds is not None
        assert result.cooldown_seconds > 0


# ---------------------------------------------------------------------------
# 5. bounded_recovery.py classify_failure
# ---------------------------------------------------------------------------


class TestBoundedRecoveryClassify:
    def test_context_length_error_class(self) -> None:
        evidence = FailureEvidence(
            error_class="context_length_exceeded", message=KIRO_REAL_ERROR, status_code=400
        )
        assert classify_failure(evidence) is RecoveryFailureClass.CONTEXT_LENGTH_OVERFLOW

    def test_context_length_overflow_error_class(self) -> None:
        evidence = FailureEvidence(
            error_class="context_length_overflow", message="input is too long", status_code=400
        )
        assert classify_failure(evidence) is RecoveryFailureClass.CONTEXT_LENGTH_OVERFLOW

    def test_context_length_413(self) -> None:
        evidence = FailureEvidence(error_class="", message=STATUS_413_BODY, status_code=413)
        assert classify_failure(evidence) is RecoveryFailureClass.CONTEXT_LENGTH_OVERFLOW

    def test_context_length_via_message_markers(self) -> None:
        evidence = FailureEvidence(
            error_class="unknown",
            message="maximum context length is 128000 tokens",
            status_code=400,
        )
        assert classify_failure(evidence) is RecoveryFailureClass.CONTEXT_LENGTH_OVERFLOW

    def test_context_length_signal(self) -> None:
        evidence = FailureEvidence(signals=frozenset({"context_length_exceeded"}))
        assert classify_failure(evidence) is RecoveryFailureClass.CONTEXT_LENGTH_OVERFLOW

    def test_normal_400_not_context_length(self) -> None:
        """Normal 400 without context-length markers -> not CONTEXT_LENGTH_OVERFLOW."""
        evidence = FailureEvidence(
            signals=frozenset({"capability_deficit"}),
            message="model cannot handle this task",
            status_code=400,
        )
        assert classify_failure(evidence) is RecoveryFailureClass.MODEL_CAPABILITY_DEFICIT

    def test_kiro_real_error_does_not_become_capability(self) -> None:
        """The exact real error must never become MODEL_CAPABILITY_DEFICIT."""
        evidence = FailureEvidence(
            error_class="context_length_exceeded", message=KIRO_REAL_ERROR, status_code=400
        )
        result = classify_failure(evidence)
        assert result is not RecoveryFailureClass.MODEL_CAPABILITY_DEFICIT
        assert result is RecoveryFailureClass.CONTEXT_LENGTH_OVERFLOW


# ---------------------------------------------------------------------------
# 6. bounded_recovery.py decide() maps context-length to SWITCH_EXECUTION_PLANE
# ---------------------------------------------------------------------------


class TestBoundedRecoveryDecide:
    @pytest.fixture()
    def route(self) -> ExecutionRoute:
        return ExecutionRoute(model_id="claude-sonnet-5", provider="kiro", capability_tier=2)

    @pytest.fixture()
    def alt_route(self) -> ExecutionRoute:
        return ExecutionRoute(
            model_id="claude-opus-5", provider="kiro", capability_tier=3, is_frontier=True
        )

    @pytest.fixture()
    def eq_route(self) -> ExecutionRoute:
        return ExecutionRoute(model_id="gpt-4.1", provider="openai", capability_tier=2)

    @pytest.fixture()
    def ledger(self) -> CostLedger:
        return CostLedger(trajectory_id="test-ctx-overflow", cash_budget_usd=Decimal("10.00"))

    @pytest.fixture()
    def controller(self) -> BoundedRecoveryController:
        return BoundedRecoveryController(bounds=RecoveryBounds(max_attempts=5))

    def test_context_overflow_switches_to_equivalent(
        self,
        controller: BoundedRecoveryController,
        route: ExecutionRoute,
        eq_route: ExecutionRoute,
        ledger: CostLedger,
    ) -> None:
        evidence = FailureEvidence(
            error_class="context_length_exceeded", message=KIRO_REAL_ERROR, status_code=400
        )
        decision = controller.decide(
            evidence=evidence, route=route, ledger=ledger, equivalent_routes=[eq_route]
        )
        assert decision.action is RecoveryAction.SWITCH_EXECUTION_PLANE
        assert decision.failure_class is RecoveryFailureClass.CONTEXT_LENGTH_OVERFLOW
        assert decision.route_after == eq_route
        assert not decision.escalated

    def test_context_overflow_falls_to_stronger_when_no_equivalent(
        self,
        controller: BoundedRecoveryController,
        route: ExecutionRoute,
        alt_route: ExecutionRoute,
        ledger: CostLedger,
    ) -> None:
        evidence = FailureEvidence(
            error_class="context_length_exceeded", message="input is too long", status_code=400
        )
        decision = controller.decide(
            evidence=evidence, route=route, ledger=ledger, stronger_routes=[alt_route]
        )
        assert decision.action is RecoveryAction.SWITCH_EXECUTION_PLANE
        assert decision.route_after == alt_route

    def test_context_overflow_blocks_when_no_alternatives(
        self, controller: BoundedRecoveryController, route: ExecutionRoute, ledger: CostLedger
    ) -> None:
        evidence = FailureEvidence(
            error_class="context_length_exceeded", message="input is too long", status_code=400
        )
        decision = controller.decide(evidence=evidence, route=route, ledger=ledger)
        assert decision.action is RecoveryAction.BLOCK
        assert decision.failure_class is RecoveryFailureClass.CONTEXT_LENGTH_OVERFLOW

    def test_context_overflow_never_escalates_capability(
        self,
        controller: BoundedRecoveryController,
        route: ExecutionRoute,
        alt_route: ExecutionRoute,
        ledger: CostLedger,
    ) -> None:
        """Context-length must never produce ESCALATE_CAPABILITY."""
        evidence = FailureEvidence(
            error_class="context_length_exceeded", message=KIRO_REAL_ERROR, status_code=400
        )
        decision = controller.decide(
            evidence=evidence, route=route, ledger=ledger, stronger_routes=[alt_route]
        )
        assert decision.action is not RecoveryAction.ESCALATE_CAPABILITY

    def test_reset_hint_not_parsed_as_cooldown(
        self,
        controller: BoundedRecoveryController,
        route: ExecutionRoute,
        eq_route: ExecutionRoute,
        ledger: CostLedger,
    ) -> None:
        """The 'reset after 73h' text must never produce a cooldown."""
        evidence = FailureEvidence(
            error_class="context_length_exceeded", message=KIRO_REAL_ERROR, status_code=400
        )
        decision = controller.decide(
            evidence=evidence, route=route, ledger=ledger, equivalent_routes=[eq_route]
        )
        # SWITCH_EXECUTION_PLANE, not a cooldown-bearing action
        assert decision.action is RecoveryAction.SWITCH_EXECUTION_PLANE


# ---------------------------------------------------------------------------
# 7. Negative tests — existing classifications preserved
# ---------------------------------------------------------------------------


class TestNegativePreservation:
    """Existing failure classifications must not change."""

    def test_normal_400_relay_unchanged(self) -> None:
        assert failure_class(400) == "capability_or_request_failure"

    def test_429_relay_unchanged(self) -> None:
        assert failure_class(429) == "transport_retryable"

    def test_500_relay_unchanged(self) -> None:
        assert failure_class(500) == "transport_upstream"

    def test_401_relay_unchanged(self) -> None:
        assert failure_class(401) == "auth_failure"

    def test_bounded_recovery_normal_capability_deficit(self) -> None:
        evidence = FailureEvidence(
            error_class="capability_deficit", message="model too weak for task", status_code=400
        )
        assert classify_failure(evidence) is RecoveryFailureClass.MODEL_CAPABILITY_DEFICIT

    def test_bounded_recovery_provider_failure(self) -> None:
        evidence = FailureEvidence(
            error_class="provider_error", message="gateway timeout", status_code=502
        )
        assert classify_failure(evidence) is RecoveryFailureClass.PROVIDER_OR_GATEWAY_FAILURE
