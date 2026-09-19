"""Conflict resolution for capacity evidence (BOD-129).

Gateway vs direct conflicts keep both observations. Prefer by authority rank,
freshness, and account identity — NEVER average remaining percentages.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone

from verdict.capacity_models import (
    CapacityEvidenceError,
    CapacityPool,
    CapacitySnapshot,
    ConflictResolution,
    ConnectionIdentity,
)


def _utc(value: datetime | None = None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if value.tzinfo is None:
        raise CapacityEvidenceError("datetimes must be timezone-aware")
    return value.astimezone(timezone.utc)


def same_account(a: ConnectionIdentity, b: ConnectionIdentity) -> bool:
    return (
        a.provider_id == b.provider_id
        and a.account_id == b.account_id
        and (a.workspace_id or "") == (b.workspace_id or "")
    )


def pool_key(identity: ConnectionIdentity, pool: CapacityPool) -> tuple[str, str, str, str]:
    """Concrete pool key — shared_pool_id collapses product slices."""

    shared = pool.shared_pool_id or pool.pool_id
    return (identity.provider_id, identity.account_id, shared, pool.pool_id)


@dataclass(frozen=True)
class ResolvedCapacity:
    """Resolved view: preferred snapshots plus retained conflicts."""

    snapshots: tuple[CapacitySnapshot, ...]
    conflicts: tuple[ConflictResolution, ...]
    shared_pool_ids: frozenset[str]

    def to_dict(self) -> dict[str, object]:
        return {
            "snapshots": [item.to_dict() for item in self.snapshots],
            "conflicts": [item.to_dict() for item in self.conflicts],
            "shared_pool_ids": sorted(self.shared_pool_ids),
        }


def _is_fresher(candidate: CapacitySnapshot, incumbent: CapacitySnapshot, now: datetime) -> bool:
    """True when candidate should beat incumbent on freshness alone."""

    # Explicit fresh_until: expired evidence loses to non-expired.
    cand_expired = candidate.fresh_until is not None and candidate.fresh_until < now
    inc_expired = incumbent.fresh_until is not None and incumbent.fresh_until < now
    if cand_expired and not inc_expired:
        return False
    if inc_expired and not cand_expired:
        return True
    return candidate.observed_at > incumbent.observed_at


def prefer_snapshot(
    left: CapacitySnapshot,
    right: CapacitySnapshot,
    *,
    now: datetime | None = None,
) -> ConflictResolution:
    """Choose preferred observation without averaging.

    Rules (in order):
    1. Different accounts → keep both; prefer neither for the other's account
       (caller groups by account). Here we still pick a winner for the pair
       using authority+freshness, but reason notes account mismatch.
    2. Higher authority (lower rank) wins if both are fresh for the same account.
    3. Fresher observation wins when authority ties or stale official loses.
    4. Never average remaining_pct / units.
    """

    moment = _utc(now)
    same = same_account(left.identity, right.identity)

    # Stale lower-authority vs fresh higher-authority, etc.
    left_beats = False
    reason: str

    if not same:
        # Account identity mismatch: still record a deterministic preference for
        # telemetry, but consumers must treat pools as distinct.
        if left.observed_at >= right.observed_at:
            left_beats = True
            reason = "distinct_accounts_prefer_left_by_observed_at"
        else:
            left_beats = False
            reason = "distinct_accounts_prefer_right_by_observed_at"
    else:
        left_fresher = _is_fresher(left, right, moment)
        right_fresher = _is_fresher(right, left, moment)
        if left.authority.rank < right.authority.rank:
            if _is_fresher(right, left, moment) and (
                left.fresh_until is not None and left.fresh_until < moment
            ):
                left_beats = False
                reason = "stale_higher_authority_yields_to_fresh_lower"
            else:
                left_beats = True
                reason = "higher_authority"
        elif right.authority.rank < left.authority.rank:
            if _is_fresher(left, right, moment) and (
                right.fresh_until is not None and right.fresh_until < moment
            ):
                left_beats = True
                reason = "stale_higher_authority_yields_to_fresh_lower"
            else:
                left_beats = False
                reason = "higher_authority"
        elif left_fresher and not right_fresher:
            left_beats = True
            reason = "fresher_observation"
        elif right_fresher and not left_fresher:
            left_beats = False
            reason = "fresher_observation"
        elif left.observed_at >= right.observed_at:
            left_beats = True
            reason = "tie_break_observed_at"
        else:
            left_beats = False
            reason = "tie_break_observed_at"

    preferred = left if left_beats else right
    alternate = right if left_beats else left
    return ConflictResolution(preferred=preferred, alternate=alternate, reason=reason)


def resolve_capacity_snapshots(
    snapshots: Sequence[CapacitySnapshot],
    *,
    now: datetime | None = None,
) -> ResolvedCapacity:
    """Group by account, resolve same-account conflicts, retain both sides."""

    moment = _utc(now)
    by_account: dict[tuple[str, str, str], list[CapacitySnapshot]] = {}
    for snap in snapshots:
        key = (
            snap.identity.provider_id,
            snap.identity.account_id,
            snap.identity.workspace_id or "",
        )
        by_account.setdefault(key, []).append(snap)

    preferred: list[CapacitySnapshot] = []
    conflicts: list[ConflictResolution] = []
    shared: set[str] = set()

    for group in by_account.values():
        for snap in group:
            for pool in snap.pools:
                if pool.shared_pool_id:
                    shared.add(pool.shared_pool_id)
        if len(group) == 1:
            preferred.append(group[0])
            continue
        # Pairwise reduce: keep best, record each conflict.
        winner = group[0]
        for challenger in group[1:]:
            resolution = prefer_snapshot(winner, challenger, now=moment)
            conflicts.append(resolution)
            winner = resolution.preferred
        preferred.append(winner)

    # Also surface cross-source conflicts that share provider but differ by source
    # kind for the same account (already covered). Cross-account pairs that
    # observers may want to compare (direct 8% vs gateway 43%) are recorded when
    # callers pass them in the same batch with matching account_id.
    return ResolvedCapacity(
        snapshots=tuple(preferred),
        conflicts=tuple(conflicts),
        shared_pool_ids=frozenset(shared),
    )


def unique_shared_capacity(
    snapshots: Sequence[CapacitySnapshot],
) -> dict[str, CapacityPool]:
    """Map shared_pool_id → one representative pool (no double-counting)."""

    found: dict[str, CapacityPool] = {}
    for snap in snapshots:
        for pool in snap.pools:
            if not pool.shared_pool_id:
                continue
            # First observation wins; do not sum product slices.
            found.setdefault(pool.shared_pool_id, pool)
    return found


def stale_cannot_overwrite(
    incumbent: CapacitySnapshot,
    challenger: CapacitySnapshot,
    *,
    now: datetime | None = None,
) -> CapacitySnapshot:
    """Return the snapshot that should remain for a concrete pool/account."""

    moment = _utc(now)
    if not same_account(incumbent.identity, challenger.identity):
        raise CapacityEvidenceError("cannot overwrite across distinct accounts")
    if challenger.observed_at <= incumbent.observed_at:
        return incumbent
    if (
        incumbent.fresh_until is not None
        and incumbent.fresh_until >= moment
        and challenger.fresh_until is not None
        and challenger.fresh_until < moment
    ):
        return incumbent
    return challenger


__all__ = [
    "ResolvedCapacity",
    "pool_key",
    "prefer_snapshot",
    "resolve_capacity_snapshots",
    "same_account",
    "stale_cannot_overwrite",
    "unique_shared_capacity",
]
