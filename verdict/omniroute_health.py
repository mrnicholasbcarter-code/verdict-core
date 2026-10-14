"""Live OmniRoute traffic and resilience joined into exact-route health evidence."""

from __future__ import annotations

import math
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any

import httpx

from verdict.admission import RuntimeEvidence, RuntimeObservation

STATS_PATH = "/api/usage/model-latency-stats?windowHours=168"
RESILIENCE_PATH = "/api/resilience/connections"
SOURCE = "omniroute:model-latency-stats+resilience/connections"
MIN_REQUESTS = 3
MIN_SUCCESS_RATE = 0.9
LIVE_EVIDENCE_TTL_SECONDS = 300


def _number(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return float("nan")
    return float(value)


def _usable(connection: Mapping[str, Any], model: str, route: str) -> bool:
    breaker = connection.get("breaker")
    if not (
        connection.get("isActive") is True
        and connection.get("connectionStatus") == "healthy"
        and _number(connection.get("cooldownRemainingMs")) == 0
        and isinstance(breaker, Mapping)
        and breaker.get("state") == "CLOSED"
    ):
        return False
    lockouts = connection.get("lockouts")
    if not isinstance(lockouts, list):
        return False
    return not any(
        isinstance(row, Mapping)
        and row.get("model") in {model, route, "*"}
        and (
            not math.isfinite(_number(row.get("remainingMs")))
            or _number(row.get("remainingMs")) > 0
        )
        for row in lockouts
    )


def evidence_from_omniroute_health(
    stats: Any, resilience: Any, *, now: datetime
) -> RuntimeEvidence:
    """>=90% success over >=3 calls in a 24-168h window plus live healthy connection.

    No provider-wide success, price, quota or capability claims are inferred.
    The live join expires after five minutes; callers fetch it anew, not from disk.
    """
    entries = stats.get("entries") if isinstance(stats, Mapping) else None
    connections = resilience.get("connections") if isinstance(resilience, Mapping) else None
    if not isinstance(entries, list) or not isinstance(connections, list):
        return RuntimeEvidence((), (SOURCE + ":absent",))
    observations = []
    for row in entries:
        if not isinstance(row, Mapping):
            continue
        provider, model = row.get("provider"), row.get("model")
        if not isinstance(provider, str) or not provider or not isinstance(model, str) or not model:
            continue
        route = f"{provider}/{model}"
        if row.get("key") != route:
            continue
        count = _number(row.get("totalRequests"))
        rate = _number(row.get("successRate"))
        window = _number(row.get("windowHours"))
        if not (
            math.isfinite(count)
            and count >= MIN_REQUESTS
            and MIN_SUCCESS_RATE <= rate <= 1
            and 24 <= window <= 168
        ):
            continue
        if not any(
            isinstance(conn, Mapping)
            and conn.get("provider") == provider
            and _usable(conn, model, route)
            for conn in connections
        ):
            continue
        observations.append(
            RuntimeObservation(
                key=f"route:{route}",
                state="healthy",
                category="passive_success",
                source=SOURCE,
                observed_at=now.isoformat(),
                until=(now + timedelta(seconds=LIVE_EVIDENCE_TTL_SECONDS)).isoformat(),
            )
        )
    return RuntimeEvidence(tuple(observations), (SOURCE,))


def load_omniroute_health(
    gateway: str,
    *,
    api_key: str | None,
    now: datetime,
    timeout: float = 20,
    transport: httpx.BaseTransport | None = None,
) -> RuntimeEvidence:
    """Read-only optional health source. Missing/failed data never grants health."""
    from verdict.free_tier_admit import normalize_omniroute_origin

    origin = normalize_omniroute_origin(gateway)
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    try:
        with httpx.Client(transport=transport, timeout=timeout, follow_redirects=False) as client:
            stats = client.get(origin + STATS_PATH, headers=headers)
            stats.raise_for_status()
            resilience = client.get(origin + RESILIENCE_PATH, headers=headers)
            resilience.raise_for_status()
            return evidence_from_omniroute_health(stats.json(), resilience.json(), now=now)
    except (httpx.HTTPError, ValueError):
        return RuntimeEvidence((), (SOURCE + ":absent",))
