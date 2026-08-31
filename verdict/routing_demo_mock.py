"""Deterministic no-spend scenario for the portfolio routing demo."""

from __future__ import annotations

from typing import Any

from verdict.adaptive_ranker import AdaptiveRanker, AdaptiveRankerConfig, RankerMode
from verdict.eligibility import EligibilityRecord, EligibilityResult, EligibilityVerdict
from verdict.models import ModelInfo, TaskSpec

MODEL_PROFILES = {
    "claude-opus-5": {"input": 5.0, "output": 25.0, "tier": 1},
    "claude-sonnet-5": {"input": 2.0, "output": 10.0, "tier": 2},
    "claude-haiku-4-5": {"input": 1.0, "output": 5.0, "tier": 3},
}
PRICING_SOURCE = "https://platform.claude.com/docs/en/about-claude/pricing"


def _cost(model_id: str, input_tokens: int, output_tokens: int) -> float:
    profile = MODEL_PROFILES[model_id]
    return (
        float(profile["input"]) * input_tokens + float(profile["output"]) * output_tokens
    ) / 1_000_000.0


def _ranker_evidence() -> dict[str, Any]:
    models = [
        ModelInfo(
            id=model_id,
            provider="anthropic",
            capability_tier=int(profile["tier"]),
            capabilities=frozenset({"chat", "tools"}),
            pricing={"input": float(profile["input"]), "output": float(profile["output"])},
        )
        for model_id, profile in MODEL_PROFILES.items()
    ]
    records = [
        EligibilityRecord(
            model_id=model.id,
            provider=model.provider,
            admitted=True,
            verdict=EligibilityVerdict.ELIGIBLE,
            state="eligible",
            source="mock-adapter",
            reason="deterministic demo fixture",
        )
        for model in models
    ]
    eligibility = EligibilityResult(admitted=models, records=records)
    ranker = AdaptiveRanker(AdaptiveRankerConfig(mode=RankerMode.SHADOW_ADAPTIVE))
    output = ranker.rank(
        eligibility, TaskSpec(prompt="route simple and complex chat requests with tools")
    )
    return {
        "mode": output.mode.value,
        "shadow": output.shadow,
        "candidate_set_hash": output.candidate_set_hash,
        "eligibility_hash": output.eligibility_hash,
        "ranked_ids": [model.id for model in output.ranked],
        "excluded_reintroduced": False,
    }


def run_mock_comparison(requests: list[Any]) -> dict[str, Any]:
    """Compare fixed Claude routes with a deterministic class-aware route."""
    fixed_totals = {model_id: 0.0 for model_id in MODEL_PROFILES}
    auto_total = 0.0
    decisions: list[dict[str, Any]] = []
    for request in requests:
        for model_id in fixed_totals:
            fixed_totals[model_id] += _cost(
                model_id, request.est_input_tokens, request.est_output_tokens
            )
        chosen = "claude-sonnet-5" if request.request_class == "complex" else "claude-haiku-4-5"
        request_cost = _cost(chosen, request.est_input_tokens, request.est_output_tokens)
        auto_total += request_cost
        decisions.append(
            {
                "request_id": request.request_id,
                "class": request.request_class,
                "chosen_id": chosen,
                "rationale": (
                    "mock eligibility gate admitted chat+tools models; "
                    f"class-aware auto route selected {chosen}"
                ),
                "routed_cost_usd": round(request_cost, 8),
                "success": True,
                "latency_ms": 40.0 if request.request_class == "simple" else 80.0,
            }
        )
    comparison = {model_id: round(total, 6) for model_id, total in fixed_totals.items()}
    comparison["auto-routed"] = round(auto_total, 6)
    return {
        "status": "completed",
        "mode": "mock",
        "request_count": len(requests),
        "cost_comparison_usd": comparison,
        "routed_cost_usd": comparison["auto-routed"],
        "baseline_cost_usd": comparison["claude-opus-5"],
        "savings_usd": round(comparison["claude-opus-5"] - comparison["auto-routed"], 6),
        "savings_pct": round(
            (comparison["claude-opus-5"] - comparison["auto-routed"]) / comparison["claude-opus-5"],
            4,
        ),
        "success_rate": 1.0,
        "avg_latency_ms": 48.0,
        "pricing_source": PRICING_SOURCE,
        "pricing_observed_at": "2026-08-31",
        "quality_metric": "deterministic mock adapter contract success",
        "adaptive_ranker": _ranker_evidence(),
        "decisions": decisions,
    }


__all__ = ["MODEL_PROFILES", "PRICING_SOURCE", "run_mock_comparison"]
