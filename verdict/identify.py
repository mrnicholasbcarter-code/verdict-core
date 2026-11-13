"""Passive identification and consented agentic screening, never dispatch permission."""

from __future__ import annotations

import json
import time
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from typing import Any

from verdict.credentials_store import resolve_gateway_binding, resolve_management_token
from verdict.free_tier_admit import normalize_omniroute_origin
from verdict.identify_pools import build_supply, build_task_pools
from verdict.identify_rank import RatedModel, rank_models, rating_key
from verdict.metadata.mapping import IdentityMap, load_identity_map
from verdict.metadata.records import ModelMetadataError
from verdict.metadata.store import MetadataSnapshot, load_store
from verdict.omniroute_evidence import OmniRouteEvidence, fetch_omniroute_evidence
from verdict.orchestration.contracts import CapacityClass
from verdict.orchestration.credential_pools import canonical_route, pool_of
from verdict.orchestration.eligibility import _CAPACITY_ORDER
from verdict.orchestration.health_cache import (
    STATE_NEGATIVE,
    USABLE_SECONDS,
    HealthCache,
    HealthCacheError,
    HealthCacheLockTimeoutError,
    HealthEntry,
    agentic_capability,
    default_cache_path,
)
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
) -> dict[str, Any]:
    """Screen cheapest tier first, then strongest, with explicit consent and route caps."""
    _screen_options(allow_live_probe, max_per_provider, max_routes)
    report = identify_models(
        evidence,
        metadata=metadata,
        identity_map=identity_map,
        all_excluded=all_excluded,
        tool_proof=cache.routes(),
    )
    candidates = [row["route_id"] for row in report["available_routes"]]
    rated = rank_models(candidates, metadata=metadata, identity_map=identity_map, now=clock())

    # Strongest task rating first, unknown ratings last: no economic
    # membership or rank filtering (BOD-297). When the caller selects
    # capacity classes explicitly, screen those classes in the canonical
    # consent order instead, strongest rating first within each class.
    cache.reload()
    requested = cache.rescreen_requests()
    if capacities:
        capacity_rank = {c: i for i, c in enumerate(_CAPACITY_ORDER)}
        rated.sort(
            key=lambda rating: (
                capacity_rank.get(
                    CapacityClass(evidence.route_capacities.get(rating.route_id, "unknown")),
                    len(_CAPACITY_ORDER),
                ),
                rating.route_id not in requested,
                *rating_key(rating),
                rating.route_id,
            )
        )
    else:
        # No capacity filter: strongest task rating first, unknown ratings last
        # (BOD-297). Equal scores prefer the lexicographically later route id so
        # a canonical route screens before its alias siblings.
        rated.sort(key=lambda rating: rating.route_id, reverse=True)
        rated.sort(key=lambda rating: (rating.score is None, -(rating.score or 0.0)))

    # All configured, reachable supply is considered: inventory ids that are
    # live candidates but carry no traffic proof yet screen with an unknown
    # rating, last.
    seen_ids = {row["route_id"] for row in report["available_routes"]}
    extra_ratings: list[RatedModel] = []
    for rid in sorted(evidence.inventory_route_ids):
        if rid in seen_ids:
            continue
        live = evidence.live_candidates.get(rid)
        if live is None or not live.reachable:
            continue
        provider = evidence.route_identities.get(rid, (rid.split("/", 1)[0],))[0]
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
        }
    try:
        for rating in all_ratings:
            route_id = rating.route_id
            if route_id in evidence.proven_by_traffic:
                provider = evidence.proven_by_traffic[route_id].provider
            else:
                provider = rating.provider
            capacity = evidence.route_capacities.get(route_id, "unknown")
            if (providers and provider not in providers) or (
                capacities and capacity not in capacities
            ):
                continue
            fallback = canonical_route(route_id).split("/", 1)
            family = evidence.route_identities.get(route_id, (fallback[0], fallback[-1]))
            row: dict[str, Any] = {
                **asdict(rating),
                "provider": provider,
                "capacity": capacity,
                "capacity_evidence": evidence.route_capacity_evidence.get(route_id),
                "turns_sent": 0,
                "rescreen_requested": route_id in requested,
                "latency_ms": None,
            }
            now = clock()
            cache.reload()
            lookup = cache.lookup(route_id, now)
            if family in families:
                row.update(result="skipped_alias", alias_of=families[family])
            else:
                families[family] = route_id
                # A fresh alias proof avoids repeating the same underlying model.
                siblings = [r for r in candidates if evidence.route_identities.get(r) == family]
                if lookup.state == STATE_NEGATIVE:
                    row["result"] = "skipped_negative"
                elif any(
                    agentic_capability(cache.entry(r), now)["qualified"]
                    for r in [route_id, *siblings]
                ):
                    row["result"] = "skipped_fresh"
                elif provider in stopped_providers:
                    row["result"] = "skipped_provider_stopped"
                elif (
                    pool_of(route_id) in _cooled_pools(cache, now)
                    or cache.cooldown_for(f"provider:{provider}", now) is not None
                ):
                    row["result"] = "skipped_cooldown"
                elif checked >= max_routes or counts[provider] >= max_per_provider:
                    row["result"] = "skipped_cap"
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
                    requests += row["turns_sent"]
                    passed += int(row["result"] == "pass")
                    if row["result"] == "rate_limited":
                        stopped_providers.add(provider)
            rows.append(row)
    finally:
        lock.release()
    stopped_reason = "complete"
    if any(row["result"] == "skipped_deadline" for row in rows):
        stopped_reason = "wall_cap"
    elif any(row["result"] == "skipped_cap" for row in rows):
        stopped_reason = "route_cap" if checked >= max_routes else "provider_cap"
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
    }


def _screen_options(
    allow_live_probe: bool, max_per_provider: int | None, max_routes: int | None
) -> None:
    if not allow_live_probe:
        raise ValueError("live_probe_consent_required: use --allow-live-probe")
    if max_per_provider is None or max_routes is None or min(max_per_provider, max_routes) < 1:
        raise ValueError(
            "screen_caps_required: positive --max-per-provider and --max-routes required"
        )


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
) -> dict[str, Any]:
    """Read the seven passive OmniRoute sources and local metadata into one report.

    Only ``refresh_health_cache`` in passive mode writes, and only chat-only
    liveness and availability negatives from observed traffic.
    """
    if screen:
        _screen_options(allow_live_probe, max_per_provider, max_routes)
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
        )
    else:
        tool_proof: Mapping[str, HealthEntry] | None
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
    report.update(
        metadata_error=metadata_error,
        identity_map_error=mapping_error,
        tool_proof_error=tool_proof_error,
    )
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
) -> None:
    try:
        report = (
            identify_report(
                screen=screen,
                allow_live_probe=allow_live_probe,
                max_per_provider=max_per_provider,
                max_routes=max_routes,
                providers=providers,
                capacities=capacities,
                all_excluded=all_excluded,
            )
            if screen
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
