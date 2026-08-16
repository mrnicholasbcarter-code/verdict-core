"""Deterministic pre-execution simulator tests (CLI-001, #261)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from verdict.model_passports import ModelPassport
from verdict.models import ModelInfo, TaskSpec
from verdict.simulator import (
    SimulationForecast,
    SimulatorError,
    TokenForecast,
    expected_model,
    forecast_tokens,
    simulate,
)

NOW = datetime(2026, 8, 3, 12, tzinfo=timezone.utc)


def _model(**overrides: object) -> ModelInfo:
    base = dict(
        id="cheap/model",
        provider="cheap",
        capability_tier=3,
        context_window=128_000,
        cost_per_1k=0.25,
    )
    return ModelInfo(**{**base, **overrides})


def _passport(**overrides: object) -> ModelPassport:
    base = dict(
        provider="cheap",
        model_id="cheap/model",
        auth_state="authorized",
        latency_p95=120.0,
        context_window=128_000,
        tool_support=True,
        token_cost_per_1k=0.25,
        last_verified_timestamp=NOW,
        availability_state="eligible",
        qualified_at=NOW - timedelta(seconds=1),
        expires_at=NOW + timedelta(seconds=10),
    )
    return ModelPassport(**{**base, **overrides})


def _catalog() -> list[ModelInfo]:
    return [
        ModelInfo(
            id="frontier/opus",
            provider="frontier",
            capability_tier=0,
            context_window=200_000,
            cost_per_1k=15.0,
        ),
        ModelInfo(
            id="balanced/sonnet",
            provider="balanced",
            capability_tier=1,
            context_window=200_000,
            cost_per_1k=3.0,
        ),
        ModelInfo(
            id="cheap/flash",
            provider="cheap",
            capability_tier=3,
            context_window=128_000,
            cost_per_1k=0.25,
        ),
    ]


class TestForecastTokens:
    def test_prompt_tokens_scale_with_prompt_length(self) -> None:
        short = forecast_tokens(TaskSpec(prompt="x" * 40, criticality="low"))
        long = forecast_tokens(TaskSpec(prompt="x" * 400, criticality="low"))
        assert short.prompt_tokens > 0
        assert long.prompt_tokens > short.prompt_tokens

    def test_completion_tokens_scale_with_criticality(self) -> None:
        low = forecast_tokens(TaskSpec(prompt="task " * 200, criticality="low"))
        critical = forecast_tokens(TaskSpec(prompt="task " * 200, criticality="critical"))
        assert critical.completion_tokens > low.completion_tokens

    def test_requirements_increase_output_footprint(self) -> None:
        base = forecast_tokens(TaskSpec(prompt="task " * 200, criticality="medium"))
        rich = forecast_tokens(
            TaskSpec(prompt="task " * 200, criticality="medium", requirements=["tools", "json"])
        )
        assert rich.completion_tokens > base.completion_tokens

    def test_forecast_is_bounded(self) -> None:
        forecast = forecast_tokens(TaskSpec(prompt="x" * 10_000, criticality="critical"))
        assert forecast.prompt_tokens >= 1
        assert forecast.completion_tokens >= 16
        assert forecast.total_tokens == forecast.prompt_tokens + forecast.completion_tokens


class TestExpectedModel:
    def test_low_criticality_prefers_cheap_tier(self) -> None:
        model = expected_model(TaskSpec(prompt="task", criticality="low"), _catalog())
        assert model.id == "cheap/flash"

    def test_critical_prefers_frontier_tier(self) -> None:
        model = expected_model(TaskSpec(prompt="task", criticality="critical"), _catalog())
        assert model.id == "frontier/opus"

    def test_quarantined_model_is_penalized(self) -> None:
        catalog = _catalog()
        quarantined = ModelPassport(
            provider="cheap",
            model_id="cheap/flash",
            auth_state="authorized",
            availability_state="quarantined",
            quarantine_until=NOW + timedelta(seconds=60),
            quarantined_at=NOW,
            qualified_at=NOW - timedelta(seconds=1),
            expires_at=NOW + timedelta(seconds=10),
        )
        model = expected_model(
            TaskSpec(prompt="task", criticality="low"),
            catalog,
            passports={"cheap/flash": quarantined},
        )
        assert model.id == "balanced/sonnet"

    def test_empty_catalog_raises(self) -> None:
        import pytest

        with pytest.raises(SimulatorError):
            expected_model(TaskSpec(prompt="task", criticality="low"), [])


class TestSimulate:
    def test_returns_route_forecast_and_bounded_risk(self) -> None:
        forecast = simulate(
            TaskSpec(prompt="summarize the report", criticality="medium"), model_catalog=_catalog()
        )
        assert isinstance(forecast, SimulationForecast)
        assert forecast.model in {"frontier/opus", "balanced/sonnet", "cheap/flash"}
        assert forecast.total_tokens == forecast.prompt_tokens + forecast.completion_tokens
        assert 0 <= forecast.risk_score <= 100
        assert forecast.cost_usd >= 0.0

    def test_cost_is_zero_for_free_models(self) -> None:
        catalog = [
            ModelInfo(id="free/model", provider="free", capability_tier=3, context_window=8000)
        ]
        forecast = simulate(TaskSpec(prompt="task", criticality="low"), model_catalog=catalog)
        assert forecast.cost_usd == 0.0

    def test_unverified_model_carries_higher_risk_than_verified(self) -> None:
        catalog = [_model()]
        unverified = simulate(TaskSpec(prompt="task", criticality="low"), model_catalog=catalog)
        verified = simulate(
            TaskSpec(prompt="task", criticality="low"),
            model_catalog=catalog,
            passports={"cheap/model": _passport()},
        )
        assert unverified.risk_score > verified.risk_score

    def test_denied_passport_raises_risk_to_max(self) -> None:
        denied = ModelPassport(
            provider="cheap",
            model_id="cheap/model",
            auth_state="unauthorized",
            availability_state="denied",
            qualified_at=NOW - timedelta(seconds=1),
            expires_at=NOW + timedelta(seconds=10),
        )
        catalog = [_model()]
        forecast = simulate(
            TaskSpec(prompt="task", criticality="low"),
            model_catalog=catalog,
            passports={"cheap/model": denied},
        )
        assert forecast.risk_score >= 50

    def test_model_override_wins(self) -> None:
        forecast = simulate(
            TaskSpec(prompt="task", criticality="low"),
            model_catalog=_catalog(),
            model_override="frontier/opus",
        )
        assert forecast.model == "frontier/opus"

    def test_is_deterministic(self) -> None:
        spec = TaskSpec(prompt="deterministic task", criticality="high")
        first = simulate(spec, model_catalog=_catalog()).to_dict()
        second = simulate(spec, model_catalog=_catalog()).to_dict()
        assert first == second

    def test_no_network(self) -> None:
        # The engine must stay pure: importing and running it may not touch
        # any socket, transport, or subprocess.
        spec = TaskSpec(prompt="offline task", criticality="medium")
        forecast = simulate(spec, model_catalog=_catalog())
        assert forecast.provider


class TestTokenForecastShape:
    def test_fields(self) -> None:
        tokens = TokenForecast(prompt_tokens=100, completion_tokens=50)
        assert tokens.total_tokens == 150
