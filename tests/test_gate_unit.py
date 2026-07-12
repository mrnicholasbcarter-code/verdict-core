"""Unit tests for the Gate routing engine."""

import contextlib
from unittest.mock import MagicMock, patch

from llm_gate.gate import TIER_MAP, Gate
from llm_gate.models import ProviderConfig


class TestTierMap:
    def test_critical_is_zero(self):
        assert TIER_MAP["critical"] == 0

    def test_high_is_one(self):
        assert TIER_MAP["high"] == 1

    def test_medium_is_two(self):
        assert TIER_MAP["medium"] == 2

    def test_low_is_three(self):
        assert TIER_MAP["low"] == 3


class TestGateInit:
    def test_default_primary_model(self):
        gate = Gate()
        assert gate.primary_model == "anthropic/claude-3-opus-20240229"

    def test_custom_primary_model(self):
        gate = Gate(primary_model="openai/gpt-4")
        assert gate.primary_model == "openai/gpt-4"

    def test_empty_providers(self):
        gate = Gate()
        assert gate.providers == {}

    def test_custom_providers(self):
        providers = {"groq": ProviderConfig(base_url="https://api.groq.com/openai/v1")}
        gate = Gate(providers=providers)
        assert "groq" in gate.providers

    def test_log_path_default(self):
        gate = Gate()
        assert gate.log_path == "llm-gate-decisions.jsonl"


class TestGateRouting:
    @patch("llm_gate.gate.scan", return_value=(None, ""))
    @patch("llm_gate.gate.log_decision")
    def test_critical_never_offloads(self, mock_log, mock_scan):
        gate = Gate(primary_model="anthropic/claude-3-opus-20240229")
        dec = gate.route("deploy to production", criticality="critical")
        assert dec.model == "anthropic/claude-3-opus-20240229"
        assert dec.provider == "primary"
        assert dec.tier == 0
        assert "critical" in dec.reason

    @patch("llm_gate.gate.scan", return_value=(None, ""))
    @patch("llm_gate.gate.log_decision")
    def test_low_criticality_falls_back_without_providers(self, mock_log, mock_scan):
        gate = Gate(primary_model="anthropic/claude-3-opus-20240229")
        dec = gate.route("format this json", criticality="low")
        assert dec.model == "anthropic/claude-3-opus-20240229"
        assert "fallback" in dec.reason

    @patch("llm_gate.gate.scan", return_value=(0, "keyword: deploy"))
    @patch("llm_gate.gate.log_decision")
    def test_escalation_bumps_to_critical(self, mock_log, mock_scan):
        gate = Gate(primary_model="anthropic/claude-3-opus-20240229")
        dec = gate.route("deploy the database migration", criticality="low")
        assert dec.tier == 0
        assert dec.escalated is True

    @patch("llm_gate.gate.scan", return_value=(None, ""))
    @patch("llm_gate.gate.log_decision")
    def test_latency_is_recorded(self, mock_log, mock_scan):
        gate = Gate()
        dec = gate.route("test task")
        assert dec.latency_ms > 0

    @patch("llm_gate.gate.scan", return_value=(None, ""))
    @patch("llm_gate.gate.log_decision")
    def test_logged_flag_set(self, mock_log, mock_scan):
        gate = Gate(log_path="test.jsonl")
        dec = gate.route("test")
        assert dec.logged is True

    @patch("llm_gate.gate.scan", return_value=(None, ""))
    @patch("llm_gate.gate.log_decision")
    def test_logged_flag_false_when_no_path(self, mock_log, mock_scan):
        gate = Gate(log_path="")
        dec = gate.route("test")
        assert dec.logged is False


