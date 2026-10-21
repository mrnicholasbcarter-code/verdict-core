"""Shared account-evidence capacity classification and tier ordering."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import TYPE_CHECKING, Any

from verdict.openrouter_free_set import (
    OpenRouterFreeSet,
    is_openrouter_catalog_route,
    openrouter_route_is_public_free,
)
from verdict.orchestration.contracts import CapacityClass as CapacityClass

if TYPE_CHECKING:
    from verdict.orchestration.health_cache import HealthCache
from verdict.orchestration.provider_catalog import (
    connection_signals_free,
    has_free_suffix,
    sanitized_plan_label,
)

# D6: prefer the cheapest capacity tier that passed every admission gate.
CAPACITY_ORDER: Mapping[CapacityClass, int] = {
    CapacityClass.FREE: 0,
    CapacityClass.SUBSCRIPTION: 1,
    CapacityClass.METERED: 2,
    CapacityClass.UNKNOWN: 3,
}


def classify_route_capacity(
    conn: Mapping[str, Any] | None,
    row: Mapping[str, Any],
    *,
    route_id: str,
    openrouter_free_set: OpenRouterFreeSet | None,
) -> tuple[CapacityClass, str, str]:
    """D7 account evidence shared by live selection and at-rest classification."""
    if conn is None:
        return CapacityClass.UNKNOWN, "", "no_connection"
    plan_label = sanitized_plan_label(conn.get("plan_label", ""))
    auth_type = str(conn.get("authType", "")).strip().lower()

    # BOD-418: openrouter-family FREE requires public 0/0 membership.
    # Suffix / connection free signals alone must not mark paid OpenRouter
    # routes FREE (they previously cooled the whole openrouter-free pool).
    if route_id and is_openrouter_catalog_route(route_id):
        free_set = openrouter_free_set
        if openrouter_route_is_public_free(route_id, free_set):
            return CapacityClass.FREE, plan_label, "openrouter_public_zero"
        # Fall through: never treat openrouter-family as FREE from suffix
        # or import_free_only. Inventory pricing / oauth rules still apply.

    # D7: public non-free OpenRouter catalog membership and the Anthropic
    # API-key connection are specific paid evidence even without local pricing.
    if (route_id and is_openrouter_catalog_route(route_id)) or str(
        conn.get("provider", "")
    ).lower() == "openrouter":
        return CapacityClass.METERED, plan_label, "openrouter_not_in_public_free_set"
    if str(conn.get("provider", "")).lower() == "anthropic" and auth_type == "apikey":
        return CapacityClass.METERED, plan_label, "anthropic_apikey"

    # (a) :free suffix overrides oauth → subscription (fixes D5: 72
    # kilocode :free misclassed as SUBSCRIPTION). Openrouter-family is
    # handled above and must not take this shortcut.
    if route_id and has_free_suffix(route_id) and not is_openrouter_catalog_route(route_id):
        return CapacityClass.FREE, plan_label, "free_suffix"

    # (b) Connection-level free signals. Openrouter-family is excluded:
    # OmniRoute may set importFreeModelsOnly on a free-only key while the
    # catalog still lists paid models.
    is_free, signal = connection_signals_free(conn)
    if is_free and not (route_id and is_openrouter_catalog_route(route_id)):
        return CapacityClass.FREE, plan_label, signal

    # (c) oauth without a free signal → SUBSCRIPTION.
    if auth_type == "oauth":
        return CapacityClass.SUBSCRIPTION, plan_label, "oauth_subscription"

    # Pricing evidence for apikey connections.
    pricing = row.get("pricing")
    prices: list[float] = []
    if isinstance(pricing, Mapping):
        for value in pricing.values():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                prices.append(float(value))
    all_zero = bool(prices) and all(p == 0 for p in prices)
    positive = any(p > 0 for p in prices)

    # All-zero explicit inventory pricing is FREE even for apikey, but not
    # for openrouter-family when a public free set is available (public
    # catalog is authoritative there). Without a free set we still refuse
    # to assert FREE for openrouter-family from inventory alone.
    if all_zero and not (route_id and is_openrouter_catalog_route(route_id)):
        return CapacityClass.FREE, plan_label, "all_zero_pricing"

    # (e) apikey with positive pricing → METERED.
    if positive:
        return CapacityClass.METERED, plan_label, "apikey_positive_pricing"

    # D7: absence of specific paid/subscription evidence is assumed free,
    # not proof of capability. The same agentic gate still applies.
    return CapacityClass.FREE, plan_label, "assumed_free_no_paid_evidence"


def billing_capacity_override(cache: HealthCache, route_id: str, now: datetime) -> bool:
    """Whether a current route-scoped billing failure is specific paid evidence."""
    entry = cache.entry(route_id)
    return bool(
        entry is not None
        and not entry.healthy
        and entry.capacity_evidence == "billing_402_metered"
        and entry.until > now
    )
