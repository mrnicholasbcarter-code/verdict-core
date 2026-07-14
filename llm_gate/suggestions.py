"""Suggestion service for evidence-backed improvements from historic telemetry."""

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


@dataclass
class Suggestion:
    id: str
    category: str
    title: str
    description: str
    evidence_references: list[str]
    confidence: float
    expected_impact: str
    novelty: str
    expiry: str
    proposed_next_experiment: str
    created_at: str


class SuggestionService:
    def __init__(self, log_path: str = "llm-gate-decisions.jsonl"):
        self.log_path = Path(log_path)

    def generate_suggestions(self) -> list[Suggestion]:
        if not self.log_path.exists():
            return []

        latency_faults = []
        escalations = []
        headroom_failures = []

        try:
            with open(self.log_path, encoding="utf-8") as f:
                for line in f:
                    if not line.strip():
                        continue
                    try:
                        record = json.loads(line)
                        if "latency_ms" in record and record["latency_ms"] > 2500:
                            latency_faults.append(record)
                        if "effective_tier" in record and record.get("escalated", False):
                            escalations.append(record)
                        if "headroom_pct" in record and record["headroom_pct"] < 0.15:
                            headroom_failures.append(record)
                    except json.JSONDecodeError:
                        continue
        except Exception:
            pass  # Failure is isolated from readiness

        suggestions = []

        # Generator 1: Latency faults
        if len(latency_faults) > 10:
            models_affected = list(set(r.get("model_chosen", "unknown") for r in latency_faults))
            task_hashes = [r.get("task_hash", "") for r in latency_faults[:3]]
            suggestions.append(
                Suggestion(
                    id="SUG-LAT-001",
                    category="performance",
                    title="High Latency Routing Detected",
                    description=f"Over 10 requests were routed to {models_affected} with latency exceeding 2500ms.",
                    evidence_references=task_hashes,
                    confidence=0.92,
                    expected_impact="High - Better perceived performance for end users",
                    novelty="first_occurrence",
                    expiry="7d",
                    proposed_next_experiment="Evaluate adding a lightweight T3 provider to the configuration.",
                    created_at=datetime.now(timezone.utc).isoformat(),
                )
            )

        # Generator 2: Escalations
        if len(escalations) > 5:
            reasons = list(set(r.get("escalation_reason", "unknown") for r in escalations))
            task_hashes = [r.get("task_hash", "") for r in escalations[:3]]
            suggestions.append(
                Suggestion(
                    id="SUG-ESC-001",
                    category="reliability",
                    title="Frequent Provider Escalations",
                    description=f"Frequent escalations observed. Common reasons: {reasons[:3]}.",
                    evidence_references=task_hashes,
                    confidence=0.88,
                    expected_impact="Medium - Cost control and predictable execution time",
                    novelty="first_occurrence",
                    expiry="14d",
                    proposed_next_experiment="Verify credentials of the failing provider or disable it proactively.",
                    created_at=datetime.now(timezone.utc).isoformat(),
                )
            )

        # Generator 3: Headroom
        if len(headroom_failures) > 3:
            task_hashes = [r.get("task_hash", "") for r in headroom_failures[:3]]
            suggestions.append(
                Suggestion(
                    id="SUG-HDR-001",
                    category="capacity",
                    title="Critical API Headroom Detected",
                    description="Several queries ran while API headroom was under 15%. This indicates approaching rate limit failures.",
                    evidence_references=task_hashes,
                    confidence=0.95,
                    expected_impact="High - Prevent complete API outages during bursts",
                    novelty="recent",
                    expiry="3d",
                    proposed_next_experiment="Investigate burst load or increase global rate limit buffering.",
                    created_at=datetime.now(timezone.utc).isoformat(),
                )
            )

        return suggestions
