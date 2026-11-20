"""OmniRoute discovery source adapter.

Wraps ``OmniRouteEvidence`` produced by
:func:`~verdict.omniroute_evidence.fetch_omniroute_evidence`.  The adapter
accepts the evidence object directly so it can be tested offline via
:func:`~verdict.omniroute_evidence.parse_omniroute_evidence` with fixture
dicts.

Sources used:
- ``/v1/models`` — the full inventory (``evidence.inventory_rows``)
- ``/api/providers`` / snapshot — connection facts (``evidence.connections``)
- ``/api/free-tier/summary`` — explicitly-free model IDs (optional caller
  responsibility; pass ``free_tier_ids`` if already fetched)
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from datetime import datetime, timezone
from typing import Any

from verdict.discovery.sources._pricing import parse_price
from verdict.discovery.sources._types import DiscoveryRow

_SOURCE = "omniroute"
_V1_MODELS_URL = "/v1/models"
_FREE_TIER_URL = "/api/free-tier/summary"


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


# Use the shared price parser so numeric strings like "0" or "0.000001" are accepted.
_price = parse_price


def rows_from_evidence(
    evidence: Any,
    *,
    base_url: str = "http://localhost:20128",
    free_tier_ids: Collection[str] = (),
    fetched_at: str | None = None,
) -> list[DiscoveryRow]:
    """Return :class:`~verdict.discovery.sources._types.DiscoveryRow` objects
    extracted from *evidence* (an ``OmniRouteEvidence`` instance).

    ``free_tier_ids`` is the set of model IDs from
    ``/api/free-tier/summary``'s ``perModel`` list.  Pass it if you have
    already fetched that endpoint; omit it when that source is unavailable
    (e.g. a restricted key).

    This function never makes HTTP calls; all data comes from *evidence*.
    """
    ts = fetched_at or _iso_now()
    source_url = base_url.rstrip("/") + _V1_MODELS_URL
    free_set = frozenset(free_tier_ids)

    rows: list[DiscoveryRow] = []

    inventory: Mapping[str, Mapping[str, Any]] = getattr(evidence, "inventory_rows", {})
    for route_id, row in inventory.items():
        if not isinstance(route_id, str) or not isinstance(row, dict):
            continue
        # Skip opaque routes; they are never candidates.
        if route_id.startswith("auto/") or route_id.startswith("combo/"):
            continue

        owned_by = row.get("owned_by") or row.get("provider") or ""
        provider_name = str(owned_by) if owned_by else route_id.split("/")[0]

        pricing = row.get("pricing") or {}
        inp = _price(pricing.get("prompt")) if isinstance(pricing, dict) else None
        out = _price(pricing.get("completion")) if isinstance(pricing, dict) else None

        ctx_raw = row.get("context_length") or row.get("context_window")
        ctx = int(ctx_raw) if isinstance(ctx_raw, int) else None

        # tool calling: check capabilities dict
        caps = row.get("capabilities") or {}
        supports_tools: bool | None = None
        if isinstance(caps, dict):
            tool_key = caps.get("tools") or caps.get("function_calling")
            if isinstance(tool_key, bool):
                supports_tools = tool_key

        free_flag: bool | None = None
        if free_set:
            free_flag = route_id in free_set

        rows.append(
            DiscoveryRow(
                id=route_id,
                provider=provider_name,
                source=_SOURCE,
                source_url=source_url,
                fetched_at=ts,
                input_price=inp,
                output_price=out,
                free_flag=free_flag,
                context_length=ctx,
                supports_tools=supports_tools,
            )
        )

    return rows


def parse_free_tier_ids(payload: Any) -> frozenset[str]:
    """Extract model IDs from a ``/api/free-tier/summary`` JSON payload.

    Returns an empty frozenset if the payload is malformed.
    """
    if not isinstance(payload, dict):
        return frozenset()
    per_model = payload.get("perModel")
    if not isinstance(per_model, list):
        return frozenset()
    return frozenset(item for item in per_model if isinstance(item, str) and item)
