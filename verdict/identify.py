"""Passive identification and consented agentic screening, never dispatch permission."""

from __future__ import annotations

import json
import time
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any

from verdict.credentials_store import resolve_gateway_binding
from verdict.free_tier_admit import normalize_omniroute_origin
from verdict.identify_rank import rank_models
from verdict.metadata.mapping import IdentityMap, load_identity_map
from verdict.metadata.records import ModelMetadataError
from verdict.metadata.store import MetadataSnapshot, load_store
from verdict.omniroute_evidence import OmniRouteEvidence, fetch_omniroute_evidence
from verdict.orchestration.contracts import CapacityClass
from verdict.orchestration.credential_pools import canonical_route, pool_of
from verdict.orchestration.eligibility import _CAPACITY_ORDER
from verdict.orchestration.health_cache import (
    STATE_NEGATIVE,
    HealthCache,
    HealthCacheLockTimeoutError,
    agentic_capability,
    default_cache_path,
)
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
}


def _timestamp(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def identify_models(
    evidence: OmniRouteEvidence,
    *,
    metadata: MetadataSnapshot | None,
    identity_map: IdentityMap | None = None,
    all_excluded: bool = False,
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
        evidence, metadata=metadata, identity_map=identity_map, all_excluded=all_excluded
    )
    candidates = [row["route_id"] for row in report["available_routes"]]
    rated = rank_models(candidates, metadata=metadata, identity_map=identity_map, now=clock())
    rated.sort(
        key=lambda rating: _CAPACITY_ORDER.get(
            CapacityClass(evidence.route_capacities.get(rating.route_id, "unknown")), 3
        )
    )
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
    for rating in rated:
        route_id = rating.route_id
        provider = evidence.proven_by_traffic[route_id].provider
        capacity = evidence.route_capacities.get(route_id, "unknown")
        if (providers and provider not in providers) or (capacities and capacity not in capacities):
            continue
        fallback = canonical_route(route_id).split("/", 1)
        family = evidence.route_identities.get(route_id, (fallback[0], fallback[-1]))
        row: dict[str, Any] = {
            **asdict(rating),
            "provider": provider,
            "capacity": capacity,
            "capacity_evidence": evidence.route_capacity_evidence.get(route_id),
            "turns_sent": 0,
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
                agentic_capability(cache.entry(r), now)["qualified"] for r in [route_id, *siblings]
            ):
                row["result"] = "skipped_fresh"
            elif provider in stopped_providers:
                row["result"] = "skipped_provider_stopped"
            elif pool_of(route_id) in _cooled_pools(cache, now):
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
                    checked += 1
                    counts[provider] += 1
                requests += row["turns_sent"]
                passed += int(row["result"] == "pass")
                if row["result"] == "rate_limited":
                    stopped_providers.add(provider)
        rows.append(row)
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
) -> dict[str, Any]:
    """Read the six passive OmniRoute sources and local metadata into one report."""
    if screen:
        _screen_options(allow_live_probe, max_per_provider, max_routes)
    base_url, key = resolve_gateway_binding()
    evidence = fetch_omniroute_evidence(normalize_omniroute_origin(base_url), api_key=key)
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
        report = identify_models(
            evidence, metadata=metadata, identity_map=identity_map, all_excluded=all_excluded
        )
    report.update(metadata_error=metadata_error, identity_map_error=mapping_error)
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
