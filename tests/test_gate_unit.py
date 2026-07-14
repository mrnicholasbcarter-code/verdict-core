"""Unit tests for the Gate compatibility wrapper."""

from __future__ import annotations

from llm_gate.gate import TIER_MAP, Gate
from llm_gate.models import ProviderConfig, RoutingDecision


class FakeIntelligence:
    def __init__(self, decision: RoutingDecision) -> None:
        self.decision = decision
        self.calls: list[tuple[str, str, dict[str, object] | None]] = []

    def route(
        self,
        task: str,
        criticality: str = "medium",
        context: dict[str, object] | None = None,
    ) -> RoutingDecision:
        self.calls.append((task, criticality, context))
        return self.decision


class TestTierMap:
    def test_critical_is_zero(self) -> None:
        assert TIER_MAP["critical"] == 0

    def test_high_is_one(self) -> None:
        assert TIER_MAP["high"] == 1

    def test_medium_is_two(self) -> None:
        assert TIER_MAP["medium"] == 2

    def test_low_is_three(self) -> None:
        assert TIER_MAP["low"] == 3


class TestGateInit:
    def test_default_primary_model(self) -> None:
        gate = Gate()
        assert gate.primary_model == "anthropic/claude-3-opus-20240229"

    def test_custom_primary_model(self) -> None:
        gate = Gate(primary_model="openai/gpt-4")
        assert gate.primary_model == "openai/gpt-4"

    def test_empty_providers(self) -> None:
        gate = Gate()
        assert gate.providers == {}

    def test_custom_providers(self) -> None:
        providers = {"groq": ProviderConfig(base_url="https://api.groq.com/openai/v1")}
        gate = Gate(providers=providers)
        assert "groq" in gate.providers

    def test_log_path_default(self) -> None:
        gate = Gate()
        assert gate.log_path == "llm-gate-decisions.jsonl"


class TestGateRouting:
    def test_route_delegates_to_intelligence_service(self) -> None:
        decision = RoutingDecision(model="chosen", provider="mock", tier=2, reason="ok")
        intelligence = FakeIntelligence(decision)
        gate = Gate(intelligence_service=intelligence)

        routed = gate.route("build docs", criticality="low", context={"request_id": "r1"})

        assert routed is decision
        assert intelligence.calls == [("build docs", "low", {"request_id": "r1"})]

    def test_route_preserves_model_and_reason_on_denial(self) -> None:
        decision = RoutingDecision(
            model="primary-model",
            provider="primary",
            tier=2,
            reason="protected work rejected",
            decision="denied",
        )
        intelligence = FakeIntelligence(decision)
        gate = Gate(intelligence_service=intelligence)

        routed = gate.route("deploy production", criticality="critical")

        assert routed.decision == "denied"
        assert routed.reason == "protected work rejected"
        assert routed.model == "primary-model"
