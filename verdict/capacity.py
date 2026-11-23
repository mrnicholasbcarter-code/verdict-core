"""Shared account-evidence capacity classification and tier ordering."""

from __future__ import annotations

import os
from collections.abc import Mapping
from datetime import datetime
from typing import TYPE_CHECKING, Any

from verdict.connection_billing import BILLING_KEYLESS, BILLING_METERED, BILLING_SUBSCRIPTION
from verdict.connection_billing import lookup as billing_lookup
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


def free_supply_type(evidence: str) -> str:
    """Count one-time and recurring supply only when the gateway says which."""
    rule, _, detail = evidence.partition(":")
    free_type, _, _hard_stop = detail.partition(":")
    if rule == "gateway_free_tier":
        if free_type == "one-time-initial":
            return "one_time_initial"
        if free_type.startswith("recurring-") or free_type == "keyless":
            return "recurring"
    return "unknown"


def catalog_price_source(row: Mapping[str, Any]) -> str:
    """Normalize explicit price provenance; never infer it from the amount."""
    pricing = row.get("pricing")
    nested = pricing.get("source") if isinstance(pricing, Mapping) else None
    raw = row.get("price_source", row.get("pricingSource", row.get("priceSource", nested)))
    if raw is None:
        if (
            not isinstance(pricing, Mapping)
            or os.getenv("PRICING_SYNC_ENABLED", "").strip().lower() == "true"
        ):
            return "unknown"
        return "legacy_catalog"
    if isinstance(raw, str):
        source = raw.strip().lower()
        if source in {"provider", "user", "synced", "hardcoded", "legacy_catalog"}:
            return source
    return "unknown"


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
    0/0 set (BOD-418); connection free facts through ``connection_signals_free``;
    billing facts through ``connection_billing.lookup``.  A ``:free`` suffix, a
    provider prefix or a provider-name list decides nothing.  Without a signal
    the route is UNKNOWN.

    Rule order (design.md CP2):
      1. OpenRouter catalog membership (BOD-418)
      2. Connection free facts (``importFreeModelsOnly`` / ``import_free_only``)
      3. Connection billing facts from snapshot + operator overlay
      4. Declared price (rule 0.2) — API-key connections only (operator, 2026-11-23)
      5. Gateway free-tier membership
      6. UNKNOWN

    ADR-049 (accepted 2026-11-21): a route is PAID only from declared or
    observed facts; absence is not proof.  Billing ``unknown`` and a missing
    catalog entry are not facts and never produce METERED on their own.

    ``deciding_fact`` names the signal that determined the class.  Billing
    facts include the source: ``billing_catalog_subscription``,
    ``billing_overlay_subscription``, ``billing_catalog_metered``,
    ``billing_overlay_metered``, ``billing_catalog_keyless``, or
    ``billing_overlay_keyless``.
    """
    if conn is None:
        return CapacityClass.UNKNOWN, "", "no_connection"
    plan_label = sanitized_plan_label(conn.get("plan_label", ""))

    # BOD-418: the OpenRouter catalog decides by public 0/0 membership; the
    # id only selects which published catalog to consult.
    if route_id and is_openrouter_catalog_route(route_id):
        if openrouter_route_is_public_free(route_id, openrouter_free_set):
            return CapacityClass.FREE, plan_label, "openrouter_public_zero"
        if openrouter_free_set is not None:
            return CapacityClass.METERED, plan_label, "openrouter_not_in_public_free_set"
        return CapacityClass.UNKNOWN, plan_label, "openrouter_free_set_unavailable"

    # Rule 1: connection free facts (import_free_only / importFreeModelsOnly).
    is_free, signal = connection_signals_free(conn)
    if is_free:
        return CapacityClass.FREE, plan_label, signal

    # Rule 2: connection billing facts from snapshot + operator overlay.
    # Billing unknown and a missing entry are not facts; they fall through.
    provider = str(conn.get("provider", "")).strip().lower()
    auth_type = str(conn.get("authType", "")).strip().lower() or None
    if provider:
        fact = billing_lookup(provider, auth_type)
        if fact is not None:
            source_tag = fact.source  # "snapshot" or "overlay"
            if fact.billing == BILLING_SUBSCRIPTION:
                # Include overage in the deciding fact so callers (and the
                # health cache) can tell hard-stop from meters-to-paid without
                # a second catalog lookup.  Format: billing_{source}_subscription:{overage}
                deciding = f"billing_{source_tag}_subscription:{fact.overage}"
                return CapacityClass.SUBSCRIPTION, plan_label, deciding
            if fact.billing == BILLING_METERED:
                deciding = f"billing_{source_tag}_metered"
                return CapacityClass.METERED, plan_label, deciding
            if fact.billing == BILLING_KEYLESS:
                deciding = f"billing_{source_tag}_keyless"
                return CapacityClass.FREE, plan_label, deciding

    # Rule 3: declared price — applies only to API-key connections.
    # (operator, 2026-11-23): a list price is evidence only when the connection
    # is API-key typed.  An OAuth or account connection with no billing fact
    # falls through to UNKNOWN rather than being labelled METERED from a price.
    # Observed facts (charge header, route-scoped 402) still apply to every
    # connection type; those come from billing_capacity_override(), not here.
    price_source = catalog_price_source(row)
    connection_price = price_source in {"provider", "user", "legacy_catalog"}
    is_apikey = auth_type == "apikey"
    status = classify_free(route_id, _route_price(row) if connection_price else None, None)
    if connection_price and is_apikey and (status is FreeStatus.NOT_FREE or _any_positive_price(row)):
        price_fact = (
            "apikey_positive_pricing"
            if price_source == "legacy_catalog"
            else f"{price_source}_positive_pricing"
        )
        return CapacityClass.METERED, plan_label, price_fact
    # A discontinued summary entry is not a free fact. List prices from the
    # gateway must not override a current free-tier fact.
    if row.get("gateway_free_tier") is True and row.get("free_type") != "discontinued":
        free_type = row.get("free_type")
        if free_type not in (
            "one-time-initial",
            "recurring-daily",
            "recurring-uncapped",
            "recurring-monthly",
            "recurring-credit",
            "keyless",
        ):
            free_type = None
        hard_stop = row.get("hard_stop_guaranteed")
        if free_type is not None or hard_stop is not None:
            hard_stop_label = str(hard_stop).lower() if isinstance(hard_stop, bool) else "unknown"
            return (
                CapacityClass.FREE,
                plan_label,
                f"gateway_free_tier:{free_type or 'unknown'}:{hard_stop_label}",
            )
        return CapacityClass.FREE, plan_label, "gateway_free_tier"
    if status is FreeStatus.FREE:
        return CapacityClass.FREE, plan_label, "all_zero_pricing"
    # Fact-based rule: at least one numeric pricing value is declared and all
    # declared values are zero (brief, 2026-11-19: "a zero prompt or input
    # price" is evidence of free). A blank or bool-only pricing dict has no
    # declared facts and falls through to UNKNOWN.
    _pricing = row.get("pricing")
    if connection_price and isinstance(_pricing, Mapping):
        _declared = [
            float(v)
            for v in _pricing.values()
            if isinstance(v, (int, float)) and not isinstance(v, bool)
        ]
        if _declared and all(p == 0 for p in _declared):
            return CapacityClass.FREE, plan_label, "all_zero_pricing"
    return CapacityClass.UNKNOWN, plan_label, "no_free_evidence"


def capacity_of(
    conn: Mapping[str, Any] | None,
    row: Mapping[str, Any],
    *,
    route_id: str,
    openrouter_free_set: OpenRouterFreeSet | None,
) -> tuple[CapacityClass, str]:
    """Single source of truth for route capacity. Returns (class, deciding_fact).

    ``deciding_fact`` names the signal that determined the class.  Known values:
    ``"no_connection"``, ``"import_free_only"``, ``"importFreeModelsOnly"``,
    ``"billing_snapshot_subscription"``, ``"billing_overlay_subscription"``,
    ``"billing_snapshot_metered"``, ``"billing_overlay_metered"``,
    ``"billing_snapshot_keyless"``, ``"billing_overlay_keyless"``,
    ``"gateway_free_tier"``, ``"all_zero_pricing"``, ``"openrouter_public_zero"``,
    ``"apikey_positive_pricing"``, or ``"no_free_evidence"``.

    Use this function everywhere a capacity decision is needed.
    ``classify_route_capacity`` is the full 3-tuple form for callers that also
    need the account plan label.

    Invariant: a name, suffix or provider-id pattern never influences the result.
    """
    cls, _plan_label, fact = classify_route_capacity(
        conn, row, route_id=route_id, openrouter_free_set=openrouter_free_set
    )
    return cls, fact


def subscription_window_spent_metered(
    cache: HealthCache, route_id: str, now: datetime, deciding_fact: str | None = None
) -> bool:
    """Whether a SUBSCRIPTION route's quota window is spent with non-hard-stop overage.

    Returns True when the health cache shows a quota-category cooldown still in
    force AND the route's recorded capacity evidence contains a subscription fact
    with overage ``meters-to-paid`` or ``unknown``.  In that state the route is
    treated as METERED until the window resets (design.md CP2 Rules).

    A ``hard-stop`` route just becomes unavailable, as today — not METERED.

    Overage is read from ``entry.capacity_evidence`` which CP2 records as
    ``billing_{source}_subscription:{overage}`` (e.g.
    ``billing_snapshot_subscription:hard-stop``).  Routes without this evidence
    format are ignored — they are not known subscription connections.
    """
    from verdict.connection_billing import OVERAGE_HARD_STOP

    entry = cache.entry(route_id)
    if entry is None or entry.healthy or entry.until <= now:
        return False
    # Must be a quota-related cooldown (not a simple health failure).
    if entry.category not in {"quota_exhausted", "quota_cooldown"}:
        return False
    # Prefer current attribution over the evidence copied into an older cache entry.
    ce = deciding_fact if deciding_fact is not None else entry.capacity_evidence or ""
    rule, _, overage = ce.partition(":")
    return rule in {"billing_snapshot_subscription", "billing_overlay_subscription"} and (
        overage != OVERAGE_HARD_STOP and overage in {"meters-to-paid", "unknown"}
    )


def billing_capacity_override(cache: HealthCache, route_id: str, now: datetime) -> bool:
    """Whether a current route-scoped billing failure is specific paid evidence."""
    entry = cache.entry(route_id)
    return bool(
        entry is not None
        and not entry.healthy
        and entry.capacity_evidence == "billing_402_metered"
        and entry.until > now
    )
