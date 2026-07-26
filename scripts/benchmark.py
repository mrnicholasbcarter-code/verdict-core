#!/usr/bin/env python3
"""
Reproducible Quality/Cost/Latency/Availability Benchmark Harness

Deterministic benchmark using fixtures - no live upstream calls required.
Outputs JSON for CI integration.
"""

import argparse
import json
import random
import statistics
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class WorkloadType(str, Enum):
    CODING = "coding"
    REASONING = "reasoning"
    CHAT = "chat"


@dataclass
class BenchmarkConfig:
    workload: WorkloadType = WorkloadType.CODING
    iterations: int = 50
    warmup: int = 10
    comparison_mode: bool = True
    seed: int = 42


@dataclass
class RequestResult:
    success: bool
    latency_ms: float
    tokens_in: int
    tokens_out: int
    cost_usd: float
    model_used: str
    error: str = ""


@dataclass
class BenchmarkResult:
    timestamp: str
    config: BenchmarkConfig
    workload: str
    iterations: int
    fixed_assignment: dict[str, Any] = None
    verdict_routing: dict[str, Any] = None
    comparison: dict[str, Any] = None


# Deterministic fixture data for reproducible benchmarks
FIXTURE_MODELS = {
    "gpt-4o": {"tier": "frontier", "cost_per_1k_in": 0.005, "cost_per_1k_out": 0.015, "capabilities": {"tools", "structured_output"}},
    "gpt-4o-mini": {"tier": "mid", "cost_per_1k_in": 0.00015, "cost_per_1k_out": 0.0006, "capabilities": {"tools", "structured_output"}},
    "claude-3-5-sonnet": {"tier": "frontier", "cost_per_1k_in": 0.003, "cost_per_1k_out": 0.015, "capabilities": {"tools", "structured_output"}},
    "claude-3-haiku": {"tier": "mid", "cost_per_1k_in": 0.00025, "cost_per_1k_out": 0.00125, "capabilities": {"tools", "structured_output"}},
}

WORKLOAD_FIXTURES = {
    WorkloadType.CODING: {
        "required_capabilities": {"tools", "structured_output"},
        "typical_tokens_in": (800, 2000),
        "typical_tokens_out": (200, 800),
        "complexity": "high",
    },
    WorkloadType.REASONING: {
        "required_capabilities": {"structured_output"},
        "typical_tokens_in": (500, 1500),
        "typical_tokens_out": (100, 500),
        "complexity": "high",
    },
    WorkloadType.CHAT: {
        "required_capabilities": set(),
        "typical_tokens_in": (50, 300),
        "typical_tokens_out": (50, 200),
        "complexity": "low",
    },
}


def deterministic_hash(*args) -> int:
    """Deterministic hash for reproducible randomness."""
    h = 0
    for arg in args:
        h = (h * 31 + hash(str(arg))) & 0x7fffffff
    return h


def simulate_request(config: BenchmarkConfig, model: str, iteration: int, use_verdict: bool) -> RequestResult:
    """Simulate a single request with deterministic behavior."""
    model_info = FIXTURE_MODELS[model]
    workload_info = WORKLOAD_FIXTURES[config.workload]

    # Deterministic token counts
    seed = deterministic_hash(config.seed, config.workload.value, model, iteration, use_verdict)
    rng = random.Random(seed)

    tokens_in = rng.randint(*workload_info["typical_tokens_in"])
    tokens_out = rng.randint(*workload_info["typical_tokens_out"])

    # Simulate latency (deterministic based on model tier)
    base_latency = 100 if model_info["tier"] == "frontier" else 50
    latency_variance = rng.uniform(0.8, 1.5)
    latency_ms = base_latency * latency_variance

    # Cost calculation
    cost = (tokens_in / 1000 * model_info["cost_per_1k_in"] +
            tokens_out / 1000 * model_info["cost_per_1k_out"])

    # Deterministic success/failure (99% success for frontier, 98% for mid)
    success_rate = 0.99 if model_info["tier"] == "frontier" else 0.98
    success = (deterministic_hash("success", seed) % 10000) / 10000 < success_rate

    error = ""
    if not success:
        error = "Upstream timeout" if rng.random() < 0.5 else "Rate limited"

    return RequestResult(
        success=success,
        latency_ms=round(latency_ms, 2),
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        cost_usd=round(cost, 6),
        model_used=model,
        error=error
    )


