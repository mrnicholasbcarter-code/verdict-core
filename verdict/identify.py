"""Passive identification and consented agentic screening, never dispatch permission."""

from __future__ import annotations

import json
import time
from collections import Counter, defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from typing import Any

from verdict.admission import is_opaque
from verdict.credentials_store import resolve_gateway_binding, resolve_management_token
from verdict.discovery.sources._types import DiscoveryRow
from verdict.free_tier_admit import normalize_omniroute_origin
from verdict.identify_pools import build_supply, build_task_pools
from verdict.identify_rank import RatedModel, rank_models
from verdict.metadata.mapping import IdentityMap, load_identity_map
from verdict.metadata.records import ModelMetadataError
from verdict.metadata.store import MetadataSnapshot, load_store
from verdict.omniroute_evidence import OmniRouteEvidence, fetch_omniroute_evidence
from verdict.orchestration.credential_pools import canonical_route, is_non_chat_route, pool_of
from verdict.orchestration.eligibility import _capability_facts, _capability_tier
from verdict.orchestration.health_cache import (
    CATEGORY_AUTH,
    CATEGORY_PAYMENT,
    CATEGORY_PERMISSION,
    STATE_FRESH,
    STATE_NEGATIVE,
    USABLE_SECONDS,
    HealthCache,
    HealthCacheError,
    HealthCacheLockTimeoutError,
    HealthEntry,
    agentic_capability,
    default_cache_path,
)
from verdict.orchestration.provider_catalog import backend_pool
from verdict.orchestration.verified_refresh import _JobLock
from verdict.passive_health import refresh_health_from_traffic
from verdict.prove_at_rest import (
    DEFAULT_MAX_WALL_SECONDS,
    AdmittedRoute,
    ProbeExchange,
    Prober,
    _cooled_pools,
    live_agentic_transport,
)

_EXCLUDED_ROUTES_LIMIT = 100

_SOURCES = {
    "providers": "/api/providers",
    "resilience": "/api/resilience/connections",
    "limits": "/api/usage/provider-limits",
    "cooldowns": "/api/resilience/model-cooldowns",
    "health_matrix": "/api/providers/health-matrix?range=7d",
    "v1_models": "/v1/models",
    "candidates": "/v1/auto-combo/auto/candidates",
}


