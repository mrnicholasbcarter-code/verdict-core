"""Tests for the codiv failure classification and provider identity design failure classification (429/529 handling)."""

import unittest
from datetime import datetime, timezone

from verdict.autodev_routing import (
    RETRY_AFTER_DEFAULT_S,
    RETRY_AFTER_MAX_S,
    OpenAICompatibleEvidenceAdapter,
    parse_retry_after,
)
from verdict.availability import AvailabilityReport
from verdict.gateway_adapter_runtime import AdapterFailureSignal
from verdict.gateway_adapters import NormalizedFailureClass


class TestParseRetryAfter(unittest.TestCase):
    """Test Retry-After header parsing with bounds."""

    def setUp(self):
        self.now = datetime(2026, 9, 25, 12, 0, 0, tzinfo=timezone.utc)

    def test_parse_integer_seconds(self):
        """Parse Retry-After as integer seconds."""
        result = parse_retry_after("60", now=self.now)
        self.assertEqual(result, 60.0)

    def test_clamp_negative_to_min(self):
        """Negative Retry-After clamped to default."""
        result = parse_retry_after("-5", now=self.now)
        self.assertEqual(result, float(RETRY_AFTER_DEFAULT_S))

    def test_clamp_huge_to_max(self):
        """Huge Retry-After clamped to max."""
        result = parse_retry_after("99999999", now=self.now)
        self.assertEqual(result, float(RETRY_AFTER_MAX_S))

    def test_garbage_returns_default(self):
        """Garbage Retry-After returns default."""
        result = parse_retry_after("garbage", now=self.now)
        self.assertEqual(result, float(RETRY_AFTER_DEFAULT_S))

    def test_none_returns_default(self):
        """None Retry-After returns default."""
        result = parse_retry_after(None, now=self.now)
        self.assertEqual(result, float(RETRY_AFTER_DEFAULT_S))

    def test_parse_http_date(self):
        """Parse Retry-After as HTTP-date."""
        # "Wed, 25 Sep 2026 12:01:00 GMT" is 60 seconds from self.now
        result = parse_retry_after("Wed, 25 Sep 2026 12:01:00 GMT", now=self.now)
        self.assertEqual(result, 60.0)

    def test_http_date_in_past_returns_default(self):
        """HTTP-date in past returns default."""
        result = parse_retry_after("Wed, 25 Sep 2026 11:59:00 GMT", now=self.now)
        self.assertEqual(result, float(RETRY_AFTER_DEFAULT_S))


class _AvailabilitySurface:
    """Minimal mock for adapter instantiation."""

    def __init__(self, report: AvailabilityReport):
        self.report = report

    def evaluate(self, *, now: datetime | None = None) -> AvailabilityReport:
        return self.report


class TestNormalizeFailure(unittest.TestCase):
    """Test normalize_failure with the codiv failure classification and provider identity design enhancements."""

    def setUp(self):
        self.test_now = datetime(2026, 9, 25, 12, 0, 0, tzinfo=timezone.utc)

        # Create minimal AvailabilityReport
        report = AvailabilityReport(
            candidates=(), eligible=(), source="test", freshness_seconds=1.0
        )

        # Create adapter with minimal surface
        surface = _AvailabilitySurface(report)
        self.adapter = OpenAICompatibleEvidenceAdapter(
            surface, gateway_id="test-gateway", protocol="openai.chat"
        )

    def test_402_quota_non_retryable(self):
        """402 maps to QUOTA, non-retryable."""
        signal = AdapterFailureSignal(code="quota_exceeded", status_code=402)
        failure = self.adapter.normalize_failure(signal, now=self.test_now)

        self.assertEqual(failure.failure_class, NormalizedFailureClass.QUOTA)
        self.assertFalse(failure.retryable)
        self.assertIsNone(failure.cooldown_seconds)

    def test_429_quota_code_non_retryable(self):
        """429 with quota code maps to QUOTA, non-retryable."""
        signal = AdapterFailureSignal(code="insufficient_quota", status_code=429)
        failure = self.adapter.normalize_failure(signal, now=self.test_now)

        self.assertEqual(failure.failure_class, NormalizedFailureClass.QUOTA)
        self.assertFalse(failure.retryable)
        self.assertIsNone(failure.cooldown_seconds)

    def test_429_rate_limit_with_retry_after(self):
        """429 rate limit with Retry-After is retryable with cooldown."""
        signal = AdapterFailureSignal(code="rate_limit_exceeded", status_code=429, retry_after="30")
        failure = self.adapter.normalize_failure(signal, now=self.test_now)

        self.assertEqual(failure.failure_class, NormalizedFailureClass.RATE_LIMIT)
        self.assertTrue(failure.retryable)
        self.assertEqual(failure.cooldown_seconds, 30.0)

    def test_429_rate_limit_malicious_retry_after_clamped(self):
        """429 with malicious Retry-After is clamped."""
        signal = AdapterFailureSignal(code="rate_limit", status_code=429, retry_after="-5")
        failure = self.adapter.normalize_failure(signal, now=self.test_now)

        self.assertEqual(failure.failure_class, NormalizedFailureClass.RATE_LIMIT)
        self.assertTrue(failure.retryable)
        self.assertEqual(failure.cooldown_seconds, float(RETRY_AFTER_DEFAULT_S))

    def test_429_rate_limit_huge_retry_after_clamped(self):
        """429 with huge Retry-After is clamped to max."""
        signal = AdapterFailureSignal(code="rate_limit", status_code=429, retry_after="999999")
        failure = self.adapter.normalize_failure(signal, now=self.test_now)

        self.assertEqual(failure.failure_class, NormalizedFailureClass.RATE_LIMIT)
        self.assertTrue(failure.retryable)
        self.assertEqual(failure.cooldown_seconds, float(RETRY_AFTER_MAX_S))

    def test_429_rate_limit_http_date(self):
        """429 with HTTP-date Retry-After calculates cooldown."""
        signal = AdapterFailureSignal(
            code="rate_limit",
            status_code=429,
            retry_after="Wed, 25 Sep 2026 12:02:00 GMT",  # 120 seconds from test_now
        )
        failure = self.adapter.normalize_failure(signal, now=self.test_now)

        self.assertEqual(failure.failure_class, NormalizedFailureClass.RATE_LIMIT)
        self.assertTrue(failure.retryable)
        self.assertEqual(failure.cooldown_seconds, 120.0)

    def test_529_overloaded_retryable(self):
        """529 maps to OVERLOADED, retryable (not quality degradation)."""
        signal = AdapterFailureSignal(code="service_overloaded", status_code=529, retry_after="120")
        failure = self.adapter.normalize_failure(signal, now=self.test_now)

        self.assertEqual(failure.failure_class, NormalizedFailureClass.OVERLOADED)
        self.assertTrue(failure.retryable)
        self.assertEqual(failure.cooldown_seconds, 120.0)

    def test_timeout_unchanged(self):
        """Timeout behavior unchanged."""
        signal = AdapterFailureSignal(code="timeout", timed_out=True)
        failure = self.adapter.normalize_failure(signal, now=self.test_now)

        self.assertEqual(failure.failure_class, NormalizedFailureClass.TIMEOUT)
        self.assertTrue(failure.retryable)

    def test_transport_unchanged(self):
        """Transport (no status) behavior unchanged."""
        signal = AdapterFailureSignal(code="network_error", status_code=None)
        failure = self.adapter.normalize_failure(signal, now=self.test_now)

        self.assertEqual(failure.failure_class, NormalizedFailureClass.TRANSPORT)
        self.assertTrue(failure.retryable)


if __name__ == "__main__":
    unittest.main()
