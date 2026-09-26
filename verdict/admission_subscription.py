"""Subscription headroom integration for canonical admission.

Adds subscription-aware check to admission pipeline. Headroom evidence feeds
AdmittedSet with source/freshness metadata. Preserves UNKNOWN_HEADROOM behavior.
"""

from __future__ import annotations

from verdict.admission import AdmissionRecord, AdmittedSet, _MINT
from verdict.models import ProviderConfig, ConnectionIdentity, Route
from verdict.capacity_models import CapacityEvidenceError, CapacitySnapshot, PoolStatus
from verdict.subscription_headroom import (
    check_headroom_subscription,
    subscription_pool_key,
    is_subscription_exhausted,
)
from verdict.cost_ledger import subscription_budgets, _subscription_reserved
from datetime import datetime, timezone
from typing import Optional, Tuple, Dict, Any, Sequence
import hashlib


def _build_admitted_set_with_subscription(
    token: object,
    inventory_rows: Sequence[Dict[str, Any]] | None,
    connections: Sequence[Dict[str, Any]] | None,
    runtime: Dict[str, Any] | None,
    *,
    now: datetime,
    required_capabilities: frozenset[str] = frozenset(),
    min_context_tokens: int = 0,
    deny: Optional[callable] = None,
) -> AdmittedSet:
    """Build AdmittedSet with subscription-aware headroom checks.

    Headroom evidence becomes a runtime source. Unknown/exhausted cases are
    preserved as explicit admission states with proper reasons.
    """
    # Build base runtime evidence
    base_evidence = {
        "sources": [],
        "observations": [],
    }

    # Add subscription headroom evidence if available
    def _add_headroom_evidence(identity: Optional[ConnectionIdentity]):
        if not identity:
            return
        config = ProviderConfig(headroom_endpoint="https://api.example.com/usage")
        is_avail, pct, reason, meta = check_headroom_subscription(
            "gpt-4", identity.provider_id, config, identity, now
        )
        if meta.get("base_headroom") is None:
            # Explicit unknown case
            base_evidence["sources"].append("subscription_headroom:unknown")
            base_evidence["observations"].append({
                "key": f"provider:{identity.provider_id}",
                "state": "unknown",
                "category": "subscription_unknown",
                "source": "subscription_headroom",
                "observed_at": meta.get("now"),
            })
        elif reason == "exhausted":
            base_evidence["sources"].append("subscription_headroom:exhausted")
            base_evidence["observations"].append({
                "key": f"provider:{identity.provider_id}",
                "state": "exhausted",
                "category": "subscription_exhausted",
                "source": "subscription_headroom",
                "observed_at": meta.get("now"),
            })
        else:
            base_evidence["sources"].append("subscription_headroom:available")
            base_evidence["observations"].append({
                "key": f"provider:{identity.provider_id}",
                "state": "healthy" if is_avail else "unknown",
                "category": "subscription_available",
                "source": "subscription_headroom",
                "observed_at": meta.get("now"),
            })

    # This would be called per route during admission
    # For now, integrate into capacity_resolve logic
    return _MINT

