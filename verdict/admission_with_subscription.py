"""Compatibility entry point for canonical subscription-aware admission."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from verdict.admission import AdmittedSet, RuntimeEvidence, admit
from verdict.capacity_models import CapacityPool, CapacitySnapshot, ConnectionIdentity, EvidenceAuthority
from verdict.cost_ledger import _subscription_reserved, subscription_budgets


def admit_with_subscription(
    inventory_rows: Sequence[Mapping[str, Any]] | None,
    connections: Sequence[Mapping[str, Any]] | None,
    runtime: RuntimeEvidence | None,
    *,
    now: datetime,
    required_capabilities: frozenset[str] = frozenset(),
    min_context_tokens: int = 0,
) -> AdmittedSet:
    """Forward legacy callers to the canonical ``admit`` implementation.

    ``admit`` projects legacy account-scoped subscription budgets itself.  The
    wrapper intentionally does not mint snapshots or inspect private state.
    """
    moment = now.astimezone(timezone.utc)
    snapshots: list[CapacitySnapshot] = []
    for conn in connections or ():
        provider = str(conn.get("provider") or "")
        account = str(conn.get("account_id") or "")
        key = f"{provider}/{account}/subscription"
        if not provider or not account or key not in subscription_budgets:
            continue
        remaining = subscription_budgets[key] - _subscription_reserved.get(key, Decimal("0"))
        snapshots.append(CapacitySnapshot(
            identity=ConnectionIdentity(provider_id=provider, account_id=account, adapter_id="legacy-ledger"),
            source_kind="local_history", authority=EvidenceAuthority.LOCAL_HISTORY,
            observed_at=moment, fresh_until=moment + timedelta(seconds=300),
            pools=(CapacityPool(key, status="exhausted" if remaining <= 0 else "available"),),
        ))
    return admit(
        inventory_rows,
        connections,
        runtime or RuntimeEvidence(),
        now=moment,
        require_runtime=False,
        required_capabilities=required_capabilities,
        min_context_tokens=min_context_tokens,
        subscription_snapshots=snapshots,
    )


__all__ = ["admit_with_subscription"]
