"""Role ladder shadow event builder (T3: Slice 1).

Builds deterministic role_ladder.shadow records (schema v1) for stdout/CLI.
Side-effect free: no HTTP calls, no writes, no state changes.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

from verdict.role_selection import LadderResult, Role


@dataclass(frozen=True)
class RoleLadderShadowEvent:
    """role_ladder.shadow event (schema_version 1).

    Fields per shadow_event_contract in plan.yaml:
    role, node_id, run_id, correlation_id, mode, H0, H1, H2, H3,
    admitted_set, dynamic_candidates, skips, legacy_pick, dynamic_pick,
    agree, signals_digest, metadata_fixture, live_proof (false for any fixture input)
    """

    schema_version: int = 1
    event_type: str = "role_ladder.shadow"

    role: str = ""
    node_id: str = ""
    run_id: str = ""
    correlation_id: str = ""
    mode: str = "shadow"
    h0: str = ""
    h1: str = ""
    h2: str = ""
    h3: str = ""
    admitted_set: list[str] = None  # type: ignore
    dynamic_candidates: list[dict[str, Any]] = None  # type: ignore
    skips: list[dict[str, str]] = None  # type: ignore
    legacy_pick: str | None = None
    dynamic_pick: str | None = None
    agree: bool | None = None
    signals_digest: str | None = None
    metadata_fixture: bool = False
    live_proof: bool = True
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def __post_init__(self) -> None:
        if self.admitted_set is None:
            object.__setattr__(self, "admitted_set", [])
        if self.dynamic_candidates is None:
            object.__setattr__(self, "dynamic_candidates", [])
        if self.skips is None:
            object.__setattr__(self, "skips", [])

    def to_json(self) -> str:
        """Serialize to JSON with sorted keys for determinism."""
        data = asdict(self)
        return json.dumps(data, sort_keys=True, separators=(",", ":"))


def build_shadow_event(
    ladder_result: LadderResult,
    role: Role,
    node_id: str = "",
    run_id: str = "",
    correlation_id: str = "",
    mode: str = "shadow",
    admitted_set: list[str] | None = None,
    timestamp: str | None = None,
) -> RoleLadderShadowEvent:
    """Build a deterministic role_ladder.shadow event from LadderResult.

    Side-effect free: no HTTP, no writes, no state changes.
    Deterministic for fixed inputs (sorted keys, injected clock via timestamp).
    """
    # Build dynamic_candidates from route_scores
    dynamic_candidates = []
    for rs in ladder_result.route_scores:
        dynamic_candidates.append(
            {
                "route_id": rs.route_id,
                "external_strength": rs.external_strength,
                "strength_source": rs.strength_source,
                "strength_age_s": rs.strength_age_s,
                "reliability_lb": rs.reliability_lb,
                "n": rs.n,
                "confidence": rs.confidence,
                "capacity_class": rs.capacity_class.value,
                "capacity_confidence": rs.capacity_confidence,
                "capacity_evidence": rs.capacity_evidence,
                "pool": rs.pool,
                "quota_status": rs.quota_status,
                "exploration": rs.exploration,
            }
        )

    # Build skips
    skips = [
        {"route_id": skip.route_id, "stage": skip.stage, "reason": skip.reason}
        for skip in ladder_result.skips
    ]

    # Admitted set (from H1)
    admitted = admitted_set or []

    ts = timestamp or datetime.now(timezone.utc).isoformat()

    return RoleLadderShadowEvent(
        role=role.value,
        node_id=node_id,
        run_id=run_id,
        correlation_id=correlation_id,
        mode=mode,
        h0=ladder_result.h0_inventory,
        h1=ladder_result.h1_admitted,
        h2=ladder_result.h2_narrowed,
        h3=ladder_result.h3_ordered,
        admitted_set=admitted,
        dynamic_candidates=dynamic_candidates,
        skips=skips,
        legacy_pick=ladder_result.legacy_pick,
        dynamic_pick=ladder_result.dynamic_pick,
        agree=ladder_result.agree,
        signals_digest=ladder_result.signals_digest,
        metadata_fixture=ladder_result.metadata_fixture,
        live_proof=ladder_result.live_proof,
        timestamp=ts,
    )


__all__ = ["RoleLadderShadowEvent", "build_shadow_event"]
