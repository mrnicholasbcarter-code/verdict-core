"""DIRECT-frontier vs Verdict route comparison harness (issue #265, V1-002).

Compares the model a client would pin by calling the frontier/OmniRoute API
DIRECT-ly against the model and strategy Verdict's Gate selects for the same
task.  Every report field is a pure function of static configuration and the
deterministic routing decision, so identical inputs always produce identical
reports — including with ``allow_offline=True`` and no network.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from verdict.classifier import classify
from verdict.gate import Gate, strategy_from_decision

# Static per-tier planning estimates (per 1k tokens / per request).  These are
# deliberately configuration constants, not measurements, so comparison
# reports stay deterministic and readable offline.
TIER_COST_PER_1K = {0: 15.0, 1: 3.0, 2: 0.5, 3: 0.1}
TIER_LATENCY_MS = {0: 2000.0, 1: 1200.0, 2: 600.0, 3: 300.0}
TIER_QUALITY = {0: 1.0, 1: 0.8, 2: 0.6, 3: 0.4}


def _tier_value(table: dict[int, float], tier: int) -> float:
    return table.get(max(0, min(tier, 3)), table[2])


@dataclass(frozen=True)
class DirectResult:
    """Model resolution for a DIRECT frontier call (no Verdict gating)."""

    task: str
    model: str
    tier: int
    cost_per_1k: float
    latency_estimate_ms: float
    source: str


@dataclass(frozen=True)
class VerdictResult:
    """Verdict-gated route for the same task, with its strategy record."""

    task: str
    strategy: str
    model: str
    provider: str
    tier: int
    reason: str
    cost_per_1k: float
    latency_estimate_ms: float


@dataclass(frozen=True)
class ComparisonReport:
    """Deterministic DIRECT-vs-Verdict comparison record.

    ``latency_delta`` and ``cost_delta`` are Verdict minus DIRECT static
    estimates (negative means Verdict is cheaper/faster on paper);
    ``quality_score`` is the static quality estimate of the Verdict route.
    """

    task: str
    direct_model: str
    verdict_strategy: str
    verdict_model: str
    latency_delta: float
    cost_delta: float
    quality_score: float

    def to_dict(self) -> dict[str, object]:
        return {
            "task": self.task,
            "direct_model": self.direct_model,
            "verdict_strategy": self.verdict_strategy,
            "verdict_model": self.verdict_model,
            "latency_delta": self.latency_delta,
            "cost_delta": self.cost_delta,
            "quality_score": self.quality_score,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True)


class ComparisonHarness:
    """Runs the DIRECT resolution and the Verdict route on one task."""

    def __init__(self, gate: Gate | None = None, allow_offline: bool = True):
        self.gate = gate or Gate(allow_offline=allow_offline)

    def run_direct(self, task: str) -> DirectResult:
        """Resolve the model a DIRECT frontier/OmniRoute call would pin.

        A direct client bypasses routing entirely and pins the configured
        primary frontier model, so the resolution is static: no completion is
        issued and no network is required.
        """
        model = self.gate.primary_model
        tier = classify(model)
        return DirectResult(
            task=task,
            model=model,
            tier=tier,
            cost_per_1k=_tier_value(TIER_COST_PER_1K, tier),
            latency_estimate_ms=_tier_value(TIER_LATENCY_MS, tier),
            source="primary_model",
        )

    def run_verdict(self, task: str, criticality: str = "medium") -> VerdictResult:
        """Route the task through Gate and capture the strategy record."""
        decision = self.gate.route(task, criticality=criticality)
        selection = strategy_from_decision(decision)
        return VerdictResult(
            task=task,
            strategy=selection.strategy,
            model=decision.model,
            provider=decision.provider,
            tier=decision.tier,
            reason=decision.reason,
            cost_per_1k=_tier_value(TIER_COST_PER_1K, decision.tier),
            latency_estimate_ms=_tier_value(TIER_LATENCY_MS, decision.tier),
        )

    def compare(self, task: str, criticality: str = "medium") -> ComparisonReport:
        """Produce the deterministic DIRECT-vs-Verdict comparison report."""
        direct = self.run_direct(task)
        verdict = self.run_verdict(task, criticality=criticality)
        return ComparisonReport(
            task=task,
            direct_model=direct.model,
            verdict_strategy=verdict.strategy,
            verdict_model=verdict.model,
            latency_delta=verdict.latency_estimate_ms - direct.latency_estimate_ms,
            cost_delta=verdict.cost_per_1k - direct.cost_per_1k,
            quality_score=_tier_value(TIER_QUALITY, verdict.tier),
        )
