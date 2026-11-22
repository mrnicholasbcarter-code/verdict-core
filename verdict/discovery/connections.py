"""Read-only billing inventory for the operator's OmniRoute connections."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from verdict.capacity import capacity_of
from verdict.connection_billing import check_drift, lookup, snapshot_meta
from verdict.credentials_store import resolve_management_token
from verdict.free_tier_admit import normalize_omniroute_origin, omniroute_endpoint_from_env
from verdict.gateway_lifecycle import is_loopback_gateway
from verdict.orchestration.connections_snapshot import read_connections_snapshot
from verdict.orchestration.run import sanitize_connections


def _connection_inventory() -> tuple[str, list[dict[str, Any]]]:
    from time import monotonic

    from verdict.omniroute_evidence import _DecodedBudget, _request_json_bounded

    if "VERDICT_CONNECTIONS_SNAPSHOT" in os.environ:
        return "connections_snapshot", read_connections_snapshot(
            Path(os.environ["VERDICT_CONNECTIONS_SNAPSHOT"])
        )
    endpoint = omniroute_endpoint_from_env()
    if endpoint is None:
        raise ValueError("no gateway")
    base_url, inference_key = endpoint
    origin = normalize_omniroute_origin(base_url)
    if not is_loopback_gateway(origin):
        raise ValueError("a nonlocal gateway requires VERDICT_CONNECTIONS_SNAPSHOT")
    payload = _request_json_bounded(
        origin,
        "providers",
        "/api/providers",
        resolve_management_token() or inference_key,
        10.0,
        budget=_DecodedBudget(8 * 1024 * 1024),
        deadline=monotonic() + 10.0,
        row_limit=10_000,
    )
    rows = payload.get("connections") if isinstance(payload, Mapping) else None
    if not isinstance(rows, list) or any(not isinstance(row, Mapping) for row in rows):
        raise ValueError("invalid connection inventory")
    return "gateway_providers", sanitize_connections(payload)


def run_connections_action() -> dict[str, Any]:
    """Read connections without probing models or modifying local state."""
    from verdict.omniroute_evidence import _BoundedReadError

    meta = snapshot_meta()
    result: dict[str, Any] = {
        "schema": "verdict.discovery.connections/v1",
        "snapshot_version": meta.get("omniroute_version"),
        "snapshot_sha256": meta.get("source_sha256"),
        "drift_status": check_drift().get("status"),
        "connections": [],
    }
    try:
        inventory_source, inventory = _connection_inventory()
    except (OSError, ValueError, _BoundedReadError) as exc:
        code = getattr(exc, "code", None)
        return {
            **result,
            "status": "unavailable",
            "error": f"http_{code}" if isinstance(code, int) else type(exc).__name__,
        }
    rows = []
    for conn in inventory:
        provider = conn.get("provider")
        auth_type = conn.get("authType")
        capacity, deciding_fact = capacity_of(conn, {}, route_id="", openrouter_free_set=None)
        fact = (
            lookup(provider, auth_type if isinstance(auth_type, str) else None)
            if isinstance(provider, str)
            else None
        )
        rows.append(
            {
                "connection_id": conn.get("id"),
                "provider": provider,
                "authType": auth_type,
                "active": conn.get("isActive"),
                "billing": fact.billing if fact else None,
                "overage": fact.overage if fact else None,
                "source": fact.source if fact else None,
                "capacity": capacity.value,
                "deciding_fact": deciding_fact,
            }
        )
    return {**result, "status": "ok", "inventory_source": inventory_source, "connections": rows}


def render_connections_plain(result: dict[str, Any]) -> str:
    """Render connection identity and billing evidence without credentials."""
    version = result.get("snapshot_version") or "?"
    drift = result.get("drift_status") or "?"
    lines = [f"Snapshot: OmniRoute {version}  drift={drift}", ""]
    if result.get("status") != "ok":
        return "\n".join([*lines, f"Connection inventory unavailable: {result.get('error')}"])
    rows = result["connections"]
    if not rows:
        return "\n".join([*lines, "(no connections)"])
    fields = (
        "connection_id",
        "provider",
        "authType",
        "capacity",
        "deciding_fact",
        "overage",
        "source",
    )
    lines.append("  ".join(fields))
    lines.extend("  ".join(str(row.get(key) or "—") for key in fields) for row in rows)
    return "\n".join(lines)
