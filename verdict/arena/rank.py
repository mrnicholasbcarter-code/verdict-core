"""Arena VR (Verdict Rating) calculation.

Each Arena metric is mapped to a 0-100 percentile (VR) against a reference
set of store-matched models:

    VR_M(x) = 100 * |{r in R : M(r) <= x}| / |R|

- R: store-matched models with usable data for the config selected from the
  role's chain (first config where |R| ≥ _MIN_REF_SET).
- x: the lower bound (lo) of the metric value for the route being rated.
- If |R| < _MIN_REF_SET for the first config, the chain continues to the next.
  If none yields |R| ≥ _MIN_REF_SET, VR is not computable.
- Floors: text 5000 votes, webdev 1500 votes, agent 5000 sessions.
  Rows below the floor are matched but excluded from ranking.
- Source staleness: leaderboard_publish_date older than 45 days → stale.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from verdict.arena.snapshot import (
    CONFIG_AGENT,
    CONFIG_TEXT,
    CONFIG_WEBDEV,
    FLOOR_N,
    STALENESS_DAYS,
    ArenaSlot,
    ArenaSnapshot,
)

if TYPE_CHECKING:
    from verdict.metadata.mapping import IdentityMap

logger = logging.getLogger(__name__)

# Minimum reference-set size before moving to the next chain metric
_MIN_REF_SET = 15

# Effort slot preference order: later entries fall back to earlier ones
_EFFORT_FALLBACK: list[str] = ["default", "low", "medium", "high", "xhigh", "max"]

# Metric chains per role.
# Each entry is a list of config names tried in order; the first with |R| ≥ _MIN_REF_SET wins.
ROLE_CHAINS: dict[str, list[str]] = {
    "planner": [CONFIG_TEXT, CONFIG_WEBDEV],
    "implement": [CONFIG_AGENT, CONFIG_WEBDEV, CONFIG_TEXT],
    "reviewer": [CONFIG_WEBDEV, CONFIG_TEXT],
}


@dataclass(frozen=True)
class ArenaVR:
    """Verdict Rating from Arena data for one route."""

    store_id: str
    vr: float  # 0-100 percentile
    metric: str  # e.g. "arena_text/overall"
    slot: str  # effort slot used
    n: float  # vote_count or session_count
    basis: str  # "arena"
    via: str  # "canonical" | "explicit"
    published: str  # leaderboard_publish_date


def _is_usable(slot: ArenaSlot, config: str, now: datetime) -> tuple[bool, str]:
    """Return (usable, reason). below_floor or stale makes it unusable for ranking."""
    floor = FLOOR_N.get(config, 0.0)
    if slot.n < floor:
        return False, "below_floor"
    try:
        published = datetime.strptime(slot.published, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        days_old = (now - published).days
        if days_old > STALENESS_DAYS:
            return False, "stale"
    except (ValueError, TypeError):
        return False, "unparseable_date"
    return True, ""


def _get_lo(
    snapshot: ArenaSnapshot, store_id: str, config: str, now: datetime
) -> tuple[float, str, str, str] | None:
    """Get the lower-bound value for a store_id+config, checking floors/staleness.

    Returns (lo, effort_slot, via, published) or None.

    Walks effort slots from highest to lowest, using the first slot that passes
    both the floor and staleness checks.  Never borrows from a higher effort
    than available; never skips directly to None after the best slot fails.
    """
    record = snapshot.records.get(store_id, {})
    config_slots = record.get(config)
    if not config_slots:
        return None
    for effort in reversed(_EFFORT_FALLBACK):
        if effort not in config_slots:
            continue
        arena_slot = config_slots[effort]
        usable, _reason = _is_usable(arena_slot, config, now)
        if usable:
            return arena_slot.lo, effort, arena_slot.via, arena_slot.published
    return None


def _build_reference_set(snapshot: ArenaSnapshot, config: str, now: datetime) -> dict[str, float]:
    """Return {store_id: lo_value} for all store-matched models with usable data."""
    result: dict[str, float] = {}
    for sid in snapshot.records:
        lo_data = _get_lo(snapshot, sid, config, now)
        if lo_data is not None:
            result[sid] = lo_data[0]
    return result


def _vr(x: float, reference_values: list[float]) -> float:
    """Percentile of x in reference_values (sorted ascending, 0-100)."""
    if not reference_values:
        return 0.0
    count_at_or_below = sum(1 for v in reference_values if v <= x)
    return 100.0 * count_at_or_below / len(reference_values)


def compute_vr(
    store_id: str, snapshot: ArenaSnapshot, role_key: str, now: datetime | None = None
) -> ArenaVR | None:
    """Compute VR for a store_id under a role.

    Returns None when no usable Arena data is available.
    role_key: "planner" | "implement" | "reviewer"
    """
    clock = now or datetime.now(timezone.utc)
    chain = ROLE_CHAINS.get(role_key, ROLE_CHAINS["implement"])

    for config in chain:
        ref = _build_reference_set(snapshot, config, clock)
        if len(ref) < _MIN_REF_SET:
            continue  # reference set too small; try next config in chain

        lo_data = _get_lo(snapshot, store_id, config, clock)
        if lo_data is None:
            continue  # this model has no usable data for this config

        lo, slot_name, via, published = lo_data
        ref_values = sorted(ref.values())
        vr_value = _vr(lo, ref_values)

        return ArenaVR(
            store_id=store_id,
            vr=vr_value,
            metric=f"{config}/overall",
            slot=slot_name,
            n=snapshot.records[store_id][config][slot_name].n,
            basis="arena",
            via=via,
            published=published,
        )

    return None


def vr_for_route(
    route_id: str,
    snapshot: ArenaSnapshot,
    role_key: str,
    identity_map: IdentityMap | None = None,
    now: datetime | None = None,
) -> ArenaVR | None:
    """Get VR for a routing route_id.

    Resolves route_id → store_id via identity_map, then computes VR.
    Returns None when the route cannot be resolved to a store_id.
    """
    clock = now or datetime.now(timezone.utc)
    store_id = _resolve_store_id(route_id, identity_map)
    if store_id is None:
        return None
    return compute_vr(store_id, snapshot, role_key, clock)


def _resolve_store_id(route_id: str, identity_map: IdentityMap | None) -> str | None:
    """Map a routing route_id to a models.dev store_id via the identity map.

    Returns None on a mapping miss or when no identity_map is provided.
    Route ids and store ids are different schemas; never treats one as the other.
    """
    if identity_map is None:
        return None
    store_id = identity_map.models_dev_id_for(route_id)
    return store_id  # None on miss


def reference_set_sizes(snapshot: ArenaSnapshot, now: datetime | None = None) -> dict[str, int]:
    """Return {role_key: ref_set_size} for all roles in ROLE_CHAINS.

    Public interface for callers that only need the size, not the full set.
    Returns 0 for any role whose chain has no config with |R| >= _MIN_REF_SET.
    """
    clock = now or datetime.now(timezone.utc)
    result: dict[str, int] = {}
    for role_key, chain in ROLE_CHAINS.items():
        for config in chain:
            ref = _build_reference_set(snapshot, config, clock)
            if len(ref) >= _MIN_REF_SET:
                result[role_key] = len(ref)
                break
        if role_key not in result:
            result[role_key] = 0
    return result


def build_reference_sets(
    snapshot: ArenaSnapshot, now: datetime | None = None
) -> dict[str, list[str]]:
    """Build reference sets per role and store in the snapshot (mutates).

    Should be called after fetch resolves rows, before saving the snapshot.
    """
    clock = now or datetime.now(timezone.utc)
    sets: dict[str, list[str]] = {}
    for role_key, chain in ROLE_CHAINS.items():
        for config in chain:
            ref = _build_reference_set(snapshot, config, clock)
            if len(ref) >= _MIN_REF_SET:
                sets[role_key] = sorted(ref.keys())
                break
        if role_key not in sets:
            sets[role_key] = []
    snapshot.reference_sets.update(sets)
    return sets
