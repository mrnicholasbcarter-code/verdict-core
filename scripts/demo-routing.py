#!/usr/bin/env python3
"""
Verdict-Core Intelligent Model Routing Demo

Demonstrates cost-quality optimization through intelligent model selection.
Routes 100 sample requests comparing Opus-only, Haiku-only, Sonnet-only,
and auto-routed strategies.

Run: PYTHONPATH=. python scripts/demo-routing.py
"""

import json
import random
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from verdict.models import ModelInfo


@dataclass
class MockRequest:
    """Simulated routing request."""

    request_id: str
    prompt: str
    criticality: str  # low, medium, high, critical
    context_tokens: int
    required_capabilities: list[str] = field(default_factory=list)


@dataclass
class RoutingResult:
    """Result of a single routing decision."""

    request_id: str
    selected_model: str
    selected_tier: int
    provider: str
    latency_ms: float
    tokens_used: int
    cost_usd: float
    quality_score: float
    routing_reason: str
    alternatives: list[str] = field(default_factory=list)


# Model catalog with realistic pricing (per 1M tokens)
# Quality confidence adjusted so auto-routing picks appropriately for each tier
MOCK_CATALOG: list[ModelInfo] = [
    # Tier 0 - Frontier (Opus-class) - Best for complex reasoning
    ModelInfo(
        id="anthropic/claude-opus-4",
        model="claude-opus-4",
        provider="anthropic",
        capability_tier=0,
        is_available=True,
        availability_state="eligible",
        quality_confidence=0.99,
        context_window=200000,
        capabilities=frozenset(["tools", "vision", "streaming", "reasoning"]),
        pricing={"input": 15.0, "output": 75.0},
    ),
    ModelInfo(
        id="openai/gpt-5.5-turbo",
        model="gpt-5.5-turbo",
        provider="openai",
        capability_tier=0,
        is_available=True,
        availability_state="eligible",
        quality_confidence=0.98,
        context_window=128000,
        capabilities=frozenset(["tools", "vision", "streaming", "reasoning"]),
        pricing={"input": 10.0, "output": 30.0},
    ),
    # Tier 1 - High (Sonnet-class) - Best balance for most tasks
    ModelInfo(
        id="anthropic/claude-sonnet-4",
        model="claude-sonnet-4",
        provider="anthropic",
        capability_tier=1,
        is_available=True,
        availability_state="eligible",
        quality_confidence=0.96,
        context_window=200000,
        capabilities=frozenset(["tools", "vision", "streaming"]),
        pricing={"input": 3.0, "output": 15.0},
    ),
    ModelInfo(
        id="openai/gpt-4o",
        model="gpt-4o",
        provider="openai",
        capability_tier=1,
        is_available=True,
        availability_state="eligible",
        quality_confidence=0.95,
        context_window=128000,
        capabilities=frozenset(["tools", "vision", "streaming"]),
        pricing={"input": 2.5, "output": 10.0},
    ),
    # Tier 2 - Medium (Haiku-class) - Great for routine tasks
    ModelInfo(
        id="anthropic/claude-haiku-3.5",
        model="claude-haiku-3.5",
        provider="anthropic",
        capability_tier=2,
        is_available=True,
        availability_state="eligible",
        quality_confidence=0.94,
        context_window=200000,
        capabilities=frozenset(["tools", "vision", "streaming"]),
        pricing={"input": 0.80, "output": 4.0},
    ),
    ModelInfo(
        id="openai/gpt-4o-mini",
        model="gpt-4o-mini",
        provider="openai",
        capability_tier=2,
        is_available=True,
        availability_state="eligible",
        quality_confidence=0.92,
        context_window=128000,
        capabilities=frozenset(["tools", "vision", "streaming"]),
        pricing={"input": 0.15, "output": 0.60},
    ),
    # Tier 3 - Economy - Fast/cheap for simple tasks
    ModelInfo(
        id="anthropic/claude-haiku-4",
        model="claude-haiku-4",
        provider="anthropic",
        capability_tier=3,
        is_available=True,
        availability_state="eligible",
        quality_confidence=0.88,
        context_window=200000,
        capabilities=frozenset(["tools", "streaming"]),
        pricing={"input": 0.25, "output": 1.25},
    ),
]

# Provider configs for routing
PROVIDER_CONFIGS = {
    "anthropic": type("ProviderConfig", (), {"priority": 10})(),
    "openai": type("ProviderConfig", (), {"priority": 9})(),
}