def _timestamp(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def identify_models(
    evidence: OmniRouteEvidence,
    *,
    metadata: MetadataSnapshot | None,
    identity_map: IdentityMap | None = None,
    all_excluded: bool = False,
    tool_proof: Mapping[str, HealthEntry] | None = None,
) -> dict[str, Any]:
    """Partition the observed catalog; availability still requires healthy mapped traffic."""
    available: list[str] = []
    excluded: list[dict[str, str]] = []
    for route, traffic in sorted(evidence.proven_by_traffic.items()):
        reason = None
        if traffic.provider not in evidence.usable_providers:
            reason = "provider_unavailable"
        elif route in evidence.cooled_models:
            reason = "model_cooling_down"
        if reason is None:
            available.append(route)
        else:
            excluded.append({"route_id": route, "reason": reason})
    for route in sorted(evidence.inventory_route_ids - evidence.proven_by_traffic.keys()):
        observed_traffic = evidence.traffic_by_route.get(route)
        if (
            observed_traffic is not None
            and observed_traffic.last_status is not None
            and observed_traffic.last_status >= 400
        ):
            reason = f"traffic_last_status_error:{observed_traffic.last_status}"
        elif observed_traffic is not None and observed_traffic.status not in {
            "healthy",
            "degraded",
        }:
            reason = f"traffic_last_status_error:{observed_traffic.status}"
        elif (
            observed_traffic is not None
            and observed_traffic.provider not in evidence.usable_providers
        ):
            reason = "provider_unavailable"
        elif route in evidence.cooled_models:
            reason = "model_cooling_down"
        else:
            reason = "no_traffic_evidence"
        excluded.append({"route_id": route, "reason": reason})
    observed_routes = evidence.proven_by_traffic.keys() | evidence.traffic_by_route.keys()
    excluded.sort(key=lambda row: (row["route_id"] not in observed_routes, row["route_id"]))
    excluded_counts = dict(sorted(Counter(row["reason"] for row in excluded).items()))
    rated = rank_models(
        available, metadata=metadata, identity_map=identity_map, now=evidence.fetched_at
    )
    routes = []
    for rating in rated:
        traffic = evidence.proven_by_traffic[rating.route_id]
        routes.append(
            {
                **asdict(rating),
                "rating_unknown_reason": "missing_or_stale_task_rating"
                if rating.basis == "unknown"
                else None,
                "last_request_at": _timestamp(traffic.last_request_at),
                "requests": traffic.requests,
                "successes": traffic.successes,
                "tool_proof": "unknown",
                "agentic_proof": "unknown",
            }
        )
    connections = [
        {
            "connection_id": c.connection_id,
            "provider": c.provider,
            "usable": c.usable,
            "reason": c.reason,
            "test_status": c.test_status,
            "last_tested": _timestamp(c.last_tested),
            "quota_state": c.quota_state,
            "quota_reason": c.quota_reason,
            "quota_observed_at": _timestamp(c.quota_observed_at),
        }
        for c in evidence.connections
    ]
    return {
        "mode": "passive",
        "model_calls": 0,
        "dispatch_authorized": False,
        "fetched_at": _timestamp(evidence.fetched_at),
        "task_family": "code_edit",
        "inventory_complete": False,
        "inventory_coverage": "unknown"
        if "v1_models" in evidence.source_errors
        else "catalog_only",
        "inventory_unknown_reason": (
            "catalog_source_unavailable"
            if "v1_models" in evidence.source_errors
            else "catalog_is_not_full_entitled_inventory"
        ),
        "inventory_route_count": len(evidence.inventory_route_ids),
        "traffic_scope": "mixed_or_unknown",
        "traffic_proof_strength": "historical_chat_only",
        "traffic_route_ids": sorted(evidence.proven_by_traffic),
        "traffic_route_alias_count": len(evidence.proven_by_traffic),
        "independent_proof_count": None,
        "tool_proof_count": None,
        "connections": connections,
        "usable_connection_count": sum(c.usable for c in evidence.connections),
        "excluded_connection_count": sum(not c.usable for c in evidence.connections),
        "source_errors": list(evidence.source_errors),
        # Only when they carry information, so the pinned default report is unchanged.
        **(
            {"source_error_kinds": dict(evidence.source_error_kinds)}
            if evidence.source_error_kinds
            else {}
        ),
        **(
            {"connections_source": evidence.connections_source}
            if evidence.connections_source != "management"
            else {}
        ),
        "sources": [
            {
                "name": name,
                "path": path,
                "status": "error" if name in evidence.source_errors else "ok",
                "fetched_at": _timestamp(evidence.fetched_at),
                "observed_at": None,
            }
            for name, path in _SOURCES.items()
        ],
        "available_routes": routes,
        "excluded_routes": excluded if all_excluded else excluded[:_EXCLUDED_ROUTES_LIMIT],
        "excluded_routes_total": len(excluded),
        "excluded_routes_by_reason": excluded_counts,
        "excluded_routes_limit": None if all_excluded else _EXCLUDED_ROUTES_LIMIT,
        "excluded_routes_order": "traffic_first_then_route_id",
        "excluded_routes_truncated": not all_excluded and len(excluded) > _EXCLUDED_ROUTES_LIMIT,
        "supply": build_supply(evidence, tool_proof, evidence.fetched_at),
        "task_pools": build_task_pools(
            evidence, metadata, identity_map, tool_proof, evidence.fetched_at
        ),
    }


def _roster_routes(
    roster: Sequence[DiscoveryRow], evidence: OmniRouteEvidence
) -> list[dict[str, Any]]:
    """Join source identities to the gateway's exact IDs; never match by leaf."""
    # These names identify the same upstream service, not a billing class.
    provider_names = {"cloudflare": "cloudflare-ai", "kilo": "kilocode", "ovhcloud": "ovhfree"}
    index: dict[tuple[str, str], set[str]] = defaultdict(set)
    for route in evidence.inventory_route_ids:
        if "/" not in route or is_opaque(route):
            continue
        prefix, _, model = route.partition("/")
        provider, upstream_model = evidence.route_identities.get(
            route, (str(evidence.inventory_rows.get(route, {}).get("owned_by") or prefix), model)
        )
        for exact in (model, upstream_model):
            index[(provider, exact)].add(route)
    # A keyless provider can serve catalog routes without a connection row.
    inventory_providers = {provider for provider, _ in index}
    return [
        {
            "provider": row.provider,
            "gateway_provider": provider_names.get(row.provider, row.provider),
            "provider_in_inventory": provider_names.get(row.provider, row.provider)
            in inventory_providers,
            "model_id": row.id,
            "source": row.source,
            "commit": row.commit,
            "fetched_at": row.fetched_at,
            "routes": sorted(
                index[provider_names.get(row.provider, row.provider), row.id.partition("/")[2]]
            ),
        }
        for row in roster
    ]


def _roster_report(
    matches: list[dict[str, Any]],
    rows: Sequence[dict[str, Any]],
    evidence: OmniRouteEvidence,
    *,
    tool_proof: Mapping[str, HealthEntry] | None = None,
    observed_at: datetime | None = None,
) -> dict[str, Any]:
    now = observed_at or evidence.fetched_at
    results = {row["route_id"]: row["result"] for row in rows}
    aliases = {row["route_id"]: row["alias_of"] for row in rows if row["result"] == "skipped_alias"}
    report = []
    connected = {conn.provider for conn in evidence.connections} | evidence.usable_providers
    for item in matches:
        configured = item["gateway_provider"] in connected or item["provider_in_inventory"]
        routes = item["routes"]
        statuses = {}
        for route in routes:
            candidate = evidence.live_candidates.get(route)
            if route in results:
                reason = results[route]
            elif "candidates" in evidence.source_errors:
                reason = "reachability_unverified"
            elif candidate is None or not candidate.reachable:
                reason = "not_reachable"
            elif candidate.provider not in evidence.usable_providers:
                reason = "no_usable_connection"
            elif route in evidence.cooled_models:
                reason = "model_cooling_down"
            else:
                reason = "not_screened"
            statuses[route] = reason
        # Resolve aliases only in the aggregate. An unprobed alias must never
        # be presented as having passed its own probe.
        outcomes = {
            results.get(aliases[route], status) if route in aliases else status
            for route, status in statuses.items()
        }
        result = next((status for status in ("pass", "skipped_fresh") if status in outcomes), None)
        if result is None:
            if not routes and "v1_models" in evidence.source_errors:
                result = "inventory_unverified"
            elif not routes and configured:
                result = "model_not_registered"
            elif not routes:
                result = "not_configured"
            else:
                result = next(iter(outcomes)) if len(outcomes) == 1 else "mixed_route_results"
        proofs = {}
        for route in routes:
            entry = (tool_proof or {}).get(route)
            proofs[route] = {
                "capacity_class": evidence.route_capacities.get(route, "unknown"),
                "capacity_evidence": evidence.route_capacity_evidence.get(route),
                "last_observation_at": _timestamp(entry.checked_at) if entry else None,
                "last_observation": entry.category if entry else None,
                "fresh_liveness": bool(
                    entry and entry.healthy and entry.chat_ok and entry.state_at(now) == STATE_FRESH
                ),
                "fresh_tool_proof": bool(
                    entry and entry.healthy and agentic_capability(entry, now)["qualified"]
                ),
            }
        report.append(
            {
                **item,
                "provider_configured": configured,
                "result": result,
                "route_results": statuses,
                "route_evidence": proofs,
            }
        )
    return {
        "roster": report,
        "roster_counts": dict(Counter(row["result"] for row in report)),
        "roster_coverage": {
            "parsed": len(report),
            "providers": len({row["provider"] for row in report}),
            "configured_provider_matches": sum(row["provider_configured"] for row in report),
            "exact_route_matches": sum(bool(row["routes"]) for row in report),
            "distinct_gateway_routes": len({route for row in report for route in row["routes"]}),
            "fresh_tool_proof_models": sum(
                any(proof["fresh_tool_proof"] for proof in row["route_evidence"].values())
                for row in report
            ),
        },
    }


def screen_models(
    evidence: OmniRouteEvidence,
    *,
    metadata: MetadataSnapshot | None,
    cache: HealthCache,
    transport: Callable[[str, dict[str, Any], float], ProbeExchange],
    allow_live_probe: bool,
    max_per_provider: int,
    max_routes: int,
    identity_map: IdentityMap | None = None,
    providers: Sequence[str] = (),
    capacities: Sequence[str] = (),
    all_excluded: bool = False,
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    monotonic: Callable[[], float] = time.monotonic,
    max_capability_tier: int | None = None,
    min_context_tokens: int | None = None,
    require_tools: bool = False,
    max_per_pool: int | None = None,
    roster: Sequence[DiscoveryRow] | None = None,
    assume_unmetered_connections: bool = False,
) -> dict[str, Any]:
    """Screen strongest task rating first; capacity class is a filter, not an order.

    ``max_capability_tier`` skips, without probing, every route whose tier is a
    known value above the ceiling.  Routes with an unknown tier pass only when
    their stripped model key appears in the ADR-051 fallback planner band per
    fresh ``aa_intelligence`` ratings; otherwise they receive
    ``skipped_unknown_tier``.
    ``min_context_tokens`` and ``require_tools`` skip, the same way, every route
    whose context window is unknown or below the minimum, or whose tools support
    is not known to be true. ``max_per_pool`` caps probes per backend pool
    (aliased prefixes share one count) and covers pools breadth-first. All four
    only remove routes from the probed set or change which admissible route is
    probed first; none adds a candidate or grants eligibility.
    """
    _screen_options(
        allow_live_probe,
        max_per_provider,
        max_routes,
        max_capability_tier,
        min_context_tokens,
        max_per_pool,
    )
    report = identify_models(
        evidence,
        metadata=metadata,
        identity_map=identity_map,
        all_excluded=all_excluded,
        tool_proof=cache.routes(),
    )
    candidates = [row["route_id"] for row in report["available_routes"]]
    rated = rank_models(candidates, metadata=metadata, identity_map=identity_map, now=clock())

    # Task rating strongest first, unknown ratings last; rescreen-request
    # hints only reorder routes. Capacity class never enters the sort key:
    # price/capacity is excluded from selection ordering per the governing
    # contract, and --capacity stays a membership filter only.
    cache.reload()
    requested = cache.rescreen_requests()
    rated.sort(key=lambda rating: rating.route_id not in requested)

    # All configured, reachable supply is considered: inventory ids that are
    # live candidates but carry no traffic proof yet screen with an unknown
    # rating, last.  Subscription frontier routes (not in the auto-combo pool)
    # are included when their provider has a usable connection.
    seen_ids = {row["route_id"] for row in report["available_routes"]}
    extra_ratings: list[RatedModel] = []
    for rid in sorted(evidence.inventory_route_ids):
        if rid in seen_ids:
            continue
        if is_opaque(rid) or is_non_chat_route(rid):
            continue
        live = evidence.live_candidates.get(rid)
        if live is not None:
            if not live.reachable:
                continue
            # Use the provider field from the live candidate — it comes from
            # the connection data, the same namespace as usable_providers.
            provider = live.provider
        else:
            # Route is in inventory but not in the auto-combo pool (e.g. a
            # subscription frontier route).  Include only when its provider
            # has a usable connection.  Use the route prefix — same namespace
            # as usable_providers / live_candidates.provider.
            provider = rid.split("/", 1)[0]
            if provider not in evidence.usable_providers:
                continue
        extra_ratings.append(
            RatedModel(
                route_id=rid,
                provider=provider,
                score=None,
                metric=None,
                source=None,
                as_of=None,
                basis="unknown",
            )
        )
    all_ratings = list(rated) + extra_ratings

    # Planner band for unknown-tier filtering (ADR-051): a no-tier route passes
    # --max-capability-tier only when its stripped route key appears in the
    # fallback band per fresh aa_intelligence ratings.  Empty when max_capability_tier
    # is None (no tier cap set) or when metadata is absent or stale.
    _tier_band_keys: frozenset[str] = frozenset()
    if max_capability_tier is not None:
        from verdict.planner_band import compute_planner_bands as _cpb
        from verdict.planner_band import load_planner_band_config as _lbc

        _tier_band_keys = _cpb(
            list(evidence.inventory_route_ids), metadata, identity_map, config=_lbc(), now=clock()
        ).fallback_model_keys

    matches = _roster_routes(roster, evidence) if roster is not None else None
    if matches is not None:
        matched = {route for item in matches for route in item["routes"]}
        all_ratings = [
            rating
            for rating in all_ratings
            if rating.route_id in matched
            and (live := evidence.live_candidates.get(rating.route_id)) is not None
            and live.reachable
            and live.provider in evidence.usable_providers
            and rating.route_id not in evidence.cooled_models
        ]
        # Rotate capped runs across missing and oldest evidence before repeating work.
        oldest = datetime.min.replace(tzinfo=timezone.utc)
        all_ratings.sort(
            key=lambda rating: (
                entry.checked_at if (entry := cache.entry(rating.route_id)) else oldest
            )
        )
    # Billing refusals still inside their negative window, by route and by
    # confirmed alias family. An expired refusal is screened again, so a model
    # whose credit or free tier came back is found on the next run.
    refused_routes: set[str] = set()
    refused_families: set[tuple[str, str]] = set()
    if matches is not None:
        scanned_at = clock()
        for rid, entry in cache.routes().items():
            if entry.state_at(scanned_at) == STATE_NEGATIVE and (
                entry.category == CATEGORY_PAYMENT
                or entry.http_status == 402
                or entry.capacity_evidence == "billing_402_metered"
            ):
                refused_routes.add(rid)
                if rid in evidence.route_identities:
                    refused_families.add(evidence.route_identities[rid])
    prober = Prober(
        cache=cache,
        routes_loader=lambda: (),
        transport=lambda *_: ProbeExchange(http_status=None, ok=False),
        agentic_transport=transport,
        clock=clock,
        monotonic=monotonic,
    )
    deadline = monotonic() + DEFAULT_MAX_WALL_SECONDS
    counts: Counter[str] = Counter()
    stopped_providers: set[str] = set()
    families: dict[tuple[str, str], str] = {}
    rows = []
    checked = passed = requests = 0

    # BOD-297: identify screening is a coordinator consumer. Acquire the
    # shared lease before sending; another producer's in-flight job wins and
    # identify sends nothing.
    lock = _JobLock(cache.path.parent / "verified-refresh.lock")
    if not lock.acquire():
        # The lease is held: report every candidate as skipped and send
        # nothing. Nothing was acquired, so there is nothing to release.
        stopped_reason = "lock_timeout"
        for rating in all_ratings:
            route_id = rating.route_id
            rows.append(
                {
                    **asdict(rating),
                    "provider": rating.provider,
                    "capacity": evidence.route_capacities.get(route_id, "unknown"),
                    "capacity_evidence": evidence.route_capacity_evidence.get(route_id),
                    "turns_sent": 0,
                    "rescreen_requested": route_id in requested,
                    "latency_ms": None,
                    "result": "skipped_locked",
                    "rating_unknown_reason": (
                        "missing_or_stale_task_rating" if rating.basis == "unknown" else None
                    ),
                    **({} if max_per_pool is None else {"backend_pool": backend_pool(route_id)}),
                }
            )
        return {
            **report,
            "mode": "screen",
            "routes": rows,
            "routes_checked": 0,
            "passed": 0,
            "model_calls": 0,
            "model_requests_sent": 0,
            "stopped_reason": stopped_reason,
            **(
                _roster_report(
                    matches, rows, evidence, tool_proof=cache.routes(), observed_at=clock()
                )
                if matches is not None
                else {}
            ),
        }
    pool_counts: Counter[str] = Counter()
    # Pools this screen saw refuse on account grounds (auth or payment, HTTP or
    # in-band), excluding route-scoped failures. A later route in the same pool
    # would fail the same way, so it takes no cap slot. Decided on this run's
    # own probe, never on a name.
    refused_pools: set[str] = set()

    def decide(
        route_id: str,
        provider: str,
        capacity: str,
        family: tuple[str, str],
        row: dict[str, Any],
        pool_round: int,
    ) -> bool:
        """Give ``row`` its result; False defers it to the next pool round.

        Only a route whose pool already had ``pool_round`` probes is deferred,
        so every pool gets its Nth probe before any pool gets its N+1th.
        Without ``max_per_pool`` nothing is ever deferred.
        """
        nonlocal checked, passed, requests
        now = clock()
        cache.reload()
        lookup = cache.lookup(route_id, now)
        if pool_round == 1:
            if family in families:
                row.update(result="skipped_alias", alias_of=families[family])
                return True
            families[family] = route_id
        pool = backend_pool(route_id)
        # A fresh alias proof avoids repeating the same underlying model.
        siblings = [r for r in candidates if evidence.route_identities.get(r) == family]
        if route_id in refused_routes or family in refused_families:
            row["result"] = "skipped_billing_refusal"
        elif lookup.state == STATE_NEGATIVE:
            row["result"] = "skipped_negative"
        elif route_id not in requested and any(
            agentic_capability(cache.entry(r), now)["qualified"] for r in [route_id, *siblings]
        ):
            row["result"] = "skipped_fresh"
        elif provider in stopped_providers:
            row["result"] = "skipped_provider_stopped"
        elif pool in refused_pools:
            row["result"] = "skipped_pool_refused"
        elif (
            pool_of(route_id) in _cooled_pools(cache, now)
            or cache.cooldown_for(f"provider:{provider}", now) is not None
        ):
            row["result"] = "skipped_cooldown"
        elif checked >= max_routes or counts[provider] >= max_per_provider:
            row["result"] = "skipped_cap"
        elif max_per_pool is not None and pool_counts[pool] >= max_per_pool:
            row["result"] = "skipped_pool_cap"
        elif max_per_pool is not None and pool_counts[pool] >= pool_round:
            return False
        else:
            row.update(
                prober.screen_route(
                    AdmittedRoute(
                        route_id,
                        provider,
                        capacity,
                        pool=pool_of(route_id),
                        capacity_evidence=evidence.route_capacity_evidence.get(route_id),
                    ),
                    deadline=deadline,
                )
            )
            if row["turns_sent"]:
                if route_id in requested:
                    cache.complete_rescreen(route_id)
                checked += 1
                counts[provider] += 1
                pool_counts[pool] += 1
            requests += row["turns_sent"]
            passed += int(row["result"] == "pass")
            if row["result"] == "rate_limited":
                stopped_providers.add(provider)
            if _account_refusal(row, cache.entry(route_id)):
                refused_pools.add(pool)
        return True

    deferred: list[tuple[str, str, str, tuple[str, str], dict[str, Any]]] = []
    try:
        for rating in all_ratings:
            route_id = rating.route_id
            if route_id in evidence.proven_by_traffic:
                provider = evidence.proven_by_traffic[route_id].provider
            else:
                provider = rating.provider
            capacity = evidence.route_capacities.get(route_id, "unknown")
            if matches is None and (
                (providers and provider not in providers)
                or (capacities and capacity not in capacities)
            ):
                continue
            fallback = canonical_route(route_id).split("/", 1)
            family = evidence.route_identities.get(route_id, (fallback[0], fallback[-1]))
            inventory_row = evidence.inventory_rows.get(route_id, {})
            tier = _capability_tier(inventory_row, route_id)
            context_window, supports_tools, _ = _capability_facts(inventory_row)
            row: dict[str, Any] = {
                **asdict(rating),
                "provider": provider,
                "capacity": capacity,
                "capacity_evidence": evidence.route_capacity_evidence.get(route_id),
                "capability_tier": tier,
                "supports_tools": supports_tools,
                "context_window": context_window,
                "turns_sent": 0,
                "rescreen_requested": route_id in requested,
                "latency_ms": None,
            }
            if max_per_pool is not None:
                row["backend_pool"] = backend_pool(route_id)
            rows.append(row)
            if matches is not None:
                if providers and provider not in providers:
                    row["result"] = "skipped_provider_filter"
                    continue
                if is_non_chat_route(route_id, inventory_row):
                    row["result"] = "skipped_requires_modality_probe"
                    continue
                if capacities and capacity not in capacities:
                    row["result"] = "skipped_capacity"
                    continue
                fact = evidence.route_capacity_evidence.get(route_id)
                if fact == "billing_402_metered":
                    row["result"] = "skipped_billing_refusal"
                    continue
                # Unknown capacity is screened like free (ADR-049 assume_free): the
                # screen is what produces the tool proof. An operator declaration can
                # override a list-price-only veto, never connection-level billing facts
                # or an explicit capacity filter.
                if capacity == "metered" and not (
                    assume_unmetered_connections and fact == "apikey_positive_pricing"
                ):
                    row["result"] = "skipped_spend_policy"
                    continue
            if max_capability_tier is not None:
                if tier is None:
                    # No-tier route: pass only when the model is in the
                    # planner band (ADR-051 fallback) per fresh ratings.
                    model_key = route_id.rsplit(":", 1)[0] if ":" in route_id else route_id
                    if model_key not in _tier_band_keys:
                        row["result"] = "skipped_unknown_tier"
                        continue
                elif tier > max_capability_tier:
                    row["result"] = "skipped_capability_tier"
                    continue
            if (
                min_context_tokens is not None
                and (context_window is None or context_window < min_context_tokens)
            ) or (require_tools and supports_tools is not True):
                # Same treatment as the tier skip: an unknown fact never meets
                # a floor, and the route takes no alias, cap or pool slot.
                row["result"] = "skipped_task_floor"
                continue
            if not decide(route_id, provider, capacity, family, row, 1):
                deferred.append((route_id, provider, capacity, family, row))
        pool_round = 1
        while deferred:
            # Ends by round ``max_per_pool``: a pool at its cap is not deferred.
            pool_round += 1
            deferred = [item for item in deferred if not decide(*item, pool_round)]
    finally:
        lock.release()
    stopped_reason = "complete"
    if any(row["result"] == "skipped_deadline" for row in rows):
        stopped_reason = "wall_cap"
    elif any(row["result"] == "skipped_cap" for row in rows):
        stopped_reason = "route_cap" if checked >= max_routes else "provider_cap"
    elif any(row["result"] == "skipped_pool_cap" for row in rows):
        stopped_reason = "pool_cap"
    elif stopped_providers:
        stopped_reason = "provider_rate_limited"
    return {
        **report,
        "mode": "screen",
        "routes": rows,
        "routes_checked": checked,
        "passed": passed,
        "model_calls": requests,
        "model_requests_sent": requests,
        "stopped_reason": stopped_reason,
        **(
            _roster_report(matches, rows, evidence, tool_proof=cache.routes(), observed_at=clock())
            if matches is not None
            else {}
        ),
    }


def _account_refusal(row: Mapping[str, Any], entry: HealthEntry | None) -> bool:
    """This probe was refused on account grounds, so its pool would refuse again.

    Payment (HTTP 402 or an in-band credit refusal) counts unless the cache
    scoped it to the route (a paid model on a key that still serves free
    community routes). Auth and permission refusals respect this scope the same
    way.
    """
    if not row.get("turns_sent") or entry is None or entry.healthy:
        return False
    if entry.category == CATEGORY_PAYMENT or entry.http_status == 402:
        return entry.failure_scope != "route"
    auth = row.get("result") == "auth_failed" or entry.category in {
        CATEGORY_AUTH,
        CATEGORY_PERMISSION,
    }
    return auth and entry.failure_scope != "route"


def _screen_options(
    allow_live_probe: bool,
    max_per_provider: int | None,
    max_routes: int | None,
    max_capability_tier: int | None = None,
    min_context_tokens: int | None = None,
    max_per_pool: int | None = None,
) -> None:
    if not allow_live_probe:
        raise ValueError("live_probe_consent_required: use --allow-live-probe")
    if max_per_provider is None or max_routes is None or min(max_per_provider, max_routes) < 1:
        raise ValueError(
            "screen_caps_required: positive --max-per-provider and --max-routes required"
        )
    if max_capability_tier is not None and not 0 <= max_capability_tier <= 3:
        raise ValueError("invalid_max_capability_tier: --max-capability-tier must be 0 to 3")
    if min_context_tokens is not None and min_context_tokens < 1:
        raise ValueError("invalid_min_context_tokens: --min-context-tokens must be positive")
    if max_per_pool is not None and max_per_pool < 1:
        raise ValueError("invalid_max_per_pool: --max-per-pool must be positive")


def identify_report(
    *,
    screen: bool = False,
    allow_live_probe: bool = False,
    max_per_provider: int | None = None,
    max_routes: int | None = None,
    providers: Sequence[str] = (),
    capacities: Sequence[str] = (),
    all_excluded: bool = False,
    refresh_health_cache: bool = False,
    max_capability_tier: int | None = None,
    min_context_tokens: int | None = None,
    require_tools: bool = False,
    max_per_pool: int | None = None,
    community_roster: bool = False,
    assume_unmetered_connections: bool = False,
) -> dict[str, Any]:
    """Read the seven passive OmniRoute sources and local metadata into one report.

    Only ``refresh_health_cache`` in passive mode writes, and only chat-only
    liveness and availability negatives from observed traffic.
    """
    if screen:
        _screen_options(
            allow_live_probe,
            max_per_provider,
            max_routes,
            max_capability_tier,
            min_context_tokens,
            max_per_pool,
        )
    else:
        for flag, given in (
            ("--max-capability-tier", max_capability_tier is not None),
            ("--min-context-tokens", min_context_tokens is not None),
            ("--require-tools", require_tools),
            ("--max-per-pool", max_per_pool is not None),
        ):
            if given:
                raise ValueError(f"{flag} requires --screen")
    if assume_unmetered_connections and not (community_roster and screen):
        raise ValueError("--assume-unmetered-connections requires --community-roster and --screen")
    roster: Sequence[DiscoveryRow] | None = None
    roster_error = None
    if community_roster:
        from verdict.discovery.sources.community import fetch_fcm_current

        try:
            roster = fetch_fcm_current()
        except (OSError, ValueError) as exc:
            roster = ()
            roster_error = type(exc).__name__
    base_url, key = resolve_gateway_binding()
    evidence = fetch_omniroute_evidence(
        normalize_omniroute_origin(base_url),
        api_key=key,
        management_token=resolve_management_token(),
    )
    metadata_error = None
    try:
        metadata = load_store()
    except (OSError, ValueError, ModelMetadataError):
        metadata = None
        metadata_error = "metadata_unavailable"
    mapping_error = None
    try:
        identity_map = load_identity_map()
    except (OSError, ValueError, ModelMetadataError):
        identity_map = None
        mapping_error = "identity_map_unavailable"
    tool_proof_error = None
    tool_proof: Mapping[str, HealthEntry] | None = None
    if screen:
        assert max_per_provider is not None and max_routes is not None
        report = screen_models(
            evidence,
            metadata=metadata,
            identity_map=identity_map,
            cache=HealthCache(default_cache_path()),
            transport=live_agentic_transport(base_url, api_key=key),
            allow_live_probe=allow_live_probe,
            max_per_provider=max_per_provider,
            max_routes=max_routes,
            providers=providers,
            capacities=capacities,
            all_excluded=all_excluded,
            max_capability_tier=max_capability_tier,
            min_context_tokens=min_context_tokens,
            require_tools=require_tools,
            max_per_pool=max_per_pool,
            roster=roster,
            assume_unmetered_connections=assume_unmetered_connections,
        )
    else:
        try:
            tool_proof = HealthCache(default_cache_path()).routes()
        except (HealthCacheError, OSError, ValueError):
            tool_proof = None
            tool_proof_error = "health_cache_unavailable"
        report = identify_models(
            evidence,
            metadata=metadata,
            identity_map=identity_map,
            all_excluded=all_excluded,
            tool_proof=tool_proof,
        )
        if refresh_health_cache:
            from verdict.omniroute_webhook import read_events

            now = datetime.now(timezone.utc)
            report["health_refresh"] = refresh_health_from_traffic(
                evidence,
                HealthCache(default_cache_path()),
                now=now,
                webhook_events=read_events(since=now - timedelta(seconds=USABLE_SECONDS)),
            )
    if roster is not None:
        report["roster_source_error"] = roster_error
        if not screen:
            report.update(
                _roster_report(
                    _roster_routes(roster, evidence), [], evidence, tool_proof=tool_proof
                )
            )
    report.update(
        metadata_error=metadata_error,
        identity_map_error=mapping_error,
        tool_proof_error=tool_proof_error,
    )
    # ADR-051 Decision 9: add rating_coverage and planner_band_families to the report
    # so population drift and band supply are visible from identify output.
    try:
        from verdict.planner_band import (
            compute_planner_bands,
            load_planner_band_config,
            planner_band_families,
            rating_coverage_summary,
        )

        _pb_config = load_planner_band_config()
        _pb_inv = list(evidence.inventory_route_ids)
        _pb_band = compute_planner_bands(
            _pb_inv,
            metadata if isinstance(metadata, MetadataSnapshot) else None,
            identity_map if isinstance(identity_map, IdentityMap) else None,
            config=_pb_config,
            now=datetime.now(timezone.utc),
        )
        report["rating_coverage"] = rating_coverage_summary(
            _pb_inv,
            _pb_band,
            metadata if isinstance(metadata, MetadataSnapshot) else None,
            identity_map if isinstance(identity_map, IdentityMap) else None,
            now=datetime.now(timezone.utc),
        )
        report["planner_band_families"] = planner_band_families(
            _pb_band,
            metadata if isinstance(metadata, MetadataSnapshot) else None,
            identity_map if isinstance(identity_map, IdentityMap) else None,
        )
    except Exception:
        report["rating_coverage"] = None
        report["planner_band_families"] = None
    return report


def cmd_identify(
    *,
    save_report: bool = False,
    output_json: bool = False,
    screen: bool = False,
    allow_live_probe: bool = False,
    max_per_provider: int | None = None,
    max_routes: int | None = None,
    providers: Sequence[str] = (),
    capacities: Sequence[str] = (),
    all_excluded: bool = False,
    refresh_health_cache: bool = False,
    max_capability_tier: int | None = None,
    min_context_tokens: int | None = None,
    require_tools: bool = False,
    max_per_pool: int | None = None,
    community_roster: bool = False,
    assume_unmetered_connections: bool = False,
) -> None:
    try:
        if assume_unmetered_connections and not (screen and community_roster):
            raise ValueError(
                "--assume-unmetered-connections requires --community-roster and --screen"
            )
        report = (
            identify_report(
                screen=screen,
                allow_live_probe=allow_live_probe,
                max_per_provider=max_per_provider,
                max_routes=max_routes,
                providers=providers,
                capacities=capacities,
                all_excluded=all_excluded,
                max_capability_tier=max_capability_tier,
                min_context_tokens=min_context_tokens,
                require_tools=require_tools,
                max_per_pool=max_per_pool,
                community_roster=community_roster,
                assume_unmetered_connections=assume_unmetered_connections,
            )
            if screen
            else identify_report(
                all_excluded=all_excluded,
                refresh_health_cache=refresh_health_cache,
                community_roster=True,
            )
            if community_roster
            else identify_report(all_excluded=all_excluded, refresh_health_cache=True)
            if refresh_health_cache
            else identify_report(all_excluded=True)
            if all_excluded
            else identify_report()
        )
    except HealthCacheLockTimeoutError as exc:
        raise SystemExit(f"health_cache_{exc.reason}: {exc}") from exc
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    if save_report:
        from verdict.identify_service import save_report as persist_report

        persist_report(report)
    if output_json:
        print(json.dumps(report, sort_keys=True, allow_nan=False))
    elif screen:
        print(
            f"Screened {report['routes_checked']} routes; passed {report['passed']}; "
            f"model requests: {report['model_requests_sent']}; stopped: {report['stopped_reason']}."
        )
    else:
        print(
            f"Passive identify: {report['usable_connection_count']} usable connections; "
            f"{len(report['available_routes'])} available mapped traffic routes."
        )
        print(
            f"Inventory coverage {report['inventory_coverage']}; "
            "traffic scope mixed_or_unknown; historical chat only."
        )
        print(
            f"Excluded {report['excluded_routes_total']} routes: "
            f"{json.dumps(report['excluded_routes_by_reason'], sort_keys=True)}."
        )
        print(f"Source errors: {', '.join(report['source_errors']) or 'none'}; model calls: 0.")
        pool, supply = report["task_pools"]["implementation"], report["supply"]
        print(
            f"Task pools: implementation eligible {pool['eligible_count']} "
            f"(free {pool['eligible_by_capacity']['free']}) fallback {pool['fallback_count']}; "
            f"supply complete: {str(supply['complete']).lower()} "
            f"({len(supply['unknowns'])} unknowns)."
        )
        refresh = report.get("health_refresh")
        if refresh is not None:
            print(
                f"Health cache refresh: {refresh['recorded']} liveness, "
                f"{refresh['recorded_failures']} negatives recorded from observed traffic; "
                "tool proof granted: 0."
            )
