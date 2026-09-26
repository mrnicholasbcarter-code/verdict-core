"""Project CapacitySnapshot into runtime certification (passport) / session economics (STAY/SWITCH) / execution-path authority consumer shapes.

No provider-specific branching — consumers stay generic.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from verdict.capacity_models import CapacityPool, CapacitySnapshot, _format_datetime
from verdict.capacity_resolve import unique_shared_capacity
from verdict.cost_ledger import QuotaEvidenceInput


def _cooldown_iso(pool: CapacityPool) -> str | None:
    if pool.cooldown_until is not None:
        return _format_datetime(pool.cooldown_until)
    return None


def project_quota_evidence(
    snapshot: CapacitySnapshot, *, pool_id: str | None = None
) -> list[QuotaEvidenceInput]:
    """Map pools onto session economics (STAY/SWITCH) ``QuotaEvidenceInput`` (unknown stays absent/None)."""

    results: list[QuotaEvidenceInput] = []
    for pool in snapshot.pools:
        if pool_id is not None and pool.pool_id != pool_id:
            continue
        # Missing remaining_pct stays None — never invent 0 or 100.
        item: QuotaEvidenceInput = {
            "pool_id": pool.shared_pool_id or pool.pool_id,
            "remaining_pct": pool.remaining_pct,
            "cooldown_until": _cooldown_iso(pool),
            "observed_at": _format_datetime(snapshot.observed_at),
            "fresh_until": (
                None if snapshot.fresh_until is None else _format_datetime(snapshot.fresh_until)
            ),
            "evidence_id": pool.evidence_id or snapshot.evidence_digest,
        }
        results.append(item)
    return results


def project_runtime_certification_quota(snapshot: CapacitySnapshot) -> Mapping[str, Any] | None:
    """runtime certification (passport) CertifiedComponent.quota mapping — None when nothing observed."""

    if not snapshot.pools and not snapshot.balances:
        if snapshot.errors:
            return {
                "status": "error",
                "errors": [error.to_dict() for error in snapshot.errors],
                "evidence_digest": snapshot.evidence_digest,
            }
        return None

    shared = unique_shared_capacity((snapshot,))
    pools_payload = []
    seen_shared: set[str] = set()
    for pool in snapshot.pools:
        if pool.shared_pool_id and pool.shared_pool_id in seen_shared:
            # Product slices sharing a pool are not double-counted.
            continue
        if pool.shared_pool_id:
            seen_shared.add(pool.shared_pool_id)
            representative = shared.get(pool.shared_pool_id, pool)
            pools_payload.append(representative.to_dict())
        else:
            pools_payload.append(pool.to_dict())

    return {
        "source_kind": snapshot.source_kind,
        "authority": snapshot.authority.value,
        "identity": {
            "provider_id": snapshot.identity.provider_id,
            "account_id": snapshot.identity.account_id,
            "adapter_id": snapshot.identity.adapter_id,
            "workspace_id": snapshot.identity.workspace_id,
            "gateway_id": snapshot.identity.gateway_id,
        },
        "pools": pools_payload,
        "balances": [balance.to_dict() for balance in snapshot.balances],
        "observed_at": _format_datetime(snapshot.observed_at),
        "fresh_until": (
            None if snapshot.fresh_until is None else _format_datetime(snapshot.fresh_until)
        ),
        "confidence": snapshot.confidence,
        "health": snapshot.health,
        "evidence_digest": snapshot.evidence_digest,
        "errors": [error.to_dict() for error in snapshot.errors],
    }


def project_execution_path_evidence(snapshots: Sequence[CapacitySnapshot]) -> Mapping[str, Any]:
    """Provider-neutral evidence bag for execution-path authority (no brand switches)."""

    quota_inputs: list[QuotaEvidenceInput] = []
    accounts: list[dict[str, Any]] = []
    for snap in snapshots:
        quota_inputs.extend(project_quota_evidence(snap))
        accounts.append(
            {
                "provider_id": snap.identity.provider_id,
                "account_id": snap.identity.account_id,
                "adapter_id": snap.identity.adapter_id,
                "source_kind": snap.source_kind,
                "authority": snap.authority.value,
                "pool_ids": [pool.pool_id for pool in snap.pools],
                "shared_pool_ids": sorted(
                    {pool.shared_pool_id for pool in snap.pools if pool.shared_pool_id}
                ),
                "evidence_digest": snap.evidence_digest,
                "observed_at": _format_datetime(snap.observed_at),
            }
        )
    # Scarcity signals without collapsing into fallback_tier.
    scarcest: QuotaEvidenceInput | None = None
    for item in quota_inputs:
        remaining = item.get("remaining_pct")
        if not isinstance(remaining, (int, float)):
            continue
        current = scarcest.get("remaining_pct") if scarcest is not None else None
        if (
            scarcest is None
            or not isinstance(current, (int, float))
            or float(remaining) < float(current)
        ):
            scarcest = item

    return {
        "schema": "capacity_execution_evidence.v1",
        "quota_evidence": list(quota_inputs),
        "accounts": accounts,
        "scarcest_pool": scarcest,
        "unknown_pools": [item for item in quota_inputs if item.get("remaining_pct") is None],
    }


def scarcest_quota_evidence(snapshots: Sequence[CapacitySnapshot]) -> QuotaEvidenceInput | None:
    """Single session economics (STAY/SWITCH) input: scarcest observed remaining_pct (unknown ignored)."""

    best: QuotaEvidenceInput | None = None
    for snap in snapshots:
        for item in project_quota_evidence(snap):
            remaining = item.get("remaining_pct")
            if not isinstance(remaining, (int, float)):
                continue
            current = best.get("remaining_pct") if best is not None else None
            if (
                best is None
                or not isinstance(current, (int, float))
                or float(remaining) < float(current)
            ):
                best = item
    return best


__all__ = [
    "project_execution_path_evidence",
    "project_quota_evidence",
    "project_runtime_certification_quota",
    "scarcest_quota_evidence",
]