def generate_sample_requests(n: int = 100, seed: int = 42) -> list[MockRequest]:
    """Generate diverse sample requests with varying complexity."""
    random.seed(seed)

    request_templates = [
        ("Fix the typo in this function name", "low", 500, []),
        ("Explain what this code does", "low", 1200, []),
        ("Add error handling to this API endpoint", "medium", 2500, ["tools"]),
        ("Refactor this class to use dependency injection", "medium", 4000, []),
        ("Write unit tests for this module", "medium", 3500, ["tools"]),
        ("Debug why this async function hangs", "high", 6000, ["tools"]),
        ("Optimize this database query performance", "high", 8000, []),
        ("Design a REST API for user management", "high", 10000, []),
        ("Implement authentication with JWT tokens", "high", 12000, ["tools", "reasoning"]),
        (
            "Review this security-critical code for vulnerabilities",
            "critical",
            15000,
            ["tools", "reasoning", "vision"],
        ),
        ("Design a microservices architecture for a fintech app", "critical", 20000, ["reasoning"]),
        ("Implement a distributed consensus algorithm", "critical", 25000, ["reasoning", "tools"]),
        ("Analyze this image for accessibility issues", "high", 5000, ["vision"]),
        (
            "Generate a technical spec from this whiteboard photo",
            "critical",
            8000,
            ["vision", "reasoning"],
        ),
        ("Simple string formatting question", "low", 200, []),
        ("Convert this JSON to YAML", "low", 300, []),
        ("Explain the difference between async and sync", "low", 400, []),
        ("Write a regex to validate email addresses", "low", 600, []),
        ("Fix a bug in this 10-line function", "medium", 800, []),
        ("Add logging to this module", "medium", 1000, []),
    ]

    requests = []
    for i in range(n):
        template = random.choice(request_templates)
        prompt, criticality, base_tokens, capabilities = template

        # Add variation
        tokens = base_tokens + random.randint(-200, 500)
        tokens = max(100, tokens)

        # Occasionally add/remove capabilities
        if capabilities and random.random() < 0.2:
            capabilities = capabilities.copy()
            capabilities.pop(random.randint(0, len(capabilities) - 1))

        requests.append(
            MockRequest(
                request_id=f"req_{i:04d}",
                prompt=prompt,
                criticality=criticality,
                context_tokens=tokens,
                required_capabilities=capabilities,
            )
        )

    return requests


def determine_tier_from_criticality(criticality: str) -> int:
    """Map criticality to maximum acceptable tier (lower = more capable)."""
    mapping = {
        "critical": 0,  # Only tier 0
        "high": 1,  # Tier 0 or 1
        "medium": 2,  # Tier 0, 1, or 2
        "low": 3,  # Any tier
    }
    return mapping.get(criticality, 2)


def mock_model_response(model: ModelInfo, request: MockRequest) -> RoutingResult:
    """Simulate a model response with realistic metrics."""
    # Simulate latency (tier affects speed)
    base_latency = {
        0: 2500,  # Opus-class slower
        1: 1500,  # Sonnet-class
        2: 800,  # Haiku-class
        3: 500,  # Fastest
    }
    latency = base_latency[model.capability_tier] + random.randint(-200, 500)
    latency = max(200, latency)

    # Estimate tokens (output roughly 30-50% of context)
    output_tokens = int(request.context_tokens * random.uniform(0.3, 0.5))
    total_tokens = request.context_tokens + output_tokens

    # Calculate cost from pricing dict
    pricing = model.pricing or {}
    input_cost = (request.context_tokens / 1_000_000) * pricing.get("input", 0)
    output_cost = (output_tokens / 1_000_000) * pricing.get("output", 0)
    total_cost = input_cost + output_cost

    # Quality score (higher tier = better, with variance)
    base_quality = {0: 0.97, 1: 0.93, 2: 0.87, 3: 0.82}
    quality = base_quality[model.capability_tier] + random.uniform(-0.03, 0.02)
    quality = max(0.5, min(1.0, quality))

    return RoutingResult(
        request_id=request.request_id,
        selected_model=model.id,
        selected_tier=model.capability_tier,
        provider=model.provider,
        latency_ms=latency,
        tokens_used=total_tokens,
        cost_usd=total_cost,
        quality_score=quality,
        routing_reason=f"tier {model.capability_tier} selected for {request.criticality} task",
    )


def route_single_model_strategy(
    requests: list[MockRequest], model: ModelInfo
) -> list[RoutingResult]:
    """Route all requests to a single fixed model (baseline)."""
    results = []
    for request in requests:
        result = mock_model_response(model, request)
        result.routing_reason = f"fixed assignment to {model.id}"
        results.append(result)
    return results


