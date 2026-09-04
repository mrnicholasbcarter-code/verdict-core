"""Waiver model for launch-gate findings and gate-unavailable outages.

Implements FR-010/FR-011: every bypass of a blocking check must be an
explicit, attributed, recorded waiver — never silent or automatic. A
`finding`-scope waiver excuses one specific finding; a `gate_unavailable`-
scope waiver excuses a full gate outage and requires a named emergency
approver (see `emergency_approvers.py`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

from verdict.release.emergency_approvers import is_emergency_approver

WaiverScope = Literal["finding", "gate_unavailable"]


class WaiverValidationError(ValueError):
    """Raised when a waiver does not satisfy FR-010/FR-011."""


@dataclass(frozen=True, slots=True)
class Waiver:
    scope: WaiverScope
    reviewer: str
    reason: str
    finding_id: str | None = None
    is_emergency_approver: bool = False
    recorded_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self) -> None:
        if not self.reviewer.strip():
            raise WaiverValidationError("waiver.reviewer must be non-empty")
        if not self.reason.strip():
            raise WaiverValidationError("waiver.reason must be non-empty")

        if self.scope == "finding":
            if not self.finding_id or not self.finding_id.strip():
                raise WaiverValidationError("finding-scope waiver requires a non-empty finding_id")
        elif self.scope == "gate_unavailable":
            if self.finding_id is not None:
                raise WaiverValidationError("gate_unavailable-scope waiver must not set finding_id")
            if not self.is_emergency_approver:
                raise WaiverValidationError(
                    "gate_unavailable-scope waiver requires is_emergency_approver=True"
                )
            if not is_emergency_approver(self.reviewer):
                raise WaiverValidationError(
                    f"reviewer {self.reviewer!r} is not a registered emergency "
                    "approver (see verdict/release/emergency_approvers.py)"
                )
        else:  # pragma: no cover - Literal exhaustiveness guard
            raise WaiverValidationError(f"unknown waiver scope: {self.scope!r}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "scope": self.scope,
            "finding_id": self.finding_id,
            "reviewer": self.reviewer,
            "reason": self.reason,
            "recorded_at": self.recorded_at.isoformat(),
            "is_emergency_approver": self.is_emergency_approver,
        }
