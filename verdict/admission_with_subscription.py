"""Compatibility entry point for canonical subscription-aware admission."""
from __future__ import annotations
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from verdict.admission import AdmittedSet, RuntimeEvidence, admit
from verdict.capacity_models import CapacityPool, CapacitySnapshot, ConnectionIdentity, EvidenceAuthority
from verdict.cost_ledger import subscription_budgets, _subscription_reserved


def admit_with_subscription(
    inventory_rows: Sequence[Mapping[str, Any]] | None,
    connections: Sequence[Mapping[str, Any]] | None,
    runtime: RuntimeEvidence | None,
    *, now: datetime,
    required_capabilities: frozenset[str] = frozenset(),
    min_context_tokens: int = 0,
) -> AdmittedSet:
    """Build admission while projecting legacy subscription ledger budgets."""
    moment = now.astimezone(timezone.utc)
    snapshots: list[CapacitySnapshot] = []
    seen: set[tuple[str, str]] = set()
    for row in inventory_rows or ():
        owned = str(row.get("owned_by") or row.get("provider") or "")
        provider = owned.split("/", 1)[0]
        if not provider:
            continue
        conn = next((c for c in (connections or ()) if str(c.get("provider", "")).lower() == provider.lower()), {})
        account = str(conn.get("account_id") or "unknown")
        key = f"{provider}/{account}/subscription"
        if key in seen or key not in subscription_budgets:
            continue
        seen.add(key)
        budget = subscription_budgets[key]
        reserved = _subscription_reserved.get(key, Decimal("0"))
        remaining = max(Decimal("0"), budget - reserved)
        pct = float(remaining / budget * 100) if budget else 0.0
        status = "exhausted" if remaining <= 0 else "available"
        identity = ConnectionIdentity(provider_id=provider, account_id=account, adapter_id="legacy-subscription")
        snapshots.append(CapacitySnapshot(
            identity=identity, source_kind="local_history", authority=EvidenceAuthority.LOCAL_HISTORY,
            observed_at=moment, pools=(CapacityPool(pool_id=key, status=status, unit="credits", remaining_pct=pct),),
        ))
    return admit(
        inventory_rows, connections, runtime or RuntimeEvidence(), now=moment,
        require_runtime=False, required_capabilities=required_capabilities,
        min_context_tokens=min_context_tokens, subscription_snapshots=snapshots,
    )
