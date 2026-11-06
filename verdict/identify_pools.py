"""Configured supply (AC1) and per-task ordered route pools (AC2) from passive evidence.

Layers stay apart: the catalog (``/v1/models``), the live candidate pool
(``/v1/auto-combo/auto/candidates``), tool proof (agentic proof in the health
cache) and task performance (not measured here). Pools are read-only advice:
fallback rows are probation that still needs bounded qualification, and no
pool authorizes dispatch.

Admission runs before ordering over every catalog route and live candidate.
Hard checks, first failure wins: not an opaque ``auto/*``/``combo/*`` route, in the catalog, live (a reachable candidate;
any candidate-source error makes liveness unknown), a provider with a usable
connection, not cooling down, and latest traffic not 401/403/404. Fresh tool
proof on the route or on a route reaching the same ``backend_model`` makes a
passing route eligible; without it the route is fallback. Everything else is
blocked and only counted by reason.

Supply groups routes by provider: the live candidate's provider, else the
catalog's ``owned_by`` identity, else ``pool_of(route)``. Connection counts
use the ``/api/providers`` provider name.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from verdict.admission import is_opaque
from verdict.capacity import CAPACITY_ORDER, CapacityClass
from verdict.identify_rank import rate_model, rating_key, rating_metric
from verdict.metadata.mapping import IdentityMap
from verdict.metadata.store import MetadataSnapshot
from verdict.omniroute_evidence import OmniRouteEvidence
from verdict.orchestration.credential_pools import backend_model, pool_of
from verdict.orchestration.health_cache import HealthEntry, agentic_capability

TASK_FAMILIES: Mapping[str, str] = {
    "research": "research",
    "specification": "specification",
    "implementation": "code_edit",
    "review": "code_review",
    "integration": "code_edit",
}
_AUTH_OR_ENTITLEMENT = frozenset({401, 403, 404})
_NOAUTH = "noauth"


def _capacity(evidence: OmniRouteEvidence, route: str) -> CapacityClass:
    return CapacityClass(evidence.route_capacities.get(route, "unknown"))


def _by_capacity(classes: list[CapacityClass]) -> dict[str, int]:
    counts = Counter(classes)
    return {capacity.value: counts[capacity] for capacity in CAPACITY_ORDER}


def _proof_routes(
    routes: list[str], cache_routes: Mapping[str, HealthEntry] | None, now: datetime
) -> dict[str, str]:
    """Route -> the route whose fresh agentic proof covers it (itself first)."""
    fresh = sorted(
        route
        for route, entry in (cache_routes or {}).items()
        if agentic_capability(entry, now)["qualified"]
    )
    by_model: dict[tuple[str, str], str] = {}
    for route in fresh:
        by_model.setdefault(backend_model(route), route)
    covered = {}
    for route in routes:
        if route in fresh:
            covered[route] = route
        elif backend_model(route) in by_model:
            covered[route] = by_model[backend_model(route)]
    return covered


def _blocker(evidence: OmniRouteEvidence, route: str, usable_providers: set[str]) -> str | None:
    if is_opaque(route):
        return "opaque_route"
    if route not in evidence.inventory_route_ids:
        return "not_in_catalog"
    if "candidates" in evidence.source_errors:
        return "liveness_unknown"
    candidate = evidence.live_candidates.get(route)
    if candidate is None:
        return "not_live:not_a_candidate"
    if not candidate.reachable:
        return f"not_live:{','.join(candidate.blocked_reasons) or 'unknown'}"
    if candidate.provider not in usable_providers:
        return "provider_unavailable"
    if route in evidence.cooled_models:
        return "model_cooling_down"
    traffic = evidence.traffic_by_route.get(route)
    if traffic is not None and traffic.last_status in _AUTH_OR_ENTITLEMENT:
        return f"traffic_auth_or_entitlement_error:{traffic.last_status}"
    return None


def _row(
    evidence: OmniRouteEvidence,
    route: str,
    proof_route: str | None,
    *,
    metadata: MetadataSnapshot | None,
    identity_map: IdentityMap | None,
    task_family: str,
    now: datetime,
) -> tuple[tuple[Any, ...], dict[str, Any]]:
    rated = rate_model(
        route, metadata=metadata, task_family=task_family, identity_map=identity_map, now=now
    )
    capacity = _capacity(evidence, route)
    traffic = evidence.traffic_by_route.get(route)
    candidate = evidence.live_candidates[route]
    row = {
        "route_id": route,
        "provider": candidate.provider,
        "capacity": capacity.value,
        "capacity_evidence": evidence.route_capacity_evidence.get(route),
        "connection_ids": list(candidate.connection_ids),
        "basis": rated.basis,
        "score": rated.score,
        "metric": rated.metric,
        "source": rated.source,
        "as_of": rated.as_of,
        "tool_proof": "missing" if proof_route is None else "fresh",
        "tool_proof_route": proof_route,
        "traffic": None
        if traffic is None
        else {
            "requests": traffic.requests,
            "successes": traffic.successes,
            "last_status": traffic.last_status,
            "last_request_at": traffic.last_request_at.isoformat()
            if traffic.last_request_at
            else None,
        },
        "aliases": [],
    }
    return (CAPACITY_ORDER[capacity], *rating_key(rated), route), row


def _ordered(rows: list[tuple[tuple[Any, ...], dict[str, Any]]]) -> list[dict[str, Any]]:
    kept: dict[tuple[str, str], dict[str, Any]] = {}
    for _, row in sorted(rows, key=lambda item: item[0]):
        first = kept.setdefault(backend_model(row["route_id"]), row)
        if first is not row:
            first["aliases"].append(row["route_id"])
    return list(kept.values())


def build_task_pools(
    evidence: OmniRouteEvidence,
    metadata: MetadataSnapshot | None,
    identity_map: IdentityMap | None,
    cache_routes: Mapping[str, HealthEntry] | None,
    now: datetime,
) -> dict[str, Any]:
    """Eligible and fallback pools per task family, cheapest capacity then strongest."""
    usable = {c.provider for c in evidence.connections if c.usable}
    admitted: list[str] = []
    blocked: Counter[str] = Counter()
    for route in sorted(evidence.inventory_route_ids | evidence.live_candidates.keys()):
        reason = _blocker(evidence, route, usable)
        if reason is None:
            admitted.append(route)
        else:
            blocked[reason] += 1
    proof = _proof_routes(admitted, cache_routes, now)
    report: dict[str, Any] = {}
    for name, task_family in TASK_FAMILIES.items():
        rows = [
            _row(
                evidence,
                route,
                proof.get(route),
                metadata=metadata,
                identity_map=identity_map,
                task_family=task_family,
                now=now,
            )
            for route in admitted
        ]
        eligible = _ordered([item for item in rows if item[1]["tool_proof"] == "fresh"])
        fallback = _ordered([item for item in rows if item[1]["tool_proof"] == "missing"])
        report[name] = {
            "task_family": task_family,
            "metric": rating_metric(task_family),
            "eligible": eligible,
            "fallback": fallback,
            "eligible_count": len(eligible),
            "fallback_count": len(fallback),
            "eligible_by_capacity": _by_capacity([CapacityClass(r["capacity"]) for r in eligible]),
            "fallback_by_capacity": _by_capacity([CapacityClass(r["capacity"]) for r in fallback]),
        }
    report.update(
        blocked_by_reason=dict(sorted(blocked.items())),
        dispatch_authorized=False,
        order="capacity(free,subscription,metered,unknown)->task_rating->route_id",
    )
    return report


def _route_provider(evidence: OmniRouteEvidence, route: str) -> str:
    candidate = evidence.live_candidates.get(route)
    if candidate is not None:
        return candidate.provider
    identity = evidence.route_identities.get(route)
    return identity[0] if identity is not None else pool_of(route)


def build_supply(
    evidence: OmniRouteEvidence, cache_routes: Mapping[str, HealthEntry] | None, now: datetime
) -> dict[str, Any]:
    """Configured supply per provider with every gap named; complete only with none."""
    candidates = evidence.live_candidates
    tool_proven = set(_proof_routes(list(candidates), cache_routes, now))
    providers = (
        {c.provider for c in evidence.connections}
        | {c.provider for c in candidates.values()}
        | {_route_provider(evidence, r) for r in evidence.inventory_route_ids}
    )
    rows = []
    for provider in sorted(providers):
        live = [c for c in candidates.values() if c.provider == provider]
        connections = [c for c in evidence.connections if c.provider == provider]
        rows.append(
            {
                "provider": provider,
                "connections": len(connections),
                "usable_connections": sum(c.usable for c in connections),
                "catalog_routes": sum(
                    _route_provider(evidence, r) == provider for r in evidence.inventory_route_ids
                ),
                "live_candidates": len(live),
                "live_reachable": sum(c.reachable for c in live),
                "traffic_routes": sum(
                    t.provider == provider for t in evidence.traffic_by_route.values()
                ),
                "traffic_proven_routes": sum(
                    t.provider == provider for t in evidence.proven_by_traffic.values()
                ),
                "tool_proven_routes": sum(c.route_id in tool_proven for c in live),
                "by_capacity": _by_capacity([_capacity(evidence, c.route_id) for c in live]),
            }
        )
    unknowns = {
        "candidates_source_unavailable" if name == "candidates" else f"source_unavailable:{name}"
        for name in evidence.source_errors
    }
    if cache_routes is None:
        unknowns.add("tool_proof_unavailable")
    if evidence.connections:
        unknowns.add("account_identity_not_reported")
    if "candidates" not in evidence.source_errors:
        unknowns.update(
            f"connections_without_live_candidates:{row['provider']}"
            for row in rows
            if row["connections"] and not row["live_candidates"]
        )
        missing = len(evidence.inventory_route_ids - candidates.keys())
        if missing:
            unknowns.add(f"catalog_routes_without_live_candidate:{missing}")
    unknowns.update(
        f"noauth_candidates:{c.provider}"
        for c in candidates.values()
        if _NOAUTH in c.connection_ids
    )
    live_reachable = sum(c.reachable for c in candidates.values())
    return {
        "complete": not unknowns,
        "unknowns": sorted(unknowns),
        "providers": rows,
        "totals": {
            "catalog_routes": len(evidence.inventory_route_ids),
            "live_candidates": len(candidates),
            "live_reachable": live_reachable,
            "tool_proven": len(tool_proven),
            "traffic_proven": len(evidence.proven_by_traffic),
            "connections": len(evidence.connections),
            "usable_connections": sum(c.usable for c in evidence.connections),
            "by_capacity": _by_capacity([_capacity(evidence, r) for r in candidates]),
        },
        "layers": {
            "catalog": len(evidence.inventory_route_ids),
            "live": live_reachable,
            "tool_proof": len(tool_proven),
            "task_performance": "not_measured",
        },
    }
