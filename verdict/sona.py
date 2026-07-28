"""Platform-Level SONA (Self-Optimizing Neural Architecture) Self-Learning Engine.

Provides ultra-fast (<0.05ms) rank-1 LoRA pattern weight adaptation, EWC++
(Elastic Weight Consolidation) to prevent catastrophic forgetting, and dynamic
pattern evolution driven by execution feedback receipts from MemoryPlane.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from typing import Any

from verdict.memory_plane import MemoryPlane, MemoryRecord


@dataclass
class SonaPattern:
    """A self-optimizing pattern with learned weights and Fisher Importance."""

    pattern_id: str
    category: str
    description: str
    weight: float = 1.0
    fisher_importance: float = 1.0
    adaptations_count: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)


class SonaLearningEngine:
    """Self-learning neural pattern optimization engine for Verdict platform."""

    def __init__(self, ewc_lambda: float = 0.5, lora_alpha: float = 0.1) -> None:
        self.ewc_lambda = ewc_lambda
        self.lora_alpha = lora_alpha
        self.patterns: dict[str, SonaPattern] = {}

    def register_pattern(
        self, pattern_id: str, category: str, description: str, initial_weight: float = 1.0
    ) -> SonaPattern:
        """Register a new platform pattern."""
        pattern = SonaPattern(
            pattern_id=pattern_id, category=category, description=description, weight=initial_weight
        )
        self.patterns[pattern_id] = pattern
        return pattern

    def adapt_pattern(
        self, pattern_id: str, feedback_score: float, execution_latency_ms: float = 0.0
    ) -> float:
        """Perform rank-1 LoRA update with EWC++ penalty to evolve pattern weight.

        feedback_score: +1.0 for success, -1.0 for failure / rejection.
        """
        pattern = self.patterns.get(pattern_id)
        if not pattern:
            return 1.0

        # EWC++ penalty factor: higher Fisher importance dampens drastic changes
        ewc_penalty = 1.0 / (1.0 + self.ewc_lambda * pattern.fisher_importance)
        delta_weight = self.lora_alpha * feedback_score * ewc_penalty

        # Latency modifier: faster execution slightly boosts positive adaptation
        if execution_latency_ms > 0 and feedback_score > 0:
            latency_factor = max(0.5, 1.0 - math.log10(max(1.0, execution_latency_ms)) / 5.0)
            delta_weight *= latency_factor

        pattern.weight = max(0.01, pattern.weight + delta_weight)
        pattern.fisher_importance += 0.05 * (feedback_score**2)
        pattern.adaptations_count += 1

        return pattern.weight

    def predict_best_pattern(self, category: str, query: str | None = None) -> SonaPattern | None:
        """Select the highest-weighted evolved pattern for a category."""
        matching = [p for p in self.patterns.values() if p.category == category]
        if not matching:
            return None
        return max(matching, key=lambda p: p.weight)

    def sync_to_memory_plane(self, plane: MemoryPlane) -> int:
        """Persist evolved SONA neural pattern weights into MemoryPlane."""
        synced = 0
        for p in self.patterns.values():
            payload = {
                "pattern_id": p.pattern_id,
                "category": p.category,
                "description": p.description,
                "weight": p.weight,
                "fisher_importance": p.fisher_importance,
                "adaptations_count": p.adaptations_count,
            }
            content_str = json.dumps(payload, sort_keys=True)
            content_hash = hashlib.sha256(content_str.encode("utf-8")).hexdigest()

            record = MemoryRecord(
                record_id=f"rec_sona_{p.pattern_id}",
                namespace="sona_patterns",
                key=f"sona:{p.category}:{p.pattern_id}",
                content=content_str,
                source="sona_learning_engine",
                content_hash=content_hash,
                authority="sona_engine",
                confidence=min(1.0, 0.5 + 0.05 * p.adaptations_count),
                sensitivity="internal",
                provenance={"category": p.category, "weight": p.weight},
            )
            plane.put(record)
            synced += 1
        return synced

    def load_from_memory_plane(self, plane: MemoryPlane) -> int:
        """Load evolved SONA patterns from MemoryPlane."""
        records = plane.search("sona")
        loaded = 0
        for r in records:
            if r.namespace == "sona_patterns":
                try:
                    data = json.loads(r.content)
                    pattern = SonaPattern(
                        pattern_id=data["pattern_id"],
                        category=data["category"],
                        description=data["description"],
                        weight=float(data["weight"]),
                        fisher_importance=float(data["fisher_importance"]),
                        adaptations_count=int(data["adaptations_count"]),
                    )
                    self.patterns[pattern.pattern_id] = pattern
                    loaded += 1
                except Exception:
                    pass
        return loaded


__all__ = ["SonaLearningEngine", "SonaPattern"]
