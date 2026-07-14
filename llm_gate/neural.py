"""Legacy learned-router compatibility shim.

The public package no longer reads private SQLite state. This module keeps the
old class name available for callers that still import it, but routing is now
owned by :class:`llm_gate.intelligence.IntelligenceService`.
"""

from __future__ import annotations


class LearnedRouter:
    """Compatibility shim that no longer reaches into private databases."""

    def __init__(self, *_: object, **__: object) -> None:
        self.q_table: dict[str, dict[str, float]] = {}

    def predict_optimal_model(
        self, task: str, baseline_tier: int, candidates: list[str]
    ) -> tuple[int, str]:
        """Return the baseline tier and a deterministic compatibility reason."""
        if not candidates:
            return baseline_tier, "deterministic safety floor"
        return baseline_tier, f"compatibility floor: {candidates[0]}"
