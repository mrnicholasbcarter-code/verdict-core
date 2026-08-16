"""Deterministic pre-execution simulator for Verdict routing (CLI-001, #261).

Pi.dev-inspired: forecast what a task *would* cost and how risky it would be
*before* paying for a model call.  ``simulate`` derives an expected token
footprint from the task spec, prices it from the model catalog / passports,
scores execution risk in [0, 100], and returns the expected model route.
No provider API, probe, or network transport is ever invoked.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from verdict.model_passports import (
    CAPACITY_CONFIDENCE_THRESHOLD,
    LARGE_TASK_TOKENS,
    ModelPassport,
    estimate_capacity_confidence,
)
from verdict.models import ModelInfo, TaskSpec

#: Criticality -> target capability tier (0 = most capable, 3 = cheapest).
TIER_BY_CRITICALITY: dict[str, int] = {"critical": 0, "high": 1, "medium": 2, "low": 3}

#: Approximate characters that map to a single input token.
CHARS_PER_TOKEN = 4

_AVAIL_PENALTY = {"eligible": 0, "degraded": 1, "quarantined": 2, "denied": 3}
_AUTH_PENALTY = {"authorized": 0, "unknown": 1, "unauthorized": 2}
_COMPLETION_FACTOR = {"critical": 3.0, "high": 2.4, "medium": 1.8, "low": 1.2}


class SimulatorError(ValueError):
    """Raised when a simulation cannot be produced for the supplied inputs."""


@dataclass(frozen=True)
class TokenForecast:
    """Deterministic input/output token estimate for a task."""

    prompt_tokens: int
    completion_tokens: int

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


@dataclass(frozen=True)
class SimulationForecast:
    """Full pre-execution forecast: route, tokens, cost, and risk."""

    model: str
    provider: str
    tier: int
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float
    risk_score: int
    capacity_confidence: float
    rationale: str

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def to_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "provider": self.provider,
            "tier": self.tier,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "cost_usd": self.cost_usd,
            "risk_score": self.risk_score,
            "capacity_confidence": self.capacity_confidence,
            "rationale": self.rationale,
        }


def forecast_tokens(task: TaskSpec) -> TokenForecast:
    """Estimate input/output tokens deterministically from the task spec."""
    prompt_tokens = max(1, len(task.prompt) // CHARS_PER_TOKEN)
    complexity = 1.0 + 0.5 * len(task.requirements)
    factor = _COMPLETION_FACTOR.get(task.criticality, 1.8)
    completion_tokens = max(16, round(prompt_tokens * factor * complexity * 0.5))
    return TokenForecast(prompt_tokens, completion_tokens)


def _passport_for(model: ModelInfo, passports: dict[str, ModelPassport]) -> ModelPassport | None:
    if model.id in passports:
        return passports[model.id]
    return passports.get(f"{model.provider}/{model.id}")


def _route_score(model: ModelInfo, target: int, passport: ModelPassport | None) -> float:
    """Deterministic composite route score; lower is preferred.

    Tier distance dominates, then availability, then auth state, then price.
    A quarantined/denied model must lose even to a tier-mismatched eligible one.
    """
    avail = passport.availability_state if passport else model.availability_state
    auth = passport.auth_state if passport else "authorized"
    cost = model.cost_per_1k
    return (
        10.0 * abs(model.capability_tier - target)
        + _AVAIL_PENALTY.get(avail, 2) * 25.0
        + _AUTH_PENALTY.get(auth, 1) * 20.0
        + (
            min(float(cost), 100.0) * 0.01
            if isinstance(cost, (int, float)) and not isinstance(cost, bool)
            else 0.0
        )
    )


def _cost_per_1k(model: ModelInfo, passport: ModelPassport | None) -> float | None:
    """Resolve the per-1k-token price, preferring the catalog, then the passport."""
    cost = model.cost_per_1k
    if isinstance(cost, (int, float)) and not isinstance(cost, bool) and cost > 0:
        return float(cost)
    if passport is not None and passport.token_cost_per_1k is not None:
        return float(passport.token_cost_per_1k)
    return None


def expected_model(
    task: TaskSpec,
    model_catalog: list[ModelInfo],
    *,
    passports: dict[str, ModelPassport] | None = None,
) -> ModelInfo:
    """Select the expected model for the task without invoking any transport.

    The closest capability tier to the criticality target wins, breaking ties
    on availability, auth state, then price.  Deterministic for a fixed input.
    """
    if not model_catalog:
        raise SimulatorError("model catalog is empty; cannot forecast a route")
    target = TIER_BY_CRITICALITY.get(task.criticality, 2)
    known = passports or {}
    return min(model_catalog, key=lambda m: _route_score(m, target, _passport_for(m, known)))


def _capacity_confidence(model: ModelInfo, *, tokens: int) -> float:
    return estimate_capacity_confidence(model, estimated_tokens=tokens)


def _risk_score(
    model: ModelInfo,
    passport: ModelPassport | None,
    *,
    estimated_tokens: int,
    capacity_confidence: float,
) -> int:
    """Score execution risk in [0, 100]; higher is riskier."""
    avail = passport.availability_state if passport else model.availability_state
    auth = passport.auth_state if passport else "authorized"
    score = 0.0
    score += _AVAIL_PENALTY.get(avail, 2) * 30.0
    score += _AUTH_PENALTY.get(auth, 1) * 25.0
    if passport is None:
        score += 15.0  # unverified passport -> moderate uncertainty
    if capacity_confidence < CAPACITY_CONFIDENCE_THRESHOLD and estimated_tokens > LARGE_TASK_TOKENS:
        score += 40.0
    if passport is not None and passport.latency_p95 is not None:
        if passport.latency_p95 > 3000:
            score += 20.0
        elif passport.latency_p95 > 1000:
            score += 10.0
    return max(0, min(100, round(score)))


def simulate(
    task: TaskSpec,
    *,
    model_catalog: list[ModelInfo],
    passports: dict[str, ModelPassport] | None = None,
    model_override: str | None = None,
) -> SimulationForecast:
    """Produce the full pre-execution forecast for one task. No network access."""
    known = passports or {}
    if model_override is not None:
        matches = [
            m
            for m in model_catalog
            if m.id == model_override or f"{m.provider}/{m.id}" == model_override
        ]
        if matches:
            model = matches[0]
        else:
            from verdict.classifier import classify

            provider = model_override.split("/", 1)[0] if "/" in model_override else "unknown"
            model = ModelInfo(
                id=model_override, provider=provider, capability_tier=classify(model_override)
            )
            model_catalog = [*model_catalog, model]
    else:
        model = expected_model(task, model_catalog, passports=known)

    passport = _passport_for(model, known)
    forecast = forecast_tokens(task)
    capacity = _capacity_confidence(model, tokens=forecast.total_tokens)
    cost = _cost_per_1k(model, passport)
    cost_usd = round(forecast.total_tokens / 1000.0 * cost, 6) if cost else 0.0
    risk = _risk_score(
        model, passport, estimated_tokens=forecast.total_tokens, capacity_confidence=capacity
    )
    target = TIER_BY_CRITICALITY.get(task.criticality, 2)
    rationale = (
        f"criticality={task.criticality} targets T{target}; capacity_confidence={capacity:.2f}"
    )
    return SimulationForecast(
        model=model.id,
        provider=model.provider,
        tier=model.capability_tier,
        prompt_tokens=forecast.prompt_tokens,
        completion_tokens=forecast.completion_tokens,
        cost_usd=cost_usd,
        risk_score=risk,
        capacity_confidence=capacity,
        rationale=rationale,
    )


__all__ = [
    "CHARS_PER_TOKEN",
    "TIER_BY_CRITICALITY",
    "SimulationForecast",
    "SimulatorError",
    "TokenForecast",
    "expected_model",
    "forecast_tokens",
    "simulate",
]