class TestGateWithProviders:
    @patch("llm_gate.gate.scan", return_value=(None, ""))
    @patch("llm_gate.gate.log_decision")
    @patch("llm_gate.gate.fetch_models")
    @patch("llm_gate.gate.select_best_model")
    def test_routes_to_provider_when_available(self, mock_select, mock_fetch, mock_log, mock_scan):
        mock_model = MagicMock()
        mock_model.id = "groq/llama-3"
        mock_model.capability_tier = 3
        mock_model.provider = "groq"
        mock_fetch.return_value = [mock_model]
        mock_select.return_value = (mock_model, [])

        providers = {"groq": ProviderConfig(base_url="https://api.groq.com/openai/v1")}
        gate = Gate(providers=providers)
        dec = gate.route("simple formatting task", criticality="low")
        assert dec.model == "groq/llama-3"
        assert dec.provider == "groq"

    @patch("llm_gate.gate.scan", return_value=(None, ""))
    @patch("llm_gate.gate.log_decision")
    @patch("llm_gate.gate.fetch_models")
    @patch("llm_gate.gate.select_best_model")
    def test_falls_back_when_no_candidate_matches(
        self, mock_select, mock_fetch, mock_log, mock_scan
    ):
        mock_fetch.return_value = []
        mock_select.return_value = (None, [])

        providers = {"groq": ProviderConfig(base_url="https://api.groq.com/openai/v1")}
        gate = Gate(primary_model="anthropic/claude-3-opus-20240229", providers=providers)
        dec = gate.route("complex task", criticality="medium")
        assert dec.model == "anthropic/claude-3-opus-20240229"
        assert "fallback" in dec.reason

    @patch("llm_gate.gate.scan", return_value=(None, ""))
    @patch("llm_gate.gate.log_decision")
    @patch("urllib.request.urlopen")
    def test_fallback_on_429_http_error(self, mock_urlopen, mock_log, mock_scan):
        from urllib.error import HTTPError

        import llm_gate.discovery

        llm_gate.discovery._CACHE.clear()

        mock_urlopen.side_effect = HTTPError("http://fake.api", 429, "Too Many Requests", {}, None)

        providers = {"openrouter": ProviderConfig(base_url="https://fake.api/v1")}
        gate = Gate(primary_model="anthropic/claude-3-opus-20240229", providers=providers)

        dec = gate.route("do something", criticality="low")

        assert dec.model == "anthropic/claude-3-opus-20240229"
        assert dec.provider == "primary"
        assert "fallback" in dec.reason

    @patch("llm_gate.gate.scan", return_value=(None, ""))
    @patch("llm_gate.gate.log_decision")
    @patch("urllib.request.urlopen")
    def test_fallback_on_529_http_error(self, mock_urlopen, mock_log, mock_scan):
        from urllib.error import HTTPError

        import llm_gate.discovery

        llm_gate.discovery._CACHE.clear()

        mock_urlopen.side_effect = HTTPError("http://fake.api", 529, "Overloaded", {}, None)

        providers = {"openrouter": ProviderConfig(base_url="https://fake.api/v1")}
        gate = Gate(primary_model="anthropic/claude-3-opus-20240229", providers=providers)

        dec = gate.route("do something", criticality="low")

        assert dec.model == "anthropic/claude-3-opus-20240229"
        assert dec.provider == "primary"
        assert "fallback" in dec.reason

    @patch("llm_gate.gate.scan", return_value=(None, ""))
    @patch("llm_gate.gate.log_decision")
    @patch("urllib.request.urlopen")
    def test_fallback_on_403_forbidden_from_anthropic(self, mock_urlopen, mock_log, mock_scan):
        """An explicit Anthropic 403 (forbidden / blocked API key) must not crash the
        gate: discovery fails closed and routing falls back to the primary model."""
        from urllib.error import HTTPError

        import llm_gate.discovery

        llm_gate.discovery._CACHE.clear()

        mock_urlopen.side_effect = HTTPError(
            "https://api.anthropic.com/v1/models", 403, "Forbidden", {}, None
        )

        # A real Anthropic provider entry whose discovery call is mocked to 403.
        providers = {"anthropic": ProviderConfig(base_url="https://api.anthropic.com/v1")}
        gate = Gate(primary_model="anthropic/claude-3-opus-20240229", providers=providers)

        dec = gate.route("do something sensitive", criticality="low")

        # Discovery raised 403 -> no candidates -> safe fallback to primary.
        assert dec.model == "anthropic/claude-3-opus-20240229"
        assert dec.provider == "primary"
        assert "fallback" in dec.reason

    @patch("llm_gate.gate.scan", return_value=(None, ""))
    @patch("llm_gate.gate.log_decision")
    @patch("urllib.request.urlopen")
    def test_fallback_on_403_connection_error_from_anthropic(
        self, mock_urlopen, mock_log, mock_scan
    ):
        """An Anthropic 403 surfaced as a connection-level error (e.g. proxy block)
        must also fall back to the primary model without raising."""
        import urllib.error

        import llm_gate.discovery

        llm_gate.discovery._CACHE.clear()

        # Some stacks wrap a 403 in a URLError/ConnectionError carrying the code.
        err = urllib.error.URLError("403 Forbidden")
        with contextlib.suppress(AttributeError):
            err.code = 403  # type: ignore[attr-defined]
        mock_urlopen.side_effect = err

        providers = {"anthropic": ProviderConfig(base_url="https://api.anthropic.com/v1")}
        gate = Gate(primary_model="anthropic/claude-3-opus-20240229", providers=providers)

        dec = gate.route("do something sensitive", criticality="medium")

        assert dec.model == "anthropic/claude-3-opus-20240229"
        assert dec.provider == "primary"
        assert "fallback" in dec.reason
