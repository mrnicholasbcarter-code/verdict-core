"""models.dev public catalog source adapter.

Fetches the models.dev public catalog and extracts per-model facts including
context length, tool support, and pricing where stated.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from verdict.discovery.sources._fetcher import Fetcher, default_fetcher
from verdict.discovery.sources._pricing import parse_price
from verdict.discovery.sources._types import DiscoveryRow

_SOURCE = "models_dev"
MODELS_DEV_URL = "https://models.dev/api/v0/models"


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


# Use the shared price parser so numeric strings like "0" or "0.000001" are accepted.
_price = parse_price


def _provider_of(model_id: str) -> str:
    return model_id.split("/")[0] if "/" in model_id else "unknown"


def parse_models_dev(payload: Any, *, fetched_at: str | None = None) -> list[DiscoveryRow]:
    """Parse the models.dev catalog JSON payload.

    The catalog is a list (or ``{"models": [...]}`` dict) of model objects.
    Each object may carry ``id``, ``contextWindow``, ``pricing``, and
    capability flags.
    """
    ts = fetched_at or _iso_now()
    rows: list[DiscoveryRow] = []

    if isinstance(payload, dict):
        model_list = payload.get("models") or payload.get("data") or []
    elif isinstance(payload, list):
        model_list = payload
    else:
        return rows

    if not isinstance(model_list, list):
        return rows

    for row in model_list:
        if not isinstance(row, dict):
            continue
        model_id = row.get("id")
        if not isinstance(model_id, str) or not model_id:
            continue

        provider_name = _provider_of(model_id)

        pricing = row.get("pricing") or {}
        inp = (
            _price(pricing.get("prompt") or pricing.get("input"))
            if isinstance(pricing, dict)
            else None
        )
        out = (
            _price(pricing.get("completion") or pricing.get("output"))
            if isinstance(pricing, dict)
            else None
        )

        ctx_raw = row.get("contextWindow") or row.get("context_window") or row.get("context_length")
        ctx = int(ctx_raw) if isinstance(ctx_raw, int) else None

        # Tool calling: check capabilities or top-level field
        caps = row.get("capabilities") or {}
        supports_tools: bool | None = None
        if isinstance(caps, dict):
            tool_key = caps.get("tools") or caps.get("tool_calling") or caps.get("function_calling")
            if isinstance(tool_key, bool):
                supports_tools = tool_key
        if supports_tools is None:
            top_tools = row.get("tools") or row.get("tool_calling")
            if isinstance(top_tools, bool):
                supports_tools = top_tools

        rows.append(
            DiscoveryRow(
                id=model_id,
                provider=provider_name,
                source=_SOURCE,
                source_url=MODELS_DEV_URL,
                fetched_at=ts,
                input_price=inp,
                output_price=out,
                context_length=ctx,
                supports_tools=supports_tools,
            )
        )
    return rows


def fetch_models_dev(
    *, fetcher: Fetcher = default_fetcher, timeout: float = 20.0, max_bytes: int = 16 * 1024 * 1024
) -> list[DiscoveryRow]:
    """Fetch the models.dev catalog and return discovery rows."""
    ts = _iso_now()
    raw = fetcher(MODELS_DEV_URL, timeout=timeout, max_bytes=max_bytes)
    payload = json.loads(raw)
    return parse_models_dev(payload, fetched_at=ts)
