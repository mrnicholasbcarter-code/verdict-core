"""Unit tests for data models."""

import pytest

from llm_gate.models import ProviderConfig, RoutingDecision


class TestRoutingDecision:
    def test_creation(self):
        dec = RoutingDecision(model="test/model", provider="test", tier=2, reason="unit test")
        assert dec.model == "test/model"
        assert dec.tier == 2

    def test_frozen(self):
        dec = RoutingDecision(model="x", provider="y", tier=0, reason="z")
        with pytest.raises(AttributeError):
            dec.model = "changed"

    def test_defaults(self):
        dec = RoutingDecision(model="x", provider="y", tier=0, reason="z")
        assert dec.escalated is False
        assert dec.logged is False
        assert dec.latency_ms == 0.0
        assert dec.decision == "selected"
        assert dec.transport_outcome == "not_sent"
        assert dec.quality_outcome == "unknown"


class TestProviderConfig:
    def test_creation(self):
        cfg = ProviderConfig(base_url="http://localhost:11434/v1")
        assert cfg.base_url == "http://localhost:11434/v1"

    def test_defaults(self):
        cfg = ProviderConfig(base_url="http://localhost")
        assert cfg.api_key is None
        assert cfg.priority == 0
