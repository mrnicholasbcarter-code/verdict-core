"""Tests for recovery module: failure classification and recovery budgets."""

from datetime import datetime

import pytest

from verdict.orchestration.contracts import FailureClassification, WorkerTerminal
from verdict.orchestration.recovery import (
    CLASSIFIER_VERSION,
    MAX_COOLDOWN_SECONDS,
    FailureIntelligence,
    RecoveryBudget,
)


# Fixtures
@pytest.fixture
def classifier() -> FailureIntelligence:
    """Fresh classifier instance."""
    return FailureIntelligence()


@pytest.fixture
def now() -> datetime:
    """Fixed test datetime."""
    return datetime(2025, 1, 15, 12, 0, 0)


class TestFailureIntelligenceStatusCodes:
    """Test status code-based classification (priority 1)."""

    def test_429_quota_exhausted(self, classifier: FailureIntelligence, now: datetime) -> None:
        """429 + quota indicators -> quota_exhausted."""
        terminal = WorkerTerminal(
            ok=False, status_code=429, error="You have exceeded your monthly quota limit"
        )
        result = classifier.classify(terminal, now=now)
        assert result.category == "quota_exhausted"
        assert result.action == "REROUTE"
        assert result.scope == "provider"
        assert result.cooldown_seconds == 3600

    def test_429_rate_limited(self, classifier: FailureIntelligence, now: datetime) -> None:
        """429 without quota indicators -> rate_limited."""
        terminal = WorkerTerminal(ok=False, status_code=429, error="Too many requests")
        result = classifier.classify(terminal, now=now)
        assert result.category == "rate_limited"
        assert result.action == "REROUTE"
        assert result.scope == "provider"
        assert result.cooldown_seconds == 60

    def test_429_with_retry_after(self, classifier: FailureIntelligence, now: datetime) -> None:
        """429 with retry_after_seconds on terminal takes precedence."""
        terminal = WorkerTerminal(
            ok=False, status_code=429, error="Rate limited", retry_after_seconds=90.0
        )
        result = classifier.classify(terminal, now=now)
        assert result.category == "rate_limited"
        assert result.cooldown_seconds == 90.0

    def test_401_authentication(self, classifier: FailureIntelligence, now: datetime) -> None:
        """401 -> authentication."""
        terminal = WorkerTerminal(ok=False, status_code=401, error="Invalid API key")
        result = classifier.classify(terminal, now=now)
        assert result.category == "authentication"
        assert result.action == "REROUTE"
        assert result.scope == "provider"
        assert result.cooldown_seconds == 3600

    def test_402_payment_required(self, classifier: FailureIntelligence, now: datetime) -> None:
        """402 -> payment_required."""
        terminal = WorkerTerminal(ok=False, status_code=402, error="Payment method failed")
        result = classifier.classify(terminal, now=now)
        assert result.category == "payment_required"
        assert result.action == "REROUTE"
        assert result.scope == "provider"
        assert result.cooldown_seconds == 3600

    def test_403_permission(self, classifier: FailureIntelligence, now: datetime) -> None:
        """403 -> permission."""
        terminal = WorkerTerminal(ok=False, status_code=403, error="Access denied")
        result = classifier.classify(terminal, now=now)
        assert result.category == "permission"
        assert result.action == "REROUTE"
        assert result.scope == "route"
        assert result.cooldown_seconds == 3600

    def test_400_unsupported(self, classifier: FailureIntelligence, now: datetime) -> None:
        """400 with 'unsupported' -> unsupported."""
        terminal = WorkerTerminal(
            ok=False, status_code=400, error="Feature not supported by this model"
        )
        result = classifier.classify(terminal, now=now)
        assert result.category == "unsupported"
        assert result.action == "REROUTE"
        assert result.scope == "route"
        assert result.cooldown_seconds == 86400

    def test_400_generic(self, classifier: FailureIntelligence, now: datetime) -> None:
        """400 without 'unsupported' -> bad_request."""
        terminal = WorkerTerminal(ok=False, status_code=400, error="Invalid request format")
        result = classifier.classify(terminal, now=now)
        assert result.category == "bad_request"
        assert result.action == "REROUTE"

    def test_404_not_found(self, classifier: FailureIntelligence, now: datetime) -> None:
        """404 -> model_unavailable."""
        terminal = WorkerTerminal(ok=False, status_code=404, error="Model endpoint not found")
        result = classifier.classify(terminal, now=now)
        assert result.category == "model_unavailable"
        assert result.action == "REROUTE"
        assert result.scope == "route"
        assert result.cooldown_seconds == 3600

    def test_500_upstream_temporary(self, classifier: FailureIntelligence, now: datetime) -> None:
        """5xx -> upstream_temporary."""
        for code in [500, 502, 503, 504]:
            terminal = WorkerTerminal(ok=False, status_code=code, error="Internal server error")
            result = classifier.classify(terminal, now=now)
            assert result.category == "upstream_temporary"
            assert result.action == "REROUTE"
            assert result.scope == "route"
            assert result.cooldown_seconds == 60


