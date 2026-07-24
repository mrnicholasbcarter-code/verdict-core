"""
Observe-only adaptive ranker with eligibility invariant (Issue #60 / Slice 33.3).

This module implements the learning loop closure that respects the eligibility
invariant from Issue #57: the ranker receives ONLY pre-filtered eligible
candidates and can NEVER reintroduce excluded candidates through memory or
learned scores.

Key invariants:
- Ranker receives ONLY the pre-filtered eligible set (never sees excluded)
- Excluded candidates CANNOT be reintroduced via memory or learned scores
- Shadow results never change live selection
- Candidate-set hash + eligibility snapshot bound for evidence
- Versioned policy + rollback switch for canary activation
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any

from verdict.eligibility import (
    EligibilityRecord,
    EligibilityResult,
)
from verdict.models import ModelInfo


class RankerMode(str, Enum):
    """Ranker operational mode."""

    STATIC = "static"          # No learning, deterministic baseline
    SEMANTIC = "semantic"      # Advisory semantic ranking (RuVector/SONA)
    SHADOW_ADAPTIVE = "shadow_adaptive"  # Observe-only adaptive (this slice)


class CanaryPolicy(str, Enum):
    """Canary activation policy."""

    DISABLED = "disabled"
    VERSIONED = "versioned"     # Requires explicit version + rollback switch


@dataclass(frozen=True)
class RankingCandidate:
    """A candidate model that passed eligibility gate."""
    model: ModelInfo
    eligibility_record: EligibilityRecord


@dataclass(frozen=True)
class RankerInput:
    """Input to the adaptive ranker (eligibility-filtered only)."""
    candidates: tuple[RankingCandidate, ...]
    task_spec: Any
    eligibility_snapshot: EligibilityResult
    candidate_set_hash: str


@dataclass(frozen=True)
class RankerOutput:
    """Ranker output with evidence binding."""
    ranked: list[ModelInfo]
    scores: dict[str, float]
    reasoning: dict[str, str]
    candidate_set_hash: str
    eligibility_hash: str
    mode: RankerMode
    shadow: bool  # True = observe-only, never affects live selection
    version: str
    canary_policy: CanaryPolicy


@dataclass
class AdaptiveRankerConfig:
    """Configuration for adaptive ranker."""
    mode: RankerMode = RankerMode.SHADOW_ADAPTIVE
    canary_policy: CanaryPolicy = CanaryPolicy.DISABLED
    canary_version: str = "1.0.0"
    rollback_enabled: bool = True
    semantic_weight: float = 0.7
    adaptive_weight: float = 0.3
    max_history_size: int = 1000


class AdaptiveRanker:
    """
    Observe-only adaptive ranker with eligibility invariant.

    The ranker ONLY sees candidates that passed the EligibilityGate.
    It can NEVER reintroduce excluded candidates.
    Shadow mode means it never affects live selection.
    """

    def __init__(
        self,
        config: AdaptiveRankerConfig | None = None,
        *,
        ruvector_db_path: str | None = None,
        sona_enabled: bool = False,
    ) -> None:
        self.config = config or AdaptiveRankerConfig()
        self._history: list[dict[str, Any]] = []
        self._ruvector_db_path = ruvector_db_path
        self._sona_enabled = sona_enabled
        self._version = "1.0.0"

    def _compute_candidate_set_hash(self, candidates: tuple[RankingCandidate, ...]) -> str:
        """Compute deterministic hash of candidate set."""
        data = tuple(sorted(c.model.id for c in candidates))
        return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()[:16]

    def _compute_eligibility_hash(self, result: EligibilityResult) -> str:
        """Compute deterministic hash of eligibility snapshot."""
        admitted = tuple(sorted(r.model_id for r in result.records if r.admitted))
        excluded = tuple(sorted(r.model_id for r in result.records if not r.admitted))
        data = {"admitted": admitted, "excluded": excluded}
        return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()[:16]

    def _static_rank(self, candidates: tuple[RankingCandidate, ...]) -> list[ModelInfo]:
        """Deterministic static baseline: quality tier -> capability match -> name."""
        def score(c: RankingCandidate) -> tuple[int, int, str]:
            m = c.model
            tier_score = -getattr(m, "capability_tier", 99)  # lower tier = higher quality
            caps = len(getattr(m, "capabilities", []))
            return (tier_score, -caps, m.id)
        return [c.model for c in sorted(candidates, key=score)]

    def _semantic_rank(
        self,
        candidates: tuple[RankingCandidate, ...],
        task_spec: Any,
    ) -> list[ModelInfo]:
        """Semantic ranking using RuVector/SONA embeddings (advisory only)."""
        # For now, fall back to static with capability matching bonus
        # Full RuVector/SONA integration is a follow-up
        base = self._static_rank(candidates)

        # Boost candidates with relevant capabilities for the task
        task_text = str(getattr(task_spec, "prompt", "")) + str(getattr(task_spec, "capabilities", []))
        task_lower = task_text.lower()

        def semantic_boost(m: ModelInfo) -> float:
            caps = getattr(m, "capabilities", [])
            matches = sum(1 for c in caps if c.lower() in task_lower)
            return matches * 0.1

        return sorted(base, key=lambda m: semantic_boost(m), reverse=True)

    def _adaptive_rank(
        self,
        candidates: tuple[RankingCandidate, ...],
        task_spec: Any,
    ) -> list[ModelInfo]:
        """
        Shadow adaptive ranking using learned patterns.

        CRITICAL: This only runs in SHADOW mode. Results are recorded
        for evidence but NEVER affect live selection.
        """
        # Combine static + semantic as baseline
        baseline = self._semantic_rank(candidates, task_spec)

        # Apply learned patterns from history (SONA/RuVector)
        # For now, log the decision for future learning
        task_text = str(getattr(task_spec, "prompt", ""))
        self._history.append({
            "timestamp": time.time(),
            "task_hash": hashlib.sha256(task_text.encode()).hexdigest()[:16],
            "candidates": [c.model.id for c in candidates],
            "baseline_ranking": [m.id for m in baseline],
            "mode": "shadow_adaptive",
        })

        # Trim history
        if len(self._history) > self.config.max_history_size:
            self._history = self._history[-self.config.max_history_size:]

        return baseline  # Shadow: always return baseline for live path

    def rank(
        self,
        eligibility_result: EligibilityResult,
        task_spec: Any,
    ) -> RankerOutput:
        """
        Rank the pre-filtered eligible candidates.

        Precondition: eligibility_result contains ONLY candidates that passed
        the EligibilityGate. This is the single source of truth.
        """
        # Build ranking candidates from eligibility result
        admitted_by_id = {m.id: m for m in eligibility_result.admitted}
        candidates = tuple(
            RankingCandidate(model=admitted_by_id[r.model_id], eligibility_record=r)
            for r in eligibility_result.records
            if r.admitted and r.model_id in admitted_by_id
        )

        if not candidates:
            return RankerOutput(
                ranked=[],
                scores={},
                reasoning={},
                candidate_set_hash="empty",
                eligibility_hash=self._compute_eligibility_hash(eligibility_result),
                mode=self.config.mode,
                shadow=True,
                version=self._version,
                canary_policy=self.config.canary_policy,
            )

        candidate_set_hash = self._compute_candidate_set_hash(candidates)
        eligibility_hash = self._compute_eligibility_hash(eligibility_result)

        # Dispatch to appropriate ranking mode
        if self.config.mode == RankerMode.STATIC:
            ranked = self._static_rank(candidates)
        elif self.config.mode == RankerMode.SEMANTIC:
            ranked = self._semantic_rank(candidates, task_spec)
        elif self.config.mode == RankerMode.SHADOW_ADAPTIVE:
            ranked = self._adaptive_rank(candidates, task_spec)
        else:
            ranked = self._static_rank(candidates)

        # Build scores and reasoning
        scores = {m.id: 1.0 - i * 0.1 for i, m in enumerate(ranked)}
        reasoning = {m.id: f"Rank {i+1} via {self.config.mode.value}" for i, m in enumerate(ranked)}

        # Canary check
        canary_active = (
            self.config.canary_policy == CanaryPolicy.VERSIONED
            and self.config.rollback_enabled
        )

        return RankerOutput(
            ranked=ranked,
            scores=scores,
            reasoning=reasoning,
            candidate_set_hash=candidate_set_hash,
            eligibility_hash=eligibility_hash,
            mode=self.config.mode,
            shadow=not canary_active,  # Shadow unless canary explicitly active
            version=self._version,
            canary_policy=self.config.canary_policy,
        )

    def record_outcome(
        self,
        task_spec: Any,
        selected_model: str,
        success: bool,
        latency_ms: float,
        cost_usd: float,
    ) -> None:
        """Record outcome for learning (SONA/ReasoningBank)."""
        self._history.append({
            "timestamp": time.time(),
            "task_hash": hashlib.sha256(str(task_spec).encode()).hexdigest()[:16],
            "selected_model": selected_model,
            "success": success,
            "latency_ms": latency_ms,
            "cost_usd": cost_usd,
            "mode": "outcome",
        })

    def get_canary_status(self) -> dict[str, Any]:
        """Get canary status for rollback decision."""
        return {
            "version": self._version,
            "policy": self.config.canary_policy.value,
            "rollback_enabled": self.config.rollback_enabled,
            "history_size": len(self._history),
            "shadow_mode": self.config.mode == RankerMode.SHADOW_ADAPTIVE,
        }

    def rollback(self) -> dict[str, Any]:
        """Rollback canary activation."""
        if self.config.canary_policy != CanaryPolicy.VERSIONED:
            return {"status": "error", "error": "No canary active"}

        self.config.canary_policy = CanaryPolicy.DISABLED
        self.config.mode = RankerMode.SEMANTIC
        return {"status": "rolled_back", "new_mode": self.config.mode.value}


def build_adaptive_ranker(
    config: AdaptiveRankerConfig | None = None,
    *,
    ruvector_db_path: str | None = None,
) -> AdaptiveRanker:
    """Factory function for adaptive ranker."""
    return AdaptiveRanker(config=config, ruvector_db_path=ruvector_db_path)
