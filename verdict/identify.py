"""Read-only identification: passive chat history and task ratings, not permission."""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime
from typing import Any

from verdict.credentials_store import resolve_gateway_binding
from verdict.free_tier_admit import normalize_omniroute_origin
from verdict.identify_rank import rank_models
from verdict.metadata.mapping import IdentityMap, load_identity_map
from verdict.metadata.records import ModelMetadataError
from verdict.metadata.store import MetadataSnapshot, load_store
from verdict.omniroute_evidence import OmniRouteEvidence, fetch_omniroute_evidence

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
) -> dict[str, Any]:
    """Report only exact mapped traffic routes; full supply is not exposed by A1."""
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
        "inventory_coverage": "unknown",
        "inventory_unknown_reason": "full_inventory_not_exposed_by_evidence_reader",
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
        "excluded_routes": excluded,
    }


def identify_report() -> dict[str, Any]:
    """Read the six passive OmniRoute sources and local metadata into one report."""
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
    report = identify_models(evidence, metadata=metadata, identity_map=identity_map)
    report.update(metadata_error=metadata_error, identity_map_error=mapping_error)
    return report


def cmd_identify(*, output_json: bool = False) -> None:
    report = identify_report()
    if output_json:
        print(json.dumps(report, sort_keys=True, allow_nan=False))
    else:
        print(
            f"Passive identify: {report['usable_connection_count']} usable connections; "
            f"{len(report['available_routes'])} available mapped traffic routes."
        )
        print("Inventory coverage unknown; traffic scope mixed_or_unknown; historical chat only.")
        print(f"Source errors: {', '.join(report['source_errors']) or 'none'}; model calls: 0.")
