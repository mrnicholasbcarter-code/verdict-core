"""OpenRouter public models endpoint source adapter.

Fetches ``https://openrouter.ai/api/v1/models`` (no key required) and
extracts per-model pricing facts.  The adapter only records what the
endpoint states; it never classifies a route as free.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from verdict.discovery.sources._fetcher import Fetcher, default_fetcher
from verdict.discovery.sources._types import DiscoveryRow

_SOURCE = "openrouter"
OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _price(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            f = float(value)
            return f if f >= 0 else None
        except (OverflowError, ValueError):
            pass
    if isinstance(value, str):
        try:
            f = float(value)
            return f if f >= 0 else None
        except ValueError:
            pass
    return None


def parse_openrouter(payload: Any, *, fetched_at: str | None = None) -> list[DiscoveryRow]:
    """Parse the ``/v1/models`` JSON payload from OpenRouter.

    Each row in ``data`` carries pricing under ``pricing.prompt`` /
    ``pricing.completion`` (strings or numbers), context length under
    ``context_length``, and tool calling under
    ``supported_parameters[*].name == "tools"``.
    """
    ts = fetched_at or _iso_now()
    rows: list[DiscoveryRow] = []
    if not isinstance(payload, dict):
        return rows
    data = payload.get("data")
    if not isinstance(data, list):
        return rows

    for row in data:
        if not isinstance(row, dict):
            continue
        model_id = row.get("id")
        if not isinstance(model_id, str) or not model_id:
            continue
        # Skip opaque routes
        if model_id.startswith("auto/") or model_id.startswith("combo/"):
            continue

        provider_name = model_id.split("/")[0] if "/" in model_id else "openrouter"

        pricing = row.get("pricing") or {}
        inp = _price(pricing.get("prompt")) if isinstance(pricing, dict) else None
        out = _price(pricing.get("completion")) if isinstance(pricing, dict) else None

        ctx_raw = row.get("context_length")
        ctx = int(ctx_raw) if isinstance(ctx_raw, int) else None

        # Detect tool calling support via supported_parameters
        supports_tools: bool | None = None
        sp = row.get("supported_parameters")
        if isinstance(sp, list):
            supports_tools = any(
                isinstance(p, dict) and p.get("name") == "tools" for p in sp
            ) or any(isinstance(p, str) and p == "tools" for p in sp)
            if not supports_tools:
                supports_tools = False

        rows.append(
            DiscoveryRow(
                id=model_id,
                provider=provider_name,
                source=_SOURCE,
                source_url=OPENROUTER_MODELS_URL,
                fetched_at=ts,
                input_price=inp,
                output_price=out,
                context_length=ctx,
                supports_tools=supports_tools,
            )
        )
    return rows


def fetch_openrouter(
    *, fetcher: Fetcher = default_fetcher, timeout: float = 20.0, max_bytes: int = 16 * 1024 * 1024
) -> list[DiscoveryRow]:
    """Fetch the OpenRouter public catalog and return discovery rows."""
    ts = _iso_now()
    raw = fetcher(OPENROUTER_MODELS_URL, timeout=timeout, max_bytes=max_bytes)
    payload = json.loads(raw)
    return parse_openrouter(payload, fetched_at=ts)