def route_auto_strategy(requests: list[MockRequest]) -> list[RoutingResult]:
    """Route using verdict intelligent selection with cost-quality optimization."""
    results = []

    for request in requests:
        # Determine max acceptable tier based on criticality
        max_tier = determine_tier_from_criticality(request.criticality)

        # Filter candidates by capability requirements
        candidates = []
        for model in MOCK_CATALOG:
            if not model.is_available:
                continue

            # Check capability requirements using capabilities frozenset
            meets_caps = True
            for cap in request.required_capabilities:
                if cap not in model.capabilities:
                    meets_caps = False

            if meets_caps:
                candidates.append(model)

        # Filter by tier eligibility
        tier_candidates = [m for m in candidates if m.capability_tier <= max_tier]
        if not tier_candidates:
            tier_candidates = candidates  # fallback

        # Intelligent selection: balance quality vs cost
        # For low-criticality tasks, prefer cheaper models
        # For high-criticality, prefer quality
        def selection_score(model: ModelInfo, request: MockRequest = request) -> float:
            quality = model.quality_confidence or (1.0 - model.capability_tier / 3.0)
            pricing = model.pricing or {}
            avg_price = (pricing.get("input", 0) + pricing.get("output", 0)) / 2

            # Cost sensitivity based on criticality
            cost_sensitivity = {"critical": 0.1, "high": 0.2, "medium": 0.4, "low": 0.7}.get(
                request.criticality, 0.4
            )

            # Score: quality * (1 - cost_sensitivity) - normalized_cost * cost_sensitivity
            # Normalize cost to 0-1 range (max ~$40/M tokens)
            normalized_cost = min(avg_price / 40.0, 1.0)
            score = quality * (1 - cost_sensitivity) - normalized_cost * cost_sensitivity
            return score

        # Sort by custom score, then by tier, then provider priority
        tier_candidates.sort(
            key=lambda m: (
                -selection_score(m),
                m.capability_tier,
                -PROVIDER_CONFIGS[m.provider].priority,
                m.id,
            )
        )

        selected = tier_candidates[0]
        alternatives = [m.id for m in tier_candidates[1:5]]

        result = mock_model_response(selected, request)
        result.routing_reason = f"auto-routed to tier {selected.capability_tier} for {request.criticality} (score={selection_score(selected):.3f})"
        result.alternatives = alternatives
        results.append(result)

    return results


