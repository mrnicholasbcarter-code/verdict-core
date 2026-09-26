"""Subscription-aware headroom checks.

Extends existing headroom.py with subscription pool awareness for canonical
admission decisions. Preserves UNKNOWN_HEADROOM sentinel for explicit unknown cases.
"""

from __future__ import annotations

from verdict.headroom import check_headroom, UNKNOWN_HEADROOM
from verdict.models import ProviderConfig, ConnectionIdentity
from verdict.cost_ledger import subscription_budgets, _subscription_reserved
from decimal import Decimal
from datetime import datetime, timezone
from typing import Optional, Tuple, Dict, Any


def check_headroom_subscription(
    model_id: str,
    provider_name: str,
    config: ProviderConfig,
    identity: Optional[ConnectionIdentity] = None,
    now: Optional[datetime] = None,
) -> Tuple[bool, Optional[float], Optional[str], Dict[str, Any]]:
    """Check headroom with subscription context.

    Returns tuple of (is_available, headroom_pct, reason, metadata).
    Reason values:
    - "available": capacity confirmed
    - "exhausted": subscription pool empty or provider unavailable
    - "unknown": explicit unknown requiring bounded confirmation
    - "stale": evidence expired (TTL violation)
    """
    now = now or datetime.now(timezone.utc)
    base_result = check_headroom(model_id, provider_name, config)
    metadata: Dict[str, Any] = {
        "base_headroom": base_result,
        "identity": identity.dict if hasattr(identity, "dict") else None,
        "now": now.isoformat() if now else None,
    }

    if base_result is None:
        return (False, None, "unknown", metadata)

    is_available, headroom_pct = base_result
    if not is_available:
        return (False, headroom_pct, "exhausted", metadata)

    # Subscription pool lookup
    if identity:
        pool_key = f"{identity.provider_id}/{identity.account_id}/subscription"
        budget = subscription_budgets.get(pool_key)
        if budget is not None:
            reserved = _subscription_reserved.get(pool_key, Decimal('0'))
            remaining = budget - reserved
            if remaining <= 0:
                metadata["subscription_budget"] = str(budget)
                metadata["subscription_reserved"] = str(reserved)
                return (False, headroom_pct, "exhausted", metadata)
            # Calculate effective headroom from subscription budget
            effective_pct = float((remaining / budget) * 100)
            metadata["subscription_budget"] = str(budget)
            metadata["subscription_reserved"] = str(reserved)
            metadata["subscription_remaining_pct"] = effective_pct
            # Apply subscription constraint to headroom
            final_pct = min(headroom_pct, effective_pct) if headroom_pct is not None else effective_pct
            return (True, final_pct, "available", metadata)

    return (is_available, headroom_pct, None, metadata)


def subscription_pool_key(identity: ConnectionIdentity) -> str:
    """Generate consistent subscription pool key."""
    return f"{identity.provider_id}/{identity.account_id}/subscription"


def is_subscription_exhausted(pool_key: str) -> bool:
    """Check if subscription pool is exhausted.
    """
    try:
        budget = subscription_budgets.get(pool_key)
        if budget is None:
            return False
        reserved = _subscription_reserved.get(pool_key, Decimal('0'))
        return budget - reserved <= 0
    except Exception:
        return False


def subscription_headroom_pct(
    identity: ConnectionIdentity,
    pool_key: str,
) -> Optional[float]:
    """Calculate remaining subscription headroom percentage."""
    try:
        budget = subscription_budgets.get(pool_key)
        if budget is None:
            return None
        reserved = _subscription_reserved.get(pool_key, Decimal('0'))
        if budget - reserved <= 0:
            return 0.0
        return float((budget - reserved) / budget) * 100
    except Exception:
        return None

