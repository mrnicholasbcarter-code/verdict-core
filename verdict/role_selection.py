"""Role-based model selection ladder (T3: Slice 1).

Read-only fail-closed ladder library for the planner role.
Reuses: admission (AdmittedSet), the eligibility ladder (canonical connection
resolution, capacity classes, planner floors, D6 rank), provider_catalog pools,
the metadata store, autodev's live planner choice, and decision_signals
advise_order. New code: stage composer, frozen QuotaView protocol, build_ladder.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Protocol

from verdict.admission import AdmittedSet, canonical_route_id
from verdict.capacity import CapacityClass
from verdict.decision_signals.advisory import advise_order
from verdict.identify_rank import RATING_MAX_AGE
from verdict.metadata.mapping import IdentityMap
from verdict.metadata.store import MetadataSnapshot, lookup_omniroute_id
from verdict.models import ModelInfo
from verdict.orchestration.contracts import RouteVerdict, TaskRequirements
from verdict.orchestration.provider_catalog import aliased_pools_for

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
    live_proof: bool = True


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

CRITICAL_ROLES = frozenset({Role.PLANNER, Role.REVIEWER, Role.CONTROLLER, Role.ESCALATION})


def role_capacity_allowed(
    role: Role, capacity_class: CapacityClass, capacity_evidence: str
) -> bool:
    """Single enforcement point for capacity per role (D7).

    Unknown capacity is never healthy, so it never enters a critical-role
    ladder. An assumed-FREE result (no paid evidence) counts as UNKNOWN for
    critical roles; the legacy classify() result is unchanged for other callers.
    """
    if role not in CRITICAL_ROLES:
        return capacity_class != CapacityClass.UNKNOWN
    if capacity_class == CapacityClass.UNKNOWN:
        return False
    return not (
        capacity_class == CapacityClass.FREE
        and capacity_evidence == "assumed_free_no_paid_evidence"
    )


# The planner requirements autodev's live planner selection uses
# (autodev_run._live_planner_models, BOD-393), minus its D22 pool exclusion:
# D22 is applied by the legacy choice itself, never by the dynamic ladder.
PLANNER_REQUIREMENTS = TaskRequirements(
    required_capabilities=frozenset({"reasoning"}),
    coding=True,
    reasoning=True,
    frontier_worthy=True,
    role_ordering="strongest_first",
)

# Planner strength is the general intelligence index from Core's store.
_PLANNER_STRENGTH_METRIC = "aa_intelligence"


# ---------------------------------------------------------------------------
# Hash helpers
# ---------------------------------------------------------------------------


def _sha256_sorted_ids(ids: Sequence[str]) -> str:
    """SHA-256 over the sorted canonical route ids leaving a stage."""
    return hashlib.sha256(json.dumps(sorted(ids)).encode()).hexdigest()


def quota_pool(route_id: str, provider: str) -> str:
    """Quota pool for a route: an aliased backend pool, else its connection.

    Gateway route ids are kept for admission and hashes. Pool identity comes
    from canonical provider evidence (the resolved connection provider), so
    ``codex/gpt-6-luna``, ``cx/...``, ``cxa/...`` and the unprefixed
    ``gpt-6-astra`` (owned_by=codex) share one pool. Cross-prefix aliases
    (agy/antigravity, the shared ``:free`` pool) win over the provider.
    """
    aliased = aliased_pools_for(frozenset({route_id}))
    if aliased:
        return next(iter(aliased))
    return provider or route_id.split("/", 1)[0].lower()


def _capacity_confidence(evidence: str) -> str:
    if evidence.startswith("assumed"):
        return "assumed"
    if evidence == "billing_402_metered":
        return "observed"
    return "declared"


# ---------------------------------------------------------------------------
# Eligibility evidence (canonical ladder, read-only)
# ---------------------------------------------------------------------------


def _ladder_verdicts(
    admitted: AdmittedSet,
    inventory_rows: Sequence[Mapping[str, Any]],
    connections: Sequence[Mapping[str, Any]],
    *,
    now: datetime,
    metadata_snapshot: MetadataSnapshot | None,
    identity_map: IdentityMap | None,
    openrouter_free_set: Any,
    health_cache: Any,
    state_path: Path | None,
) -> list[RouteVerdict]:
    """Planner verdicts from the canonical eligibility ladder, never probed.

    ``evaluate`` reuses the ladder's connection resolution (owned_by aliases,
    first active connection), capacity classes (billing override included),
    planner floors and the D6 rank. It does not probe and does not persist.
    """
    from verdict.orchestration.eligibility import EligibilityLadder
    from verdict.paths import verdict_home
    from verdict.subagent_selection import HealthResult

    ladder = EligibilityLadder(
        inventory_rows,
        connections,
        lambda _route: HealthResult(False, "not_probed"),
        state_path or (verdict_home() / "orchestration-health.json"),
        prefer_providers=(),
        admitted=admitted,
        health_cache=health_cache,
        openrouter_free_set=openrouter_free_set,
        metadata=metadata_snapshot,
        identity_map=identity_map,
        max_probes_per_select=0,
    )
    return list(ladder.evaluate(PLANNER_REQUIREMENTS, now=now))


def _legacy_planner_pick(
    verdicts: Sequence[RouteVerdict],
    metadata: MetadataSnapshot | None,
    identity_map: IdentityMap | None,
    *,
    now: datetime | None = None,
) -> str | None:
    """The top ADR-051 frontier-band planner route for this inventory (BOD-406/BOD-393).

    Uses the same band logic as the live autodev planner (ADR-051) but works
    without real health probes, so it is safe to call from ``build_ladder``
    where health is not_probed.  Returns None when band selection fails.
    """
    from verdict.orchestration.contracts import CapacityClass
    from verdict.planner_band import (
        PlannerBandError,
        compute_planner_bands,
        load_planner_band_config,
        select_planner_routes,
    )

    inventory: list[str] = []
    capacity_classes: dict[str, CapacityClass] = {}
    for v in verdicts:
        route_id = str(v.route_id)
        if "/" not in route_id:
            continue
        inventory.append(route_id)
        capacity_classes[route_id] = getattr(v, "capacity_class", CapacityClass.UNKNOWN)
    if not inventory:
        return None

    try:
        config = load_planner_band_config()
    except Exception:
        return None
    clock = now or datetime.now(timezone.utc)
    band = compute_planner_bands(inventory, metadata, identity_map, config=config, now=clock)

    # Admit non-metered routes (ADR-049: FREE, SUBSCRIPTION, UNKNOWN).
    _non_metered = frozenset(
        {CapacityClass.FREE, CapacityClass.SUBSCRIPTION, CapacityClass.UNKNOWN}
    )
    admitted = [
        r for r in inventory if capacity_classes.get(r, CapacityClass.UNKNOWN) in _non_metered
    ]
    try:
        # top_n=1: advisory pick; avoids the two-family constraint (top_n >= 2).
        selected = select_planner_routes(
            admitted, capacity_classes, band, metadata, identity_map, top_n=1, now=clock
        )
        return selected[0][0] if selected else None
    except PlannerBandError:
        return None


# ---------------------------------------------------------------------------
# Stage S2: narrowing (capacity, planner floors, quota pool)
# ---------------------------------------------------------------------------


def _stage_s2_narrowing(
    role: Role,
    admitted_ids: frozenset[str],
    verdicts: Sequence[RouteVerdict],
    quota_view: QuotaView,
    now: datetime,
) -> tuple[list[str], list[LadderSkip], list[RouteScore]]:
    """Remove-only narrowing of the admitted set, in the legacy D6 order.

    The first route of each quota pool in D6 order is the best-ordered one,
    so later routes of that pool are skipped with ``pool_dedup:<pool>``.
    """
    by_id = {canonical_route_id(v.route_id): v for v in verdicts}
    ranked = sorted(
        (rid for rid in admitted_ids if rid in by_id and by_id[rid].rank is not None),
        key=lambda rid: by_id[rid].rank or 0,
    )
    unranked = sorted(rid for rid in admitted_ids if rid not in ranked)
    seen_pools: set[str] = set()
    narrowed: list[str] = []
    skips: list[LadderSkip] = []
    scores: list[RouteScore] = []
    for rid in ranked + unranked:
        verdict = by_id.get(rid)
        if verdict is None:
            skips.append(LadderSkip(rid, "S2", "missing_inventory_row"))
            continue
        capacity, evidence = verdict.capacity_class, verdict.capacity_evidence
        if not role_capacity_allowed(role, capacity, evidence):
            skips.append(LadderSkip(rid, "S2", f"capacity_not_allowed_for_role:{capacity.value}"))
            continue
        if capacity == CapacityClass.METERED:
            skips.append(LadderSkip(rid, "S2", "pay_per_token_excluded_for_planner"))
            continue
        if verdict.rank is None:
            skips.append(LadderSkip(rid, "S2", f"planner_eligibility:{verdict.reason}"))
            continue
        pool = quota_pool(rid, verdict.provider)
        if pool in seen_pools:
            skips.append(LadderSkip(rid, "S2", f"pool_dedup:{pool}"))
            continue
        seen_pools.add(pool)
        narrowed.append(rid)
        scores.append(
            RouteScore(
                route_id=rid,
                external_strength=None,
                strength_source=None,
                strength_age_s=None,
                reliability_lb=None,
                n=None,
                confidence=None,
                capacity_class=capacity,
                capacity_confidence=_capacity_confidence(evidence),
                capacity_evidence=evidence,
                pool=pool,
                quota_status=str(quota_view.state(pool, now).get("status", "open")),
                exploration=False,
            )
        )
    return narrowed, skips, scores


# ---------------------------------------------------------------------------
# Stage S3: advisory order through advise_order
# ---------------------------------------------------------------------------


def _role_strength(
    route_id: str,
    metadata_snapshot: MetadataSnapshot | None,
    identity_map: IdentityMap | None,
    now: datetime,
) -> tuple[float | None, float | None]:
    """Fresh planner strength and its age in seconds; (None, age) when stale."""
    if metadata_snapshot is None:
        return None, None
    lookup = lookup_omniroute_id(
        metadata_snapshot, route_id, identity_map=identity_map, now=now, max_age=RATING_MAX_AGE
    )
    item = getattr(lookup.record.scores, _PLANNER_STRENGTH_METRIC) if lookup.record else None
    if item is None or isinstance(item.value, bool) or not isinstance(item.value, (int, float)):
        return None, None
    try:
        fetched = datetime.fromisoformat(item.provenance.fetched_at.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return None, None
    age = now - fetched
    if fetched.tzinfo is None or age.total_seconds() < 0 or age > RATING_MAX_AGE:
        return None, age.total_seconds() if fetched.tzinfo else None
    return float(item.value), age.total_seconds()


def _stage_s3_ordering(
    narrowed: list[str],
    scores: list[RouteScore],
    verdicts: Sequence[RouteVerdict],
    metadata_snapshot: MetadataSnapshot | None,
    identity_map: IdentityMap | None,
    now: datetime,
    signals: Any,
) -> tuple[list[str], list[RouteScore], str | None]:
    """Order the narrowed set with advise_order over the D6 baseline.

    Strength reaches the order only as ``quality_confidence`` inside
    advise_order. With no fresh strength on any candidate the advice has no
    evidence, which the spec treats as low confidence: the D6 order stays.
    """
    by_id = {canonical_route_id(v.route_id): v for v in verdicts}
    enriched: dict[str, RouteScore] = {}
    candidates: list[ModelInfo] = []
    for rs in scores:
        strength, age = _role_strength(rs.route_id, metadata_snapshot, identity_map, now)
        enriched[rs.route_id] = replace(
            rs,
            external_strength=strength,
            strength_source=_PLANNER_STRENGTH_METRIC if strength is not None else None,
            strength_age_s=age,
        )
        tier = by_id[rs.route_id].capability_tier
        candidates.append(
            ModelInfo(
                id=rs.route_id,
                provider=rs.pool,
                capability_tier=tier if tier is not None else 3,
                quality_confidence=strength,
            )
        )
    has_evidence = any(s.external_strength is not None for s in enriched.values())
    ordered, influence = advise_order(
        candidates, signals, protected=False, min_confidence=None if has_evidence else float("inf")
    )
    ids = [m.id for m in ordered]
    if sorted(ids) != sorted(narrowed):
        raise RuntimeError("advise_order violated membership invariant")
    return ids, [enriched[rid] for rid in ids], influence.signals_digest


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
    identity_map: IdentityMap | None = None,
    openrouter_free_set: Any = None,
    health_cache: Any = None,
    state_path: Path | None = None,
    fixture_input: bool = False,
) -> LadderResult:
    """Build the role ladder (S0-S3). Read-only, fail-closed, deterministic.

    ``fixture_input`` is the caller's explicit statement that inventory or
    metadata came from a fixture; it sets ``metadata_fixture`` and clears
    ``live_proof``. Snapshot presence never decides provenance.
    """
    if role != Role.PLANNER:
        raise RoleNotEnabledError(role)
    now = clock or datetime.now(timezone.utc)
    quota_view = quota_view or OpenQuotaView()

    h0 = _sha256_sorted_ids([canonical_route_id(str(r["id"])) for r in inventory_rows])
    admitted_ids = frozenset(canonical_route_id(rid) for rid in admitted.ids)
    h1 = _sha256_sorted_ids(list(admitted_ids))

    verdicts = _ladder_verdicts(
        admitted,
        inventory_rows,
        connections,
        now=now,
        metadata_snapshot=metadata_snapshot,
        identity_map=identity_map,
        openrouter_free_set=openrouter_free_set,
        health_cache=health_cache,
        state_path=state_path,
    )
    narrowed, skips, scores = _stage_s2_narrowing(role, admitted_ids, verdicts, quota_view, now)
    h2 = _sha256_sorted_ids(narrowed)
    ordered, final_scores, digest = _stage_s3_ordering(
        narrowed, scores, verdicts, metadata_snapshot, identity_map, now, signals
    )
    h3 = _sha256_sorted_ids(ordered)

    def closed(reason: str) -> LadderResult:
        return LadderResult(
            status=LadderStatus.FAIL_CLOSED,
            reason=reason,
            ordered_routes=(),
            skips=tuple(skips),
            h0_inventory=h0,
            h1_admitted=h1,
            h2_narrowed=h2,
            h3_ordered=h3,
            signals_digest=digest,
            metadata_fixture=fixture_input,
            live_proof=not fixture_input,
        )

    if set(ordered) != set(narrowed):
        return closed("candidate_set_violation: H3 != H2")
    if not set(narrowed) <= admitted_ids:
        return closed("candidate_set_violation: H2 not subset of H1")
    if not ordered:
        return closed("empty_narrowed_set")

    legacy_pick = _legacy_planner_pick(verdicts, metadata_snapshot, identity_map, now=now)
    dynamic_pick = ordered[0]
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
        agree=(legacy_pick == dynamic_pick) if legacy_pick is not None else None,
        signals_digest=digest,
        metadata_fixture=fixture_input,
        live_proof=not fixture_input,
    )


# ---------------------------------------------------------------------------
# Public exports
# ---------------------------------------------------------------------------

__all__ = [
    "PLANNER_REQUIREMENTS",
    "LadderResult",
    "LadderSkip",
    "LadderStatus",
    "OpenQuotaView",
    "QuotaView",
    "Role",
    "RoleNotEnabledError",
    "RouteScore",
    "build_ladder",
    "quota_pool",
    "role_capacity_allowed",
]