def compute_statistics(results: list[RoutingResult]) -> dict[str, Any]:
    """Compute aggregate statistics from routing results."""
    total_cost = sum(r.cost_usd for r in results)
    total_tokens = sum(r.tokens_used for r in results)
    avg_latency = sum(r.latency_ms for r in results) / len(results)
    avg_quality = sum(r.quality_score for r in results) / len(results)

    # Tier distribution
    tier_counts = {}
    for r in results:
        tier_counts[r.selected_tier] = tier_counts.get(r.selected_tier, 0) + 1

    # Provider distribution
    provider_counts = {}
    for r in results:
        provider_counts[r.provider] = provider_counts.get(r.provider, 0) + 1

    # Latency percentiles
    latencies = sorted([r.latency_ms for r in results])
    p50 = latencies[len(latencies) // 2]
    p95 = latencies[int(len(latencies) * 0.95)]
    p99 = latencies[int(len(latencies) * 0.99)]

    return {
        "total_requests": len(results),
        "total_cost_usd": round(total_cost, 4),
        "total_tokens": total_tokens,
        "avg_cost_per_request": round(total_cost / len(results), 6),
        "avg_latency_ms": round(avg_latency, 1),
        "p50_latency_ms": round(p50, 1),
        "p95_latency_ms": round(p95, 1),
        "p99_latency_ms": round(p99, 1),
        "avg_quality_score": round(avg_quality, 4),
        "tier_distribution": tier_counts,
        "provider_distribution": provider_counts,
    }


def render_terminal_table(stats: dict[str, dict[str, Any]]) -> str:
    """Render comparison table for terminal output."""
    lines = [
        "",
        "=" * 90,
        "VERDICT-CORE INTELLIGENT ROUTING DEMO",
        "=" * 90,
        "",
        "Routing 100 sample requests through different strategies...",
        "",
        "-" * 90,
        f"{'Strategy':<20} {'Cost':>12} {'Avg Latency':>14} {'Quality':>10} {'P95 Latency':>14}",
        "-" * 90,
    ]

    for name in ["Opus-Only", "Sonnet-Only", "Haiku-Only", "Auto-Routed"]:
        s = stats[name]
        cost = f"${s['total_cost_usd']:.4f}"
        latency = f"{s['avg_latency_ms']:.0f}ms"
        quality = f"{s['avg_quality_score']:.2%}"
        p95 = f"{s['p95_latency_ms']:.0f}ms"
        lines.append(f"{name:<20} {cost:>12} {latency:>14} {quality:>10} {p95:>14}")

    lines.extend(["-" * 90, "", "COST SAVINGS ANALYSIS", "-" * 90])

    opus_cost = stats["Opus-Only"]["total_cost_usd"]
    auto_cost = stats["Auto-Routed"]["total_cost_usd"]
    savings_pct = ((opus_cost - auto_cost) / opus_cost) * 100

    lines.append(f"Opus baseline:      ${opus_cost:.4f}")
    lines.append(f"Auto-routed total:  ${auto_cost:.4f}")
    lines.append(f"Cost savings:       {savings_pct:.1f}% (${opus_cost - auto_cost:.4f})")
    lines.append("")

    # Tier distribution for auto-routed
    lines.extend(["TIER DISTRIBUTION (Auto-Routed)", "-" * 90])
    tier_dist = stats["Auto-Routed"]["tier_distribution"]
    total = sum(tier_dist.values())
    for tier in sorted(tier_dist.keys()):
        count = tier_dist[tier]
        pct = (count / total) * 100
        tier_name = {0: "Opus", 1: "Sonnet", 2: "Haiku", 3: "Economy"}.get(tier, f"Tier {tier}")
        bar = "=" * int(pct / 2)
        lines.append(f"  Tier {tier} ({tier_name:>8}): {count:>3} requests ({pct:>5.1f}%) {bar}")
    lines.append("")

    lines.extend(
        [
            "QUALITY METRICS",
            "-" * 90,
            f"Auto-routed avg quality: {stats['Auto-Routed']['avg_quality_score']:.2%}",
            f"Opus-only avg quality:   {stats['Opus-Only']['avg_quality_score']:.2%}",
            f"Quality retention:       {(stats['Auto-Routed']['avg_quality_score'] / stats['Opus-Only']['avg_quality_score']) * 100:.1f}%",
            "",
            "ROUTING DECISIONS SAMPLE",
            "-" * 90,
        ]
    )

    # Sample routing decisions (from auto-routed results stored globally)
    if "sample_decisions" in stats:
        for decision in stats["sample_decisions"][:10]:
            lines.append(f"  {decision}")
    lines.append("")

    lines.extend(["=" * 90, "Status: PASS - Demo completed successfully", "=" * 90])

    return "\n".join(lines)


def main():
    """Run the routing demonstration."""
    print("Starting Verdict-Core routing demo...")
    print()

    # Generate sample requests
    requests = generate_sample_requests(100)

    # Get representative models for each strategy
    opus_model = next(m for m in MOCK_CATALOG if "opus" in m.id.lower())
    sonnet_model = next(m for m in MOCK_CATALOG if "sonnet" in m.id.lower())
    haiku_model = next(m for m in MOCK_CATALOG if "haiku-3.5" in m.id.lower())

    # Run strategies
    print("Running Opus-only baseline...")
    opus_results = route_single_model_strategy(requests, opus_model)

    print("Running Sonnet-only baseline...")
    sonnet_results = route_single_model_strategy(requests, sonnet_model)

    print("Running Haiku-only baseline...")
    haiku_results = route_single_model_strategy(requests, haiku_model)

    print("Running Verdict auto-routing...")
    auto_results = route_auto_strategy(requests)

    # Compute statistics
    stats = {
        "Opus-Only": compute_statistics(opus_results),
        "Sonnet-Only": compute_statistics(sonnet_results),
        "Haiku-Only": compute_statistics(haiku_results),
        "Auto-Routed": compute_statistics(auto_results),
    }

    # Add sample routing decisions
    stats["sample_decisions"] = [
        f"{r.request_id}: {req.criticality} -> {r.selected_model} (tier {r.selected_tier})"
        for r, req in zip(auto_results[:20], requests[:20], strict=False)
    ]

    # Terminal output
    table_output = render_terminal_table(stats)
    print(table_output)

    # JSON output
    output_data = {
        "demo": "verdict-core-routing",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "request_count": 100,
        "strategies": {
            name: {
                **s,
                "tier_distribution": {
                    f"tier_{k}": v for k, v in s.get("tier_distribution", {}).items()
                },
            }
            for name, s in stats.items()
            if name != "sample_decisions"
        },
        "sample_requests": [
            {
                "request_id": r.request_id,
                "criticality": req.criticality,
                "context_tokens": req.context_tokens,
                "required_capabilities": req.required_capabilities,
            }
            for r, req in zip(auto_results[:20], requests[:20], strict=False)
        ],
        "sample_routing_decisions": stats["sample_decisions"],
    }

    output_path = Path("/home/nick/dev/verdict-core-eco-pr263/scripts/demo-results.json")
    with open(output_path, "w") as f:
        json.dump(output_data, f, indent=2)

    print(f"\nJSON results written to: {output_path}")


if __name__ == "__main__":
    main()
