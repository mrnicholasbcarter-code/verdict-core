"""Kilo public model listing source adapter.

Fetches ``https://api.kilo.ai/api/gateway/models`` (no key required) and
extracts per-model facts including the ``isFree`` flag.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from verdict.discovery.sources._fetcher import Fetcher, default_fetcher
from verdict.discovery.sources._types import DiscoveryRow

_SOURCE = "kilo"
KILO_MODELS_URL = "https://api.kilo.ai/api/gateway/models"
_PROVIDER_PREFIX = "kc"  # canonical OmniRoute prefix for Kilo routes


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _price(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            f = float(value)
            return f if f >= 0 else None
        except (OverflowError, ValueError):
            pass
    return None


def parse_kilo(
    payload: Any, *, fetched_at: str | None = None, provider_prefix: str = _PROVIDER_PREFIX
) -> list[DiscoveryRow]:
    """Parse the Kilo ``/models`` JSON payload.

    Kilo marks free models with ``isFree: true`` and may include 0/0 pricing.
    The ``id`` field is the model's short name; the canonical route id is
    ``<provider_prefix>/<model_id>``.
    """
    ts = fetched_at or _iso_now()
    rows: list[DiscoveryRow] = []

    # Kilo returns either a list directly or {"models": [...]} or {"data": [...]}
    if isinstance(payload, list):
        model_list = payload
    elif isinstance(payload, dict):
        model_list = payload.get("models") or payload.get("data") or []
    else:
        return rows

    if not isinstance(model_list, list):
        return rows

    for row in model_list:
        if not isinstance(row, dict):
            continue
        model_id = row.get("id") or row.get("modelId")
        if not isinstance(model_id, str) or not model_id:
            continue

        route_id = model_id if "/" in model_id else f"{provider_prefix}/{model_id}"

        pricing = row.get("pricing") or {}
        inp = _price(pricing.get("prompt")) if isinstance(pricing, dict) else None
        out = _price(pricing.get("completion")) if isinstance(pricing, dict) else None

        is_free_raw = row.get("isFree")
        free_flag: bool | None = bool(is_free_raw) if isinstance(is_free_raw, bool) else None

        # Zero 0/0 pricing implies free if no explicit isFree
        if free_flag is None and inp == 0.0 and out == 0.0:
            free_flag = True

        ctx_raw = row.get("contextLength") or row.get("context_length") or row.get("context_window")
        ctx = int(ctx_raw) if isinstance(ctx_raw, int) else None

        rows.append(
            DiscoveryRow(
                id=route_id,
                provider=provider_prefix,
                source=_SOURCE,
                source_url=KILO_MODELS_URL,
                fetched_at=ts,
                input_price=inp,
                output_price=out,
                free_flag=free_flag,
                context_length=ctx,
            )
        )
    return rows


def fetch_kilo(
    *, fetcher: Fetcher = default_fetcher, timeout: float = 20.0, max_bytes: int = 8 * 1024 * 1024
) -> list[DiscoveryRow]:
    """Fetch the Kilo public model listing and return discovery rows."""
    ts = _iso_now()
    raw = fetcher(KILO_MODELS_URL, timeout=timeout, max_bytes=max_bytes)
    payload = json.loads(raw)
    return parse_kilo(payload, fetched_at=ts)
