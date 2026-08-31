"""Live OpenAI-compatible catalog fetch, probe, and named-check execute."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from urllib.parse import urljoin

import httpx

from verdict.live_routing import (
    ConcreteIdentity,
    LiveSurfaceBlocked,
    identity_from_row,
    named_check_passes,
)

DEFAULT_GATEWAY = "http://localhost:20128/v1"
NAMED_CHECK_PROMPT = 'Reply with only this JSON object and nothing else: {"golden_path":"ok"}'


def _client(*, timeout: float = 8.0) -> httpx.Client:
    return httpx.Client(timeout=timeout)


def _pricing_index(base_url: str) -> dict[str, dict[str, float]]:
    root = base_url.rstrip("/")
    if root.endswith("/v1"):
        root = root[: -len("/v1")]
    url = root + "/api/pricing"
    try:
        payload = _client(timeout=10.0).get(url).json()
    except (httpx.HTTPError, ValueError):
        return {}
    if not isinstance(payload, dict):
        return {}
    index: dict[str, dict[str, float]] = {}
    for provider, models in payload.items():
        if not isinstance(models, dict):
            continue
        for model_id, prices in models.items():
            if not isinstance(prices, dict):
                continue
            entry = {
                "input": float(prices.get("input") or 0),
                "output": float(prices.get("output") or 0),
            }
            index[str(model_id)] = entry
            index[f"{provider}/{model_id}"] = entry
    return index


def fetch_models(
    base_url: str = DEFAULT_GATEWAY, *, freshness_seconds: int = 3600
) -> tuple[list[ConcreteIdentity], datetime]:
    captured = datetime.now(timezone.utc)
    url = urljoin(base_url.rstrip("/") + "/", "models")
    try:
        response = _client(timeout=20.0).get(url)
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise LiveSurfaceBlocked(f"catalog fetch failed: {exc}") from exc
    rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise LiveSurfaceBlocked("catalog listing is not an OpenAI-compatible models list")
    pricing = _pricing_index(base_url)
    identities = []
    for row in rows:
        if not isinstance(row, dict) or not row.get("id"):
            continue
        model_id = str(row["id"])
        prices = pricing.get(model_id)
        if prices and "pricing" not in row:
            row = {**row, "pricing": prices}
        identities.append(
            identity_from_row(
                row, gateway_id=base_url, captured_at=captured, freshness_seconds=freshness_seconds
            )
        )
    if not identities:
        raise LiveSurfaceBlocked("empty_catalog")
    return identities, captured


def probe_identity(base_url: str, identity_id: str) -> bool:
    url = urljoin(base_url.rstrip("/") + "/", "chat/completions")
    try:
        response = _client(timeout=3.0).post(
            url,
            json={
                "model": identity_id,
                "messages": [{"role": "user", "content": "ping"}],
                "max_tokens": 8,
            },
        )
        if response.status_code >= 500:
            return False
        return response.status_code < 400
    except httpx.HTTPError:
        return False


def execute_chat(
    base_url: str,
    identity_id: str,
    messages: list[dict[str, str]],
    *,
    max_tokens: int = 64,
    timeout: float = 20.0,
) -> str:
    url = urljoin(base_url.rstrip("/") + "/", "chat/completions")
    try:
        response = _client(timeout=timeout).post(
            url, json={"model": identity_id, "messages": messages, "max_tokens": max_tokens}
        )
        response.raise_for_status()
        payload: dict[str, Any] = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise LiveSurfaceBlocked(f"execute failed: {exc}") from exc
    choices = payload.get("choices") or []
    if not choices:
        return ""
    message = (choices[0] or {}).get("message") or {}
    return str(message.get("content") or "")


def execute_named_check(base_url: str, identity_id: str) -> tuple[bool, str]:
    content = execute_chat(
        base_url,
        identity_id,
        [{"role": "user", "content": NAMED_CHECK_PROMPT}],
        max_tokens=64,
        timeout=12.0,
    )
    return named_check_passes(content), content
