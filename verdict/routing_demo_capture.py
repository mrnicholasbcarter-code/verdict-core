"""Recorded-catalog serialization for the routing demo."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from verdict.live_routing import ConcreteIdentity, LiveSurfaceBlocked, identity_from_row

SCHEMA_VERSION = "routing-demo/v1"


def identities_to_rows(identities: list[ConcreteIdentity]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in identities:
        rows.append(
            {
                "id": item.identity_id,
                "owned_by": item.provider_id,
                "cost_class": item.cost_class,
                "context_length": item.context_limit,
                "max_output_tokens": item.output_limit,
                "capabilities": {
                    "tools": item.tools,
                    "context": item.context_limit,
                    "max_output": item.output_limit,
                },
                "modalities": list(item.modalities or ()),
            }
        )
    return rows


def rows_to_identities(
    rows: list[dict[str, Any]], *, gateway_id: str, captured_at: datetime
) -> list[ConcreteIdentity]:
    return [
        identity_from_row(row, gateway_id=gateway_id, captured_at=captured_at)
        for row in rows
        if row.get("id")
    ]


def save_recorded_capture(
    path: Path,
    *,
    gateway_base_url: str,
    captured_at: datetime,
    identities: list[ConcreteIdentity],
    pricing: dict[str, dict[str, float]],
) -> None:
    payload = {
        "schema_version": SCHEMA_VERSION,
        "mode": "recorded",
        "gateway_base_url": gateway_base_url,
        "catalog_captured_at": captured_at.isoformat(),
        "catalog_rows": identities_to_rows(identities),
        "pricing_index": pricing,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def load_recorded_capture(
    path: Path, *, default_gateway: str
) -> tuple[list[ConcreteIdentity], dict[str, dict[str, float]], datetime, str]:
    payload = json.loads(path.read_text())
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise LiveSurfaceBlocked("recorded capture schema mismatch")
    captured_raw = payload.get("catalog_captured_at")
    if not captured_raw:
        raise LiveSurfaceBlocked("recorded capture missing catalog_captured_at")
    captured_at = datetime.fromisoformat(str(captured_raw))
    if captured_at.tzinfo is None:
        captured_at = captured_at.replace(tzinfo=timezone.utc)
    gateway = str(payload.get("gateway_base_url") or default_gateway)
    rows = payload.get("catalog_rows")
    pricing = payload.get("pricing_index")
    if not isinstance(rows, list) or not isinstance(pricing, dict):
        raise LiveSurfaceBlocked("recorded capture missing catalog_rows/pricing_index")
    identities = rows_to_identities(rows, gateway_id=gateway, captured_at=captured_at)
    if not identities:
        raise LiveSurfaceBlocked("recorded capture empty catalog")
    typed_pricing: dict[str, dict[str, float]] = {}
    for key, value in pricing.items():
        if isinstance(value, dict):
            typed_pricing[str(key)] = {
                "input": float(value.get("input") or value.get("prompt") or 0.0),
                "output": float(value.get("output") or value.get("completion") or 0.0),
            }
    return identities, typed_pricing, captured_at, gateway


__all__ = [
    "SCHEMA_VERSION",
    "identities_to_rows",
    "load_recorded_capture",
    "rows_to_identities",
    "save_recorded_capture",
]
