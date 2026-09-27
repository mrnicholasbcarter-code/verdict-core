"""Subscription capacity evidence for canonical route admission.

This module is deliberately evidence-only.  It converts account-scoped
subscription budgets and normalized :class:`CapacitySnapshot` objects into
runtime observations consumed by ``verdict.admission.admit``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from verdict.capacity_models import CapacityFailureClass, CapacitySnapshot, ConnectionIdentity
from verdict.cost_ledger import _subscription_reserved, subscription_budgets
from verdict.headroom import UNKNOWN_HEADROOM, check_headroom
from verdict.models import ProviderConfig

SUBSCRIPTION_SOURCE = "subscription_headroom"


def _utc(now: datetime | None) -> datetime:
    value = now or datetime.now(timezone.utc)
    return (
        value.replace(tzinfo=timezone.utc)
        if value.tzinfo is None
        else value.astimezone(timezone.utc)
    )


def subscription_pool_key(identity: ConnectionIdentity, *, pool_id: str = "subscription") -> str:
    """Return an account/workspace-specific pool key; never provider-only."""
    workspace_value = getattr(identity, "workspace_id", None)
    workspace = (
        f"/{workspace_value}" if isinstance(workspace_value, str) and workspace_value else ""
    )
    return f"{identity.provider_id}/{identity.account_id}{workspace}/{pool_id}"


def legacy_subscription_pool_key(identity: ConnectionIdentity) -> str:
    """Key used by the original cost-ledger subscription budget API."""
    return f"{identity.provider_id}/{identity.account_id}/subscription"


def _fresh(snapshot: CapacitySnapshot, now: datetime) -> bool:
    return snapshot.fresh_until is None or snapshot.fresh_until >= now


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.astimezone(timezone.utc).isoformat()


def _pool_reason(pool: Any, now: datetime) -> tuple[str, str, str | None]:
    status = str(pool.status)
    category = {
        "exhausted": "subscription_exhaustion",
        "cooldown": "rate_limit" if pool.retry_after_seconds is not None else "cooldown",
        "constrained": "subscription_constrained",
        "available": "subscription_available",
        "unknown": "subscription_unknown",
    }.get(status, "subscription_unknown")
    until = pool.cooldown_until
    if until is None and pool.retry_after_seconds is not None:
        until = now.replace(microsecond=0) + __import__("datetime").timedelta(
            seconds=pool.retry_after_seconds
        )
    return status, category, _iso(until)


def subscription_observations(
    snapshots: Sequence[CapacitySnapshot], *, now: datetime
) -> tuple[list[dict[str, Any]], tuple[str, ...]]:
    """Project fresh subscription snapshots to normalized observation dicts.

    Payment, lockout, and auth errors are hard failures. Stale snapshots are
    not emitted, so they cannot overwrite fresh evidence. Every pool remains a
    distinct observation, including separate reset windows.
    """
    moment = _utc(now)
    observations: list[dict[str, Any]] = []
    sources: list[str] = []
    for snapshot in snapshots:
        source = (
            f"{SUBSCRIPTION_SOURCE}:{snapshot.identity.provider_id}:{snapshot.identity.account_id}"
        )
        sources.append(source)
        if not _fresh(snapshot, moment):
            continue
        provider = snapshot.identity.provider_id.lower()
        key = f"provider:{provider}"
        for error in snapshot.errors:
            failure = error.failure_class
            if failure in {
                CapacityFailureClass.BALANCE_EXHAUSTED,
                CapacityFailureClass.QUOTA_EXHAUSTED,
            }:
                state, category = "exhausted", "subscription_exhaustion"
            elif failure in {
                CapacityFailureClass.AUTH_EXPIRED,
                CapacityFailureClass.PERMISSION_DENIED,
            }:
                state, category = "unauthorized", "lockout"
            elif failure is CapacityFailureClass.RATE_LIMIT:
                state, category = "cooldown", "rate_limit"
            elif "payment" in error.message.lower() or "billing" in error.message.lower():
                state, category = "unauthorized", "payment"
            else:
                continue
            until = None
            if error.retry_after_seconds is not None:
                until = _iso(
                    moment + __import__("datetime").timedelta(seconds=error.retry_after_seconds)
                )
            observations.append(
                {
                    "key": key,
                    "state": state,
                    "category": category,
                    "source": source,
                    "observed_at": _iso(snapshot.observed_at),
                    "until": until,
                }
            )
        for pool in snapshot.pools:
            status, category, until = _pool_reason(pool, moment)
            observations.append(
                {
                    "key": key,
                    "state": status,
                    "category": category,
                    "source": source,
                    "observed_at": _iso(snapshot.observed_at),
                    "until": until,
                    "pool_id": pool.pool_id,
                    "reset_at": _iso(pool.reset_at),
                    "remaining_pct": pool.remaining_pct,
                    "retry_after_seconds": pool.retry_after_seconds,
                }
            )
        for balance in snapshot.balances:
            if balance.kind == "subscription_credits" and balance.remaining is not None:
                state = "exhausted" if balance.remaining <= 0 else "healthy"
                observations.append(
                    {
                        "key": key,
                        "state": state,
                        "category": "subscription_exhaustion"
                        if state == "exhausted"
                        else "subscription_available",
                        "source": source,
                        "observed_at": _iso(snapshot.observed_at),
                        "pool_id": balance.balance_id,
                    }
                )
    return observations, tuple(dict.fromkeys(sources))


def check_headroom_subscription(
    model_id: str,
    provider_name: str,
    config: ProviderConfig,
    identity: ConnectionIdentity | None = None,
    now: datetime | None = None,
) -> tuple[bool, float | None, str | None, dict[str, Any]]:
    """Check provider headroom and the concrete subscription pool.

    ``unknown`` is explicit and must be bounded-confirmed by the launch gate;
    no configured endpoint or absent budget is never treated as available.
    """
    moment = _utc(now)
    base_result = check_headroom(model_id, provider_name, config)
    metadata: dict[str, Any] = {
        "base_headroom": base_result,
        "now": moment.isoformat(),
        "source": SUBSCRIPTION_SOURCE,
    }
    available, headroom_pct = base_result if base_result is not None else (True, None)
    if base_result is not None and not available:
        return False, headroom_pct, "exhausted", metadata
    if identity is None:
        if base_result is None:
            return False, None, "unknown", metadata
        return bool(available), headroom_pct, "available", metadata
    if not isinstance(getattr(identity, "provider_id", None), str) or not isinstance(
        getattr(identity, "account_id", None), str
    ):
        return False, headroom_pct, "unknown", metadata
    key = legacy_subscription_pool_key(identity)
    budget = subscription_budgets.get(key)
    if budget is None:
        return False, headroom_pct, "unknown", metadata
    reserved = _subscription_reserved.get(key, Decimal("0"))
    remaining = budget - reserved
    metadata.update(
        {
            "pool_id": key,
            "subscription_budget": str(budget),
            "subscription_reserved": str(reserved),
            "observed_at": moment.isoformat(),
        }
    )
    if remaining <= 0:
        return False, 0.0, "exhausted", metadata
    subscription_pct = float(remaining / budget * 100) if budget else 0.0
    metadata["subscription_remaining_pct"] = subscription_pct
    final_pct = subscription_pct if headroom_pct is None else min(headroom_pct, subscription_pct)
    return True, final_pct, "available", metadata


def is_subscription_exhausted(pool_key: str) -> bool:
    budget = subscription_budgets.get(pool_key)
    return budget is not None and budget - _subscription_reserved.get(pool_key, Decimal("0")) <= 0


def subscription_headroom_pct(identity: ConnectionIdentity, pool_key: str) -> float | None:
    budget = subscription_budgets.get(pool_key)
    if budget is None or budget <= 0:
        return None
    return max(
        0.0, float((budget - _subscription_reserved.get(pool_key, Decimal("0"))) / budget * 100)
    )


def legacy_subscription_observations(
    inventory_rows: Sequence[Mapping[str, Any]],
    connections: Sequence[Mapping[str, Any]],
    *,
    now: datetime,
) -> tuple[list[dict[str, Any]], tuple[str, ...]]:
    """Project process-wide legacy budgets into canonical observations."""
    moment = _utc(now)
    out: list[dict[str, Any]] = []
    sources: list[str] = []
    seen: set[str] = set()
    for row in inventory_rows:
        owned = str(row.get("owned_by") or row.get("provider") or "")
        provider = owned.split("/", 1)[0]
        if not provider:
            continue
        conn = next(
            (c for c in connections if str(c.get("provider", "")).lower() == provider.lower()), {}
        )
        account = str(conn.get("account_id") or "")
        key = f"{provider}/{account}/subscription"
        if not account or key in seen or key not in subscription_budgets:
            continue
        seen.add(key)
        budget = subscription_budgets[key]
        reserved = _subscription_reserved.get(key, Decimal("0"))
        remaining = budget - reserved
        state = "exhausted" if remaining <= 0 else "healthy"
        category = "subscription_exhaustion" if state == "exhausted" else "subscription_available"
        source = f"{SUBSCRIPTION_SOURCE}:legacy:{key}"
        sources.append(source)
        out.append(
            {
                "key": f"provider:{provider.lower()}",
                "state": state,
                "category": category,
                "source": source,
                "observed_at": _iso(moment),
                "pool_id": key,
            }
        )
    return out, tuple(sources)


__all__ = [
    "SUBSCRIPTION_SOURCE",
    "UNKNOWN_HEADROOM",
    "check_headroom_subscription",
    "is_subscription_exhausted",
    "legacy_subscription_observations",
    "legacy_subscription_pool_key",
    "subscription_headroom_pct",
    "subscription_observations",
    "subscription_pool_key",
]