class TestFailureIntelligenceTextPatterns:
    """Test text pattern matching (fallback when no status code)."""

    def test_model_not_found_text(self, classifier: FailureIntelligence, now: datetime) -> None:
        """'model not found' text pattern."""
        terminal = WorkerTerminal(ok=False, error="Unknown model does not exist")
        result = classifier.classify(terminal, now=now)
        assert result.category == "model_unavailable"
        assert result.scope == "route"

    def test_timeout_text(self, classifier: FailureIntelligence, now: datetime) -> None:
        """'timeout' text pattern."""
        terminal = WorkerTerminal(ok=False, error="Request timed out after 30s")
        result = classifier.classify(terminal, now=now)
        assert result.category == "timeout"
        assert result.cooldown_seconds == 120

    def test_timeout_stop_reason(self, classifier: FailureIntelligence, now: datetime) -> None:
        """timeout via stop_reason='timeout'."""
        terminal = WorkerTerminal(ok=False, stop_reason="timeout", error="")
        result = classifier.classify(terminal, now=now)
        assert result.category == "timeout"

    def test_connection_refused(self, classifier: FailureIntelligence, now: datetime) -> None:
        """'connection refused' text pattern."""
        terminal = WorkerTerminal(ok=False, error="Connection refused to server")
        result = classifier.classify(terminal, now=now)
        assert result.category == "transport_temporary"

    def test_connection_reset(self, classifier: FailureIntelligence, now: datetime) -> None:
        """'connection reset' text pattern."""
        terminal = WorkerTerminal(ok=False, error="Connection reset by peer")
        result = classifier.classify(terminal, now=now)
        assert result.category == "transport_temporary"

    def test_no_final_answer(self, classifier: FailureIntelligence, now: datetime) -> None:
        """no_final_answer error or stop_reason check."""
        terminal = WorkerTerminal(ok=False, error="no_final_answer")
        result = classifier.classify(terminal, now=now)
        assert result.category == "no_final_answer"
        assert result.action == "REROUTE"
        assert result.cooldown_seconds == 300

    def test_no_final_answer_stop_reason(
        self, classifier: FailureIntelligence, now: datetime
    ) -> None:
        """stop_reason not in ('stop', 'end_turn')."""
        terminal = WorkerTerminal(ok=False, stop_reason="max_tokens", error="")
        result = classifier.classify(terminal, now=now)
        assert result.category == "no_final_answer"

    def test_malformed_response(self, classifier: FailureIntelligence, now: datetime) -> None:
        """'malformed' text pattern."""
        terminal = WorkerTerminal(ok=False, error="Malformed JSON in response")
        result = classifier.classify(terminal, now=now)
        assert result.category == "malformed_response"
        assert result.cooldown_seconds == 300

    def test_model_mismatch(self, classifier: FailureIntelligence, now: datetime) -> None:
        """model_mismatch error."""
        terminal = WorkerTerminal(ok=False, error="model_mismatch")
        result = classifier.classify(terminal, now=now)
        assert result.category == "model_mismatch"
        assert result.scope == "route"
        assert result.cooldown_seconds == 3600

    def test_verification_failed(self, classifier: FailureIntelligence, now: datetime) -> None:
        """verification_failed error -> CORRECT_IMPLEMENTATION."""
        terminal = WorkerTerminal(ok=False, error="verification_failed: test output mismatch")
        result = classifier.classify(terminal, now=now)
        assert result.category == "verification_failed"
        assert result.action == "CORRECT_IMPLEMENTATION"
        assert result.scope == "none"
        assert result.cooldown_seconds == 0

    def test_ownership_violation(self, classifier: FailureIntelligence, now: datetime) -> None:
        """ownership_violation error -> REHYDRATE."""
        terminal = WorkerTerminal(ok=False, error="ownership_violation")
        result = classifier.classify(terminal, now=now)
        assert result.category == "ownership_violation"
        assert result.action == "REHYDRATE"
        assert result.scope == "none"
        assert result.cooldown_seconds == 0


