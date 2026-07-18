"""Coverage-focused tests for public helper APIs used by the CLI."""

from __future__ import annotations

import pytest

from llm_gate.classifier import classify
from llm_gate.headroom import check_headroom
from llm_gate.models import ModelInfo, ProviderConfig
from llm_gate.router import select_best_model


def test_classifier_exact_override_wins() -> None:
    assert classify("provider/custom-model", {"provider/custom-model": 0}) == 0


def test_classifier_matches_known_tiers_and_defaults() -> None:
    assert classify("anthropic/claude-3-opus-20240229") == 0
    assert classify("openai/gpt-5.4") == 1
    assert classify("openai/gpt-4o-mini") == 2
    assert classify("google/gemini-2.0-flash") == 3
    assert classify("unknown/provider-model") == 2


def test_select_best_model_prefers_highest_quality_eligible_model() -> None:
    candidates = [
        ModelInfo(id="primary", provider="frontier", capability_tier=0, is_available=True),
        ModelInfo(id="cheap-b", provider="cheap", capability_tier=3, is_available=True),
        ModelInfo(id="cheap-a", provider="cheap", capability_tier=3, is_available=True),
        ModelInfo(id="offline", provider="cheap", capability_tier=3, is_available=False),
    ]
    configs = {
        "frontier": ProviderConfig(base_url="https://frontier.example", priority=10),
        "cheap": ProviderConfig(base_url="https://cheap.example", priority=1),
    }

    chosen, alternatives = select_best_model(candidates, tier=3, configs=configs)

    assert chosen is not None
    assert chosen.id == "primary"
    assert alternatives == ["cheap-a", "cheap-b"]


def test_select_best_model_fails_open_with_exhausted_candidates() -> None:
    candidates = [ModelInfo(id="offline", provider="cheap", capability_tier=3, is_available=False)]
    configs = {"cheap": ProviderConfig(base_url="https://cheap.example")}

    chosen, exhausted = select_best_model(candidates, tier=3, configs=configs)

    assert chosen is None
    assert exhausted == ["offline"]


@pytest.mark.parametrize(
    "state",
    [
        "unknown",
        "stale",
        "degraded",
        "denied",
        "rate_limited",
        "quota_exhausted",
        "future_untrusted_state",
    ],
)
def test_select_best_model_filters_runtime_state_before_quality_ranking(state: str) -> None:
    candidates = [
        ModelInfo(
            id="ineligible-high-score",
            provider="provider",
            capability_tier=0,
            is_available=True,
            availability_state=state,
            quality_confidence=1.0,
        ),
        ModelInfo(
            id="eligible-lower-score",
            provider="provider",
            capability_tier=1,
            is_available=True,
            availability_state="ready",
            quality_confidence=0.5,
        ),
    ]
    configs = {"provider": ProviderConfig(base_url="https://provider.example")}

    chosen, alternatives = select_best_model(candidates, tier=3, configs=configs)

    assert chosen is not None
    assert chosen.id == "eligible-lower-score"
    assert alternatives == []


def test_headroom_fails_open_without_endpoint() -> None:
    available, pct = check_headroom(
        "model", "provider", ProviderConfig(base_url="https://provider.example")
    )

    assert available is True
    assert pct == 100.0


def test_headroom_placeholder_allows_configured_endpoint() -> None:
    available, pct = check_headroom(
        "model",
        "provider",
        ProviderConfig(
            base_url="https://provider.example", headroom_endpoint="https://provider.example/usage"
        ),
    )

    assert available is True
    assert pct == 100.0
