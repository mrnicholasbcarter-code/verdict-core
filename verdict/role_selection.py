"""Role-based model selection ladder (T3: Slice 1).

Read-only fail-closed ladder library for the planner role.
Reuses: admission (AdmittedSet), capacity.classify, provider_catalog pools,
metadata store lookup_omniroute_id, decision_signals advise_order.
New code: stage composer, frozen QuotaView protocol, build_ladder.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Protocol

from verdict.admission import AdmittedSet
from verdict.capacity import CapacityClass, classify_route_capacity
from verdict.metadata.store import MetadataSnapshot, lookup_omniroute_id
from verdict.orchestration.provider_catalog import backend_pool

# ---------------------------------------------------------------------------
# Role Enum
# ---------------------------------------------------------------------------


class Role(str, Enum):
    """Selection roles. Only PLANNER enabled in Slice 1."""

    PLANNER = "planner"
    IMPLEMENTER = "implementer"
    REVIEWER = "reviewer"
    ESCALATION = "escalation"
    CONTROLLER = "controller"


class RoleNotEnabledError(Exception):
    """Raised when a role is not yet enabled in this slice."""

    def __init__(self, role: Role) -> None:
        self.role = role
        super().__init__(f"Role {role.value} not enabled in Slice 1")


# ---------------------------------------------------------------------------
# LadderResult
# ---------------------------------------------------------------------------


class LadderStatus(str, Enum):
    """Ladder build status."""

    OK = "ok"
    FAIL_CLOSED = "FAIL_CLOSED"


@dataclass(frozen=True)
class RouteScore:
    """Per-route score breakdown for shadow event."""

    route_id: str
    external_strength: float | None
    strength_source: str | None
    strength_age_s: float | None
    reliability_lb: float | None
    n: int | None
    confidence: float | None
    capacity_class: CapacityClass
    capacity_confidence: str
    capacity_evidence: str
    pool: str
    quota_status: str
    exploration: bool


@dataclass(frozen=True)
class LadderSkip:
    """A route that was skipped with reason."""

    route_id: str
    stage: str
    reason: str


@dataclass(frozen=True)
class LadderResult:
    """Result of building a role ladder."""

    status: LadderStatus
    reason: str | None
    ordered_routes: tuple[str, ...]
    skips: tuple[LadderSkip, ...]
    h0_inventory: str
    h1_admitted: str
    h2_narrowed: str
    h3_ordered: str
    route_scores: tuple[RouteScore, ...] = field(default_factory=tuple)
    legacy_pick: str | None = None
    dynamic_pick: str | None = None
    agree: bool | None = None
    signals_digest: str | None = None
    metadata_fixture: bool = False


# ---------------------------------------------------------------------------
# QuotaView Protocol (frozen in T3, no-op in T3-T5)
# ---------------------------------------------------------------------------


class QuotaView(Protocol):
    """Protocol for quota state view. Frozen in T3, implemented in T6."""

    def state(self, pool: str, now: datetime) -> dict[str, Any]:
        """Return quota state for a pool at a given time.

        Returns: {status: open|pressure|cooldown|exhausted, until, reason}
        """
        ...


class OpenQuotaView:
    """No-op QuotaView that always returns open (T3-T5)."""

    def state(self, pool: str, now: datetime) -> dict[str, Any]:
        return {"status": "open", "until": None, "reason": ""}


# ---------------------------------------------------------------------------
# Capacity Policy (D7 rule)
# ---------------------------------------------------------------------------

# D7: For critical roles (planner, reviewer, controller, escalation),
# an assumed-FREE status (absence of evidence) SHALL be treated as UNKNOWN.
# This is enforced only in capacity_policy.role_capacity_allowed.
# The legacy capacity.classify() result (absence of paid evidence is FREE)
# is kept for existing callers.

CRITICAL_ROLES = frozenset({Role.PLANNER, Role.REVIEWER, Role.CONTROLLER, Role.ESCALATION})


def role_capacity_allowed(
    role: Role, capacity_class: CapacityClass, capacity_evidence: str
) -> bool:
    """Check if a capacity class is allowed for a role (D7 rule)."""
    return not (
        role in CRITICAL_ROLES
        and capacity_class == CapacityClass.FREE
        and capacity_evidence == "assumed_free_no_paid_evidence"
    )


# ---------------------------------------------------------------------------
# Inventory and Hash Helpers
# ---------------------------------------------------------------------------


def _sha256_sorted_ids(ids: list[str]) -> str:
    """Compute SHA256 over sorted canonical route IDs."""
    sorted_ids = sorted(ids)
    return hashlib.sha256(json.dumps(sorted_ids).encode()).hexdigest()


def _canonical_route_id(route_id: str) -> str:
    """Normalize route ID (lowercase)."""
    return route_id.lower()


# ---------------------------------------------------------------------------
# Stage S0: Inventory (H0)
# ---------------------------------------------------------------------------


def _stage_s0_inventory(
    admitted: AdmittedSet, inventory_rows: list[dict[str, Any]]
) -> tuple[list[str], str]:
    """S0: Inventory hash (H0) over all discovered route IDs."""
    ids = [_canonical_route_id(r["id"]) for r in inventory_rows]
    h0 = _sha256_sorted_ids(ids)
    return ids, h0


# ---------------------------------------------------------------------------
# Stage S1: Admission (H1) - reuse canonical AdmittedSet
# ---------------------------------------------------------------------------


def _stage_s1_admission(admitted: AdmittedSet) -> tuple[list[str], str]:
    """S1: Admission hash (H1) over admitted route IDs."""
    ids = sorted(_canonical_route_id(rid) for rid in admitted.ids)
    h1 = _sha256_sorted_ids(ids)
    return ids, h1


# ---------------------------------------------------------------------------
# Stage S2: Narrowing (H2) - pool dedup + capacity class filter
# ---------------------------------------------------------------------------


def _stage_s2_narrowing(
    admitted: AdmittedSet,
    inventory_rows: list[dict[str, Any]],
    connections: list[dict[str, Any]],
    role: Role,
    now: datetime,
    openrouter_free_set: Any = None,
) -> tuple[list[str], list[LadderSkip], list[RouteScore], str]:
    """S2: Narrow by pool deduplication and capacity class.

    Rules:
    - Pool deduplication: keep first route per backend pool (by admitted order)
    - Capacity class: filter by role_capacity_allowed (D7 rule)
    - Opaque routes already excluded by admission
    - Pay-per-token (METERED) excluded for planner
    """
    admitted_ids = list(admitted.ids)
    admitted_by_id = {r.route_id: r for r in admitted.records if r.admitted}

    # Build connection lookup by provider
    conn_by_provider = {c["provider"]: c for c in connections}

    # Track seen pools for deduplication
    seen_pools: set[str] = set()
    narrowed: list[str] = []
    skips: list[LadderSkip] = []
    route_scores: list[RouteScore] = []

    for route_id in admitted_ids:
        record = admitted_by_id.get(route_id)
        if not record:
            continue

        # Find inventory row
        row = next((r for r in inventory_rows if _canonical_route_id(r["id"]) == route_id), None)
        if not row:
            skips.append(LadderSkip(route_id, "S2", "missing_inventory_row"))
            continue

        # Find connection
        owned_by = row.get("owned_by", route_id.split("/", 1)[0])
        conn = conn_by_provider.get(owned_by.lower())

        # Classify capacity
        capacity_class, _plan_label, capacity_evidence = classify_route_capacity(
            conn, row, route_id=route_id, openrouter_free_set=openrouter_free_set
        )

        # Check capacity allowed for role (D7)
        if not role_capacity_allowed(role, capacity_class, capacity_evidence):
            skips.append(
                LadderSkip(route_id, "S2", f"capacity_not_allowed_for_role:{capacity_class.value}")
            )
            continue

        # Planner: exclude METERED (pay-per-token)
        if role == Role.PLANNER and capacity_class == CapacityClass.METERED:
            skips.append(LadderSkip(route_id, "S2", "pay_per_token_excluded_for_planner"))
            continue

        # Pool deduplication
        pool = backend_pool(route_id)
        if pool in seen_pools:
            skips.append(LadderSkip(route_id, "S2", f"pool_dedup:{pool}"))
            continue

        seen_pools.add(pool)
        narrowed.append(route_id)

        # Build route score (strength will be filled in S3)
        route_scores.append(
            RouteScore(
                route_id=route_id,
                external_strength=None,
                strength_source=None,
                strength_age_s=None,
                reliability_lb=None,
                n=None,
                confidence=None,
                capacity_class=capacity_class,
                capacity_confidence="high"
                if capacity_evidence != "assumed_free_no_paid_evidence"
                else "low",
                capacity_evidence=capacity_evidence,
                pool=pool,
                quota_status="open",  # T3: no-op QuotaView
                exploration=False,
            )
        )

    h2 = _sha256_sorted_ids(narrowed)
    return narrowed, skips, route_scores, h2


# ---------------------------------------------------------------------------
# Stage S3: Ordering (H3) - external strength from metadata store
# ---------------------------------------------------------------------------


def _stage_s3_ordering(
    narrowed: list[str],
    route_scores: list[RouteScore],
    metadata_snapshot: MetadataSnapshot | None,
    identity_map: Any,
    now: datetime,
    admitted: AdmittedSet,
) -> tuple[list[str], list[RouteScore], str]:
    """S3: Order by external strength from metadata store.

    Fallback to legacy D6 order (capacity tier, then rating) when strength is
    stale or missing. This is advisory - does not change membership.
    """
    if not narrowed:
        return [], route_scores, _sha256_sorted_ids([])

    # Build lookup for route_scores by route_id
    scores_by_id = {rs.route_id: rs for rs in route_scores}

    # Try to get strength from metadata
    if metadata_snapshot is not None and identity_map is not None:
        # Try to enrich with metadata
        for _i, route_id in enumerate(narrowed):
            rs = scores_by_id.get(route_id)
            if not rs:
                continue

            # Look up in metadata store
            lookup = lookup_omniroute_id(
                metadata_snapshot,
                route_id,
                identity_map=identity_map,
                now=now,
                max_age=None,  # No max_age in T3
            )

            if lookup.record is not None and lookup.drop is None:
                # Extract strength from metadata
                # Use AA intelligence index for planner role
                aa_index = lookup.record.scores.aa_intelligence
                if aa_index is not None:
                    strength_value = aa_index.value if hasattr(aa_index, "value") else aa_index
                    strength_float: float | None = (
                        float(strength_value) if isinstance(strength_value, (int, float)) else None
                    )
                    # Check freshness (24h TTL for T5+, but T3 just uses what's there)
                    fetched_at = aa_index.provenance.fetched_at if aa_index.provenance else None
                    age_s = None
                    if fetched_at:
                        try:
                            fetched = datetime.fromisoformat(fetched_at.replace("Z", "+00:00"))
                            age_s = (now - fetched).total_seconds()
                        except (ValueError, AttributeError):
                            pass

                    # Create updated RouteScore with strength
                    scores_by_id[route_id] = RouteScore(
                        route_id=rs.route_id,
                        external_strength=strength_float,
                        strength_source="aa_intelligence",
                        strength_age_s=age_s,
                        reliability_lb=rs.reliability_lb,
                        n=rs.n,
                        confidence=rs.confidence,
                        capacity_class=rs.capacity_class,
                        capacity_confidence=rs.capacity_confidence,
                        capacity_evidence=rs.capacity_evidence,
                        pool=rs.pool,
                        quota_status=rs.quota_status,
                        exploration=rs.exploration,
                    )

    # Sort by external_strength descending (strongest first), then by route_id
    # for determinism. Routes without strength data keep their narrowed position
    # (legacy D6 order) but sort after scored routes.
    has_strength = {rid for rid, rs in scores_by_id.items() if rs.external_strength is not None}

    if has_strength:
        # Scored routes first (by strength desc), then unscoped routes (by narrowed order)
        scored = sorted(
            [rid for rid in narrowed if rid in has_strength],
            key=lambda rid: (-(scores_by_id[rid].external_strength or 0.0), rid),
        )
        unscored = [rid for rid in narrowed if rid not in has_strength]
        ordered = scored + unscored
    else:
        # No strength data: keep legacy narrowed order (D6 tier order)
        ordered = list(narrowed)

    h3 = _sha256_sorted_ids(ordered)

    # Update route_scores with final order
    final_scores = [scores_by_id[rid] for rid in ordered if rid in scores_by_id]

    return ordered, final_scores, h3


# ---------------------------------------------------------------------------
# Pipeline Composer: build_ladder
# ---------------------------------------------------------------------------


def build_ladder(
    role: Role,
    admitted: AdmittedSet,
    inventory_rows: list[dict[str, Any]],
    connections: list[dict[str, Any]],
    signals: Any = None,  # DecisionSignalSetV1 or None
    quota_view: QuotaView | None = None,
    clock: datetime | None = None,
    metadata_snapshot: MetadataSnapshot | None = None,
    identity_map: Any = None,
    openrouter_free_set: Any = None,
) -> LadderResult:
    """Build the role ladder (S0-S3 pipeline).

    This is the main entry point. Read-only, fail-closed, deterministic.
    """
    if role != Role.PLANNER:
        raise RoleNotEnabledError(role)

    now = clock or datetime.now(timezone.utc)
    quota_view = quota_view or OpenQuotaView()

    # S0: Inventory
    _inventory_ids, h0 = _stage_s0_inventory(admitted, inventory_rows)

    # S1: Admission (reuse canonical AdmittedSet)
    admitted_ids, h1 = _stage_s1_admission(admitted)

    # S2: Narrowing (pool dedup + capacity)
    narrowed, skips, route_scores, h2 = _stage_s2_narrowing(
        admitted, inventory_rows, connections, role, now, openrouter_free_set
    )

    # S3: Ordering (external strength)
    ordered, final_scores, h3 = _stage_s3_ordering(
        narrowed, route_scores, metadata_snapshot, identity_map, now, admitted
    )

    # Verify invariants
    if set(ordered) != set(narrowed):
        return LadderResult(
            status=LadderStatus.FAIL_CLOSED,
            reason="candidate_set_violation: H3 != H2",
            ordered_routes=(),
            skips=tuple(skips),
            h0_inventory=h0,
            h1_admitted=h1,
            h2_narrowed=h2,
            h3_ordered=h3,
            metadata_fixture=metadata_snapshot is not None,
        )

    if not set(narrowed).issubset(set(admitted_ids)):
        return LadderResult(
            status=LadderStatus.FAIL_CLOSED,
            reason="candidate_set_violation: H2 not subset of H1",
            ordered_routes=(),
            skips=tuple(skips),
            h0_inventory=h0,
            h1_admitted=h1,
            h2_narrowed=h2,
            h3_ordered=h3,
            metadata_fixture=metadata_snapshot is not None,
        )

    # Empty narrowed set -> FAIL_CLOSED
    if not narrowed:
        return LadderResult(
            status=LadderStatus.FAIL_CLOSED,
            reason="empty_narrowed_set",
            ordered_routes=(),
            skips=tuple(skips),
            h0_inventory=h0,
            h1_admitted=h1,
            h2_narrowed=h2,
            h3_ordered=h3,
            metadata_fixture=metadata_snapshot is not None,
        )

    # Legacy pick (first admitted route)
    legacy_pick = admitted_ids[0] if admitted_ids else None
    # Dynamic pick (first ordered route)
    dynamic_pick = ordered[0] if ordered else None
    agree = legacy_pick == dynamic_pick if legacy_pick and dynamic_pick else None

    return LadderResult(
        status=LadderStatus.OK,
        reason=None,
        ordered_routes=tuple(ordered),
        skips=tuple(skips),
        h0_inventory=h0,
        h1_admitted=h1,
        h2_narrowed=h2,
        h3_ordered=h3,
        route_scores=tuple(final_scores),
        legacy_pick=legacy_pick,
        dynamic_pick=dynamic_pick,
        agree=agree,
        signals_digest=None,  # T3: no signals
        metadata_fixture=metadata_snapshot is not None,
    )


# ---------------------------------------------------------------------------
# Public exports
# ---------------------------------------------------------------------------

__all__ = [
    "LadderResult",
    "LadderSkip",
    "LadderStatus",
    "OpenQuotaView",
    "QuotaView",
    "Role",
    "RoleNotEnabledError",
    "RouteScore",
    "build_ladder",
    "role_capacity_allowed",
]