class TestSuccessAndEmpty:
    """Test success cases and empty output."""

    def test_success_ok_true(self, classifier: FailureIntelligence, now: datetime) -> None:
        """ok=True with output -> success (though we don't classify success)."""
        terminal = WorkerTerminal(ok=True, output="Successfully completed")
        result = classifier.classify(terminal, now=now)
        # Success should not appear as a classification
        # Return should be benign (BLOCK action when ok)
        assert result.category == "success"

    def test_empty_output_ok_true(self, classifier: FailureIntelligence, now: datetime) -> None:
        """ok=True but output is empty/whitespace -> empty_output."""
        for output in ["", "   ", "\n\t"]:
            terminal = WorkerTerminal(ok=True, output=output)
            result = classifier.classify(terminal, now=now)
            assert result.category == "empty_output"
            assert result.action == "REROUTE"
            assert result.cooldown_seconds == 300

    def test_empty_output_ok_false(self, classifier: FailureIntelligence, now: datetime) -> None:
        """ok=False with empty output -> depends on error or status."""
        terminal = WorkerTerminal(ok=False, output="", error="")
        result = classifier.classify(terminal, now=now)
        # Falls through to unknown
        assert result.category == "unknown"


class TestResetHintParsing:
    """Test parsing of reset hints from error text."""

    def test_retry_after_colon_format(self, classifier: FailureIntelligence, now: datetime) -> None:
        """'retry-after: N' format."""
        terminal = WorkerTerminal(ok=False, status_code=429, error="Rate limited. retry-after: 180")
        result = classifier.classify(terminal, now=now)
        assert result.cooldown_seconds == 180

    def test_retry_after_seconds_format(
        self, classifier: FailureIntelligence, now: datetime
    ) -> None:
        """'retry after N seconds' format."""
        terminal = WorkerTerminal(ok=False, status_code=429, error="Retry after 90 seconds please")
        result = classifier.classify(terminal, now=now)
        assert result.cooldown_seconds == 90

    def test_reset_in_hours_minutes(self, classifier: FailureIntelligence, now: datetime) -> None:
        """'reset in Nh Nm' format."""
        terminal = WorkerTerminal(ok=False, status_code=429, error="Quota resets in 2h 30m")
        result = classifier.classify(terminal, now=now)
        expected = 2 * 3600 + 30 * 60
        assert result.cooldown_seconds == expected

    def test_try_again_in_minutes(self, classifier: FailureIntelligence, now: datetime) -> None:
        """'try again in N minutes' format."""
        terminal = WorkerTerminal(
            ok=False, status_code=429, error="Quota exceeded. Try again in 45 minutes"
        )
        result = classifier.classify(terminal, now=now)
        assert result.cooldown_seconds == 45 * 60

    def test_cooldown_cap_at_24h(self, classifier: FailureIntelligence, now: datetime) -> None:
        """Reset hints are capped at MAX_COOLDOWN_SECONDS (86400)."""
        terminal = WorkerTerminal(ok=False, status_code=429, error="Reset in 100h 0m")
        result = classifier.classify(terminal, now=now)
        assert result.cooldown_seconds == MAX_COOLDOWN_SECONDS