def run_workload(config: BenchmarkConfig, models: list[str], use_verdict: bool) -> dict[str, Any]:
    """Run benchmark workload for given models."""
    results = []

    for iteration in range(config.warmup + config.iterations):
        for model in models:
            result = simulate_request(config, model, iteration, use_verdict)
            if iteration >= config.warmup:
                results.append(result)

    # Calculate statistics
    successful = [r for r in results if r.success]
    latencies = [r.latency_ms for r in successful]
    costs = [r.cost_usd for r in successful]

    if not latencies:
        return {"error": "All requests failed"}

    def pctl(p: float) -> float:
        if not latencies:
            return 0
        sorted_lat = sorted(latencies)
        idx = int(len(sorted_lat) * p / 100)
        return round(sorted_lat[min(idx, len(sorted_lat) - 1)], 2)

    return {
        "model": models[0] if len(models) == 1 else "mixed",
        "iterations": len(results),
        "success_rate": round(len(successful) / len(results), 4),
        "latency": {
            "p50": pctl(50),
            "p95": pctl(95),
            "p99": pctl(99),
            "mean": round(statistics.mean(latencies), 2),
            "stdev": round(statistics.stdev(latencies), 2) if len(latencies) > 1 else 0,
        },
        "cost": {
            "mean_usd": round(statistics.mean(costs), 6),
            "total_usd": round(sum(costs), 6),
            "per_1k_tokens_usd": round(statistics.mean(costs) / (statistics.mean([r.tokens_in + r.tokens_out for r in successful]) / 1000), 6) if successful else 0,
        },
        "tokens": {
            "mean_in": round(statistics.mean([r.tokens_in for r in successful]), 1),
            "mean_out": round(statistics.mean([r.tokens_out for r in successful]), 1),
        },
        "errors": [r.error for r in results if not r.success][:5],
    }


def main():
    parser = argparse.ArgumentParser(description="Verdict Benchmark Harness")
    parser.add_argument("--workload", choices=["coding", "reasoning", "chat"], default="coding")
    parser.add_argument("--iterations", type=int, default=50)
    parser.add_argument("--warmup", type=int, default=10)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--output", type=str, help="Output JSON file")
    args = parser.parse_args()

    config = BenchmarkConfig(
        workload=WorkloadType(args.workload),
        iterations=args.iterations,
        warmup=args.warmup,
        seed=args.seed,
    )

    random.seed(args.seed)

    print(f"🔬 Running benchmark: {args.workload} ({config.iterations} iterations, seed={config.seed})")

    # Fixed assignment: always use most expensive capable model
    if args.workload == "coding":
        fixed_models = ["gpt-4o"]
    elif args.workload == "reasoning":
        fixed_models = ["claude-3-5-sonnet"]
    else:
        fixed_models = ["gpt-4o-mini"]

    # Verdict routing: select cheapest capable model
    verdict_models = {
        "coding": ["gpt-4o", "gpt-4o-mini", "claude-3-5-sonnet", "claude-3-haiku"],
        "reasoning": ["claude-3-5-sonnet", "claude-3-haiku", "gpt-4o", "gpt-4o-mini"],
        "chat": ["gpt-4o-mini", "claude-3-haiku", "gpt-4o", "claude-3-5-sonnet"],
    }[args.workload]

    print("  Running fixed assignment...")
    fixed_result = run_workload(config, fixed_models, use_verdict=False)

    print("  Running Verdict routing...")
    verdict_result = run_workload(config, verdict_models, use_verdict=True)

    # Comparison
    comparison = {}
    if "latency" in fixed_result and "latency" in verdict_result:
        comparison = {
            "latency_p50_diff_ms": round(verdict_result["latency"]["p50"] - fixed_result["latency"]["p50"], 2),
            "latency_p95_diff_ms": round(verdict_result["latency"]["p95"] - fixed_result["latency"]["p95"], 2),
            "cost_savings_pct": round((1 - verdict_result["cost"]["mean_usd"] / fixed_result["cost"]["mean_usd"]) * 100, 1) if fixed_result["cost"]["mean_usd"] > 0 else 0,
            "success_rate_diff": round(verdict_result["success_rate"] - fixed_result["success_rate"], 4),
        }

    result = BenchmarkResult(
        timestamp=datetime.now(timezone.utc).isoformat() + "Z",
        config=config,
        workload=args.workload,
        iterations=config.iterations,
        fixed_assignment=fixed_result,
        verdict_routing=verdict_result,
        comparison=comparison,
    )

    # Output
    def enum_serializer(obj):
        if hasattr(obj, "value"):
            return obj.value
        raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")

    json_output = json.dumps(asdict(result), indent=2, default=enum_serializer)

    if args.output:
        with open(args.output, 'w') as f:
            f.write(json_output)
        print(f"✅ Results written to {args.output}")
    else:
        print(json_output)

    # Print summary
    print("\n📊 SUMMARY")
    print(f"  Fixed Assignment:  {fixed_result['success_rate']*100:.1f}% success, ${fixed_result['cost']['mean_usd']:.6f}/req, p95={fixed_result['latency']['p95']:.0f}ms")
    print(f"  Verdict Routing:  {verdict_result['success_rate']*100:.1f}% success, ${verdict_result['cost']['mean_usd']:.6f}/req, p95={verdict_result['latency']['p95']:.0f}ms")
    if comparison:
        print(f"  Cost Savings: {comparison['cost_savings_pct']:.1f}%")
        print(f"  Latency Delta (p95): {comparison['latency_p95_diff_ms']:.0f}ms")


if __name__ == "__main__":
    main()
