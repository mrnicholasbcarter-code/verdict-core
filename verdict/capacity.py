"""Shared account-evidence capacity classification and tier ordering."""

from __future__ import annotations

import os
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
    backend_pool,
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

DEFAULT_CREDIT_PROVIDERS: frozenset[str] = frozenset({"pollinations", "pol", "g4f-pollinations"})


def get_credit_providers() -> frozenset[str]:
    raw = os.getenv("VERDICT_CREDIT_PROVIDERS")
    if raw is not None:
        return frozenset(p.strip().lower() for p in raw.split(",") if p.strip())
    return DEFAULT_CREDIT_PROVIDERS


def is_credit_provider_route(route_id: str, conn: Mapping[str, Any] | None) -> bool:
    providers = get_credit_providers()
    if not providers:
        return False
    provider = str(conn.get("provider", "")).strip().lower() if conn else ""
    if provider in providers:
        return True
    lower_id = route_id.lower()
    return any(lower_id.startswith(f"{p}/") for p in providers)


def _prices(row: Mapping[str, Any]) -> list[float]:
    pricing = row.get("pricing")
    if not isinstance(pricing, Mapping):
        return []
    return [
        float(value)
        for value in pricing.values()
        if isinstance(value, (int, float)) and not isinstance(value, bool)
    ]


def _all_zero_pricing(row: Mapping[str, Any]) -> bool:
    prices = _prices(row)
    return bool(prices) and all(p == 0 for p in prices)


def _positive_pricing(row: Mapping[str, Any]) -> bool:
    return any(p > 0 for p in _prices(row))


def free_suffix_alias_is_public_free(route_id: str, free_set: OpenRouterFreeSet | None) -> bool:
    """True when a ``:free`` route on an OpenRouter-pool prefix is in the public 0/0 set.

    ``backend_pool`` already treats these prefixes as one upstream pool with
    ``openrouter/``. No free set, or an id outside it, is no evidence.
    """
    prefix, _, bare = route_id.partition("/")
    if free_set is None or not bare or backend_pool(route_id) != "openrouter-free":
        return False
    if prefix.lower() == "openrouter":
        return False  # openrouter/ itself is decided by openrouter_route_is_public_free
    return openrouter_route_is_public_free(f"openrouter/{bare}", free_set)


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

    # (a) A ``:free`` suffix (or "free" in the name) is a hint, never evidence
    # (operator rule, 2026-11-18): a provider can name a credit-billed route
    # ``:free``. One corroboration is checked here: kc/kilocode ``:free`` routes
    # are the OpenRouter ``:free`` routes under another prefix (same upstream
    # pool), so membership in OpenRouter's public 0/0 set is zero-price evidence
    # for them. It overrides oauth -> subscription for those routes.
    suffix_hint = bool(route_id) and has_free_suffix(route_id)
    if suffix_hint and free_suffix_alias_is_public_free(route_id, openrouter_free_set):
        return CapacityClass.FREE, plan_label, "openrouter_public_zero_alias"

    # (b) Connection-level free signals. Openrouter-family is excluded:
    # OmniRoute may set importFreeModelsOnly on a free-only key while the
    # catalog still lists paid models.
    is_free, signal = connection_signals_free(conn)
    if is_free and not (route_id and is_openrouter_catalog_route(route_id)):
        return CapacityClass.FREE, plan_label, signal

    # A credit provider bills against a prepaid balance. Its ``:free`` route with
    # no free evidence (connection signal above, zero inventory pricing below)
    # is UNKNOWN: the name alone must not make it FREE or assumed free.
    if (
        suffix_hint
        and is_credit_provider_route(route_id, conn)
        and not _all_zero_pricing(row)
        and not _positive_pricing(row)
    ):
        return CapacityClass.UNKNOWN, plan_label, "free_suffix_unverified"

    # (c) oauth without a free signal → SUBSCRIPTION.
    if auth_type == "oauth":
        return CapacityClass.SUBSCRIPTION, plan_label, "oauth_subscription"

    # Pricing evidence for apikey connections.
    all_zero = _all_zero_pricing(row)
    positive = _positive_pricing(row)

    # All-zero explicit inventory pricing is FREE even for apikey, but not
    # for openrouter-family when a public free set is available (public
    # catalog is authoritative there). Without a free set we still refuse
    # to assert FREE for openrouter-family from inventory alone.
    if all_zero and not (route_id and is_openrouter_catalog_route(route_id)):
        return CapacityClass.FREE, plan_label, "all_zero_pricing"

    # (e) apikey with positive pricing → METERED.
    if positive:
        return CapacityClass.METERED, plan_label, "apikey_positive_pricing"

    # Credit providers without free evidence or pricing stay UNKNOWN.
    if is_credit_provider_route(route_id, conn):
        return CapacityClass.UNKNOWN, plan_label, "credit_provider_unknown_pricing"

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