class TestSecretRedaction:
    """Test that secrets are redacted from evidence."""

    def test_bearer_token_redacted(self, classifier: FailureIntelligence, now: datetime) -> None:
        """Bearer tokens are redacted."""
        terminal = WorkerTerminal(
            ok=False,
            error="Authorization failed: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4gRG9lIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U",
        )
        result = classifier.classify(terminal, now=now)
        assert "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9" not in result.evidence
        assert "REDACTED" in result.evidence

    def test_api_key_sk_format_redacted(
        self, classifier: FailureIntelligence, now: datetime
    ) -> None:
        """sk-* API keys are redacted."""
        terminal = WorkerTerminal(
            ok=False, error="Invalid key sk-proj-aBcDefgHijklmNopqrst1234567890"
        )
        result = classifier.classify(terminal, now=now)
        assert "sk-proj-aBcdefgHijklmNopqrst1234567890" not in result.evidence.lower()
        assert "REDACTED" in result.evidence

    def test_evidence_truncated_300_chars(
        self, classifier: FailureIntelligence, now: datetime
    ) -> None:
        """Evidence is truncated to 300 characters."""
        long_error = "x" * 500
        terminal = WorkerTerminal(ok=False, error=long_error)
        result = classifier.classify(terminal, now=now)
        assert len(result.evidence) <= 300


class TestStatusCodePrecedence:
    """Verify status code takes precedence over text patterns."""

    def test_status_code_overrides_text_pattern(
        self, classifier: FailureIntelligence, now: datetime
    ) -> None:
        """Explicit status code 400 overrides 'timeout' in text."""
        terminal = WorkerTerminal(
            ok=False, status_code=400, error="This request timed out (somehow status 400)"
        )
        result = classifier.classify(terminal, now=now)
        # Status code 400 generic -> bad_request (not timeout)
        assert result.category == "bad_request"


class TestRecoveryBudget:
    """Test RecoveryBudget.decide() method."""

    @pytest.fixture
    def budget(self) -> RecoveryBudget:
        """Default recovery budget."""
        return RecoveryBudget(
            max_attempts_per_node=4, max_same_route_retries=0, max_verification_repairs=1
        )

    def test_empty_history_reassign(self, budget: RecoveryBudget) -> None:
        """Empty history -> REASSIGN."""
        action, reason = budget.decide("node1", [])
        assert action == "REASSIGN"
        assert "first attempt" in reason

    def test_last_action_block_fail_closed(self, budget: RecoveryBudget) -> None:
        """Last action is BLOCK -> FAIL_CLOSED."""
        history = [
            FailureClassification(
                category="unknown", action="BLOCK", cooldown_seconds=0, scope="none"
            )
        ]
        action, reason = budget.decide("node1", history)
        assert action == "FAIL_CLOSED"
        assert "BLOCK" in reason

    def test_exhausted_attempts(self, budget: RecoveryBudget) -> None:
        """Max attempts reached -> FAIL_CLOSED."""
        history = [
            FailureClassification(
                category="timeout", action="REROUTE", cooldown_seconds=120, scope="route"
            )
            for _ in range(4)
        ]
        action, reason = budget.decide("node1", history)
        assert action == "FAIL_CLOSED"
        assert "exhausted" in reason.lower()

    def test_verification_repair_allowed(self, budget: RecoveryBudget) -> None:
        """verification_failed category -> REPAIR (first time)."""
        history = [
            FailureClassification(
                category="verification_failed",
                action="CORRECT_IMPLEMENTATION",
                cooldown_seconds=0,
                scope="none",
            )
        ]
        action, _ = budget.decide("node1", history)
        assert action == "REPAIR"

    def test_verification_repairs_exhausted(self, budget: RecoveryBudget) -> None:
        """max_verification_repairs exceeded -> FAIL_CLOSED."""
        history = [
            FailureClassification(
                category="verification_failed",
                action="CORRECT_IMPLEMENTATION",
                cooldown_seconds=0,
                scope="none",
            ),
            FailureClassification(
                category="verification_failed",
                action="CORRECT_IMPLEMENTATION",
                cooldown_seconds=0,
                scope="none",
            ),
        ]
        action, reason = budget.decide("node1", history)
        assert action == "FAIL_CLOSED"
        assert "verification" in reason.lower()

    def test_normal_reassign(self, budget: RecoveryBudget) -> None:
        """Normal failure -> REASSIGN."""
        history = [
            FailureClassification(
                category="rate_limited", action="REROUTE", cooldown_seconds=60, scope="provider"
            )
        ]
        action, _ = budget.decide("node1", history)
        assert action == "REASSIGN"


class TestClassifierVersion:
    """Verify classifier version constant is present."""

    def test_classifier_version_exists(self) -> None:
        """CLASSIFIER_VERSION is defined."""
        assert CLASSIFIER_VERSION == "v1"
