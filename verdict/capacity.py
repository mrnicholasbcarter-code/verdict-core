"""Shared account-evidence capacity classification and tier ordering."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import TYPE_CHECKING, Any

from verdict.omniroute_pipeline.free_rule import FreeStatus, classify_free
from verdict.openrouter_free_set import (
    OpenRouterFreeSet,
    is_openrouter_catalog_route,
    openrouter_route_is_public_free,
)
from verdict.orchestration.contracts import CapacityClass as CapacityClass

if TYPE_CHECKING:
    from verdict.orchestration.health_cache import HealthCache
from verdict.orchestration.provider_catalog import connection_signals_free, sanitized_plan_label

# D6: prefer the cheapest capacity tier that passed every admission gate.
CAPACITY_ORDER: Mapping[CapacityClass, int] = {
    CapacityClass.FREE: 0,
    CapacityClass.SUBSCRIPTION: 1,
    CapacityClass.METERED: 2,
    CapacityClass.UNKNOWN: 3,
}


def _route_price(row: Mapping[str, Any]) -> dict[str, object] | None:
    """The row's declared price in the shape rule 0.2 reads, or None when absent."""
    pricing = row.get("pricing")
    if not isinstance(pricing, Mapping):
        return None
    return {
        "input": pricing.get("input", pricing.get("prompt")),
        "output": pricing.get("output", pricing.get("completion")),
    }


def _any_positive_price(row: Mapping[str, Any]) -> bool:
    pricing = row.get("pricing")
    return isinstance(pricing, Mapping) and any(
        isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0 for v in pricing.values()
    )


def classify_route_capacity(
    conn: Mapping[str, Any] | None,
    row: Mapping[str, Any],
    *,
    route_id: str,
    openrouter_free_set: OpenRouterFreeSet | None,
) -> tuple[CapacityClass, str, str]:
    """D7 account evidence shared by live selection and at-rest classification.

    Capacity comes only from declared facts or observations, never from a
    name (operator, 2026-11-19 14:38Z: "no names should never determine if
    something is a free route or not"). Price facts go through rule 0.2
    (``free_rule.classify_free``); OpenRouter membership through the public
    0/0 set (BOD-418); connection facts through ``connection_signals_free``.
    A ``:free`` suffix, a provider prefix or a provider-name list decides
    nothing. Without a signal the route is UNKNOWN, and unknown is never free.
    """
    if conn is None:
        return CapacityClass.UNKNOWN, "", "no_connection"
    plan_label = sanitized_plan_label(conn.get("plan_label", ""))
    auth_type = str(conn.get("authType", "")).strip().lower()

    # BOD-418: the OpenRouter catalog decides by public 0/0 membership; the
    # id only selects which published catalog to consult.
    if route_id and is_openrouter_catalog_route(route_id):
        if openrouter_route_is_public_free(route_id, openrouter_free_set):
            return CapacityClass.FREE, plan_label, "openrouter_public_zero"
        if openrouter_free_set is not None:
            return CapacityClass.METERED, plan_label, "openrouter_not_in_public_free_set"
        return CapacityClass.UNKNOWN, plan_label, "openrouter_free_set_unavailable"

    # Declared connection facts (connections snapshot / /api/providers).
    is_free, signal = connection_signals_free(conn)
    if is_free:
        return CapacityClass.FREE, plan_label, signal

    if auth_type == "oauth":
        return CapacityClass.SUBSCRIPTION, plan_label, "oauth_subscription"

    # Rule 0.2 on the declared price: nonzero is paid, explicit 0/0 is free,
    # absent fails closed.
    status = classify_free(route_id, _route_price(row), None)
    if status is FreeStatus.NOT_FREE or _any_positive_price(row):
        return CapacityClass.METERED, plan_label, "apikey_positive_pricing"
    # The gateway's own free-tier membership (/api/free-tier/summary perModel),
    # attached to the row by the caller that read it. A nonzero price wins.
    if row.get("gateway_free_tier") is True:
        return CapacityClass.FREE, plan_label, "gateway_free_tier"
    if status is FreeStatus.FREE:
        return CapacityClass.FREE, plan_label, "all_zero_pricing"
    return CapacityClass.UNKNOWN, plan_label, "no_free_evidence"


def billing_capacity_override(cache: HealthCache, route_id: str, now: datetime) -> bool:
    """Whether a current route-scoped billing failure is specific paid evidence."""
    entry = cache.entry(route_id)
    return bool(
        entry is not None
        and not entry.healthy
        and entry.capacity_evidence == "billing_402_metered"
        and entry.until > now
    )
