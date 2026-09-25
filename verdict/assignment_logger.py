"""Assignment logging for G6.1: per-assignment audit trail.

This module provides structured logging for every model assignment decision,
recording the full context needed to audit routing behavior.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

# JSON Schema for assignment log records
ASSIGNMENT_LOG_SCHEMA = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "title": "Assignment Log Record",
    "description": "Per-assignment log for G6.1: model, provider, availability, cost, reason, fallback, verification",
    "type": "object",
    "required": [
        "assignment_id",
        "timestamp",
        "model",
        "provider",
        "availability_snapshot",
        "estimated_cost_usd",
        "reason",
    ],
    "properties": {
        "assignment_id": {"type": "string", "description": "Unique identifier for this assignment"},
        "timestamp": {
            "type": "string",
            "format": "date-time",
            "description": "ISO 8601 timestamp of assignment",
        },
        "model": {"type": "string", "description": "Selected model identifier"},
        "provider": {"type": "string", "description": "Provider name"},
        "availability_snapshot": {
            "type": "object",
            "description": "Availability state at decision time",
            "required": ["observed_at", "state"],
            "properties": {
                "observed_at": {"type": "string"},
                "state": {"type": "string"},
                "candidates": {"type": "array"},
            },
        },
        "estimated_cost_usd": {
            "type": "number",
            "minimum": 0,
            "description": "Estimated cost in USD",
        },
        "actual_cost_usd": {
            "type": ["number", "null"],
            "minimum": 0,
            "description": "Actual cost after execution (null if not yet executed)",
        },
        "reason": {
            "type": "string",
            "description": "Machine-readable reason code for this assignment",
        },
        "reason_detail": {"type": "string", "description": "Human-readable explanation"},
        "fallback_from": {
            "type": ["string", "null"],
            "description": "Previous model if this is a fallback/escalation",
        },
        "escalation_depth": {
            "type": "integer",
            "minimum": 0,
            "description": "Number of escalations (0 for initial assignment)",
        },
        "verification_result": {
            "type": ["object", "null"],
            "description": "Verification check results if executed",
            "properties": {
                "status": {"type": "string", "enum": ["passed", "failed", "skipped", "unknown"]},
                "checks": {"type": "array"},
            },
        },
        "task_id": {"type": "string", "description": "Associated task identifier"},
        "receipt_id": {"type": "string", "description": "Routing receipt identifier"},
    },
}


@dataclass
class AssignmentLogRecord:
    """Structured assignment log record."""

    assignment_id: str
    timestamp: str
    model: str
    provider: str
    availability_snapshot: dict[str, Any]
    estimated_cost_usd: float
    reason: str
    reason_detail: str = ""
    actual_cost_usd: float | None = None
    fallback_from: str | None = None
    escalation_depth: int = 0
    verification_result: dict[str, Any] | None = None
    task_id: str | None = None
    receipt_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to JSON-compatible dict."""
        data = {
            "assignment_id": self.assignment_id,
            "timestamp": self.timestamp,
            "model": self.model,
            "provider": self.provider,
            "availability_snapshot": self.availability_snapshot,
            "estimated_cost_usd": self.estimated_cost_usd,
            "reason": self.reason,
        }
        if self.reason_detail:
            data["reason_detail"] = self.reason_detail
        if self.actual_cost_usd is not None:
            data["actual_cost_usd"] = self.actual_cost_usd
        if self.fallback_from:
            data["fallback_from"] = self.fallback_from
        if self.escalation_depth:
            data["escalation_depth"] = self.escalation_depth
        if self.verification_result:
            data["verification_result"] = self.verification_result
        if self.task_id:
            data["task_id"] = self.task_id
        if self.receipt_id:
            data["receipt_id"] = self.receipt_id
        return data

    def to_json(self) -> str:
        """Serialize to JSON string."""
        return json.dumps(self.to_dict(), sort_keys=True)


def create_assignment_log(
    model: str,
    provider: str,
    availability_snapshot: dict[str, Any],
    estimated_cost_usd: float,
    reason: str,
    *,
    reason_detail: str = "",
    actual_cost_usd: float | None = None,
    fallback_from: str | None = None,
    escalation_depth: int = 0,
    verification_result: dict[str, Any] | None = None,
    task_id: str | None = None,
    receipt_id: str | None = None,
) -> AssignmentLogRecord:
    """Create an assignment log record with generated ID and timestamp."""
    return AssignmentLogRecord(
        assignment_id=str(uuid4()),
        timestamp=datetime.now(timezone.utc).isoformat(),
        model=model,
        provider=provider,
        availability_snapshot=availability_snapshot,
        estimated_cost_usd=estimated_cost_usd,
        reason=reason,
        reason_detail=reason_detail,
        actual_cost_usd=actual_cost_usd,
        fallback_from=fallback_from,
        escalation_depth=escalation_depth,
        verification_result=verification_result,
        task_id=task_id,
        receipt_id=receipt_id,
    )


def create_sample_log() -> dict[str, Any]:
    """Create a sample assignment log using the flagship demo fixture."""
    from verdict.contracts import AvailabilitySnapshot
    from verdict.flagship_demo import build_demo_result

    demo = build_demo_result()
    decision = demo["decision"]

    # Extract the selected route
    selected = decision.get("selected_route", {})
    model = selected.get("model", "demo/frontier-tools")
    provider = selected.get("provider", "demo")

    # Build availability snapshot
    snapshot = AvailabilitySnapshot(
        observed_at=datetime.now(timezone.utc).isoformat(),
        state="healthy",
        candidates=[],
        source="flagship_demo_fixture",
    )

    record = create_assignment_log(
        model=model,
        provider=provider,
        availability_snapshot=snapshot.to_dict(),
        estimated_cost_usd=0.001,
        reason="selected",
        reason_detail="Least-cost eligible candidate with required capabilities",
        verification_result={"status": "passed", "checks": ["unit_tests", "schema_validation"]},
        task_id="fixture-issue-35",
    )

    return record.to_dict()


__all__ = [
    "ASSIGNMENT_LOG_SCHEMA",
    "AssignmentLogRecord",
    "create_assignment_log",
    "create_sample_log",
]
