"""verdict.discovery.omniroute_source — OmniRoute route source for discovery refresh.

Fetches the OmniRoute catalog and applies capacity classification to produce
a list of Routes ready to probe.  Only FREE and SUBSCRIPTION routes are
returned; unknown and priced routes are excluded.

This is an optional source for ``verdict discovery refresh``.  When no
OmniRoute key is available it returns an empty list rather than raising.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from verdict.discovery.model import Route

# Capacity class strings that may be probed
_PROBEABLE = frozenset({"free", "subscription"})

# Per-provider rate limits encoded as max_parallel
# (1 = serialized probes to that pool, which respects rate-limited providers)
_RATE_LIMITED_PROVIDERS: frozenset[str] = frozenset(
    {"ovhcloud", "kilocode", "kilo-gateway", "pollinations", "llm7"}
)


def load_omniroute_classified(env: Mapping[str, str] | None = None) -> list[tuple[Route, str, str]]:
    """Return (Route, capacity_class, capacity_fact) for free/subscription OmniRoute routes.

    Never raises; returns empty list on any error.
    """
    try:
        from verdict.capacity import classify_route_capacity
        from verdict.openrouter_free_set import load_openrouter_free_set
    except ImportError:
        return []

    # Connections snapshot is optional; if the accessor is unavailable, classify
    # without connection signals (capacity classification degrades gracefully).
    connections: dict[str, object] = {}
    try:
        from verdict.orchestration.run import (  # type: ignore[attr-defined]
            load_connections_snapshot,
        )

        snapshot = load_connections_snapshot(env=env)
        connections = snapshot.connections if snapshot is not None else {}
    except Exception:
        pass

    try:
        openrouter_free = load_openrouter_free_set()
    except Exception:
        openrouter_free = None

    try:
        import os
        import urllib.request

        # Try to get the OmniRoute base URL and key
        base = os.environ.get("OMNIROUTE_BASE_URL", "http://localhost:20128")
        key = None
        for var in ("OMNIROUTE_API_KEY", "VERDICT_OMNIROUTE_API_KEY", "OMNIROUTE_ACCESS_TOKEN"):
            key = os.environ.get(var, "")
            if key:
                break

        if not key:
            return []

        url = base.rstrip("/") + "/v1/models"
        req = urllib.request.Request(
            url, headers={"Authorization": f"Bearer {key}", "User-Agent": "verdict/0.1"}
        )
        with urllib.request.urlopen(req, timeout=30) as resp:  # nosec B310
            import json

            catalog = json.load(resp)
    except Exception:
        return []

    rows = catalog.get("data") if isinstance(catalog, dict) else None
    if not isinstance(rows, list):
        return []

    result: list[tuple[Route, str, str]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        route_id = str(row.get("id", ""))
        if not route_id or route_id.startswith("auto/") or route_id.startswith("combo/"):
            continue

        provider = str(row.get("owned_by", "")).split("/", 1)[0]
        conn: Any = connections.get(route_id) or connections.get(provider)

        try:
            cap_class, _plan_label, cap_fact = classify_route_capacity(
                conn, row, route_id=route_id, openrouter_free_set=openrouter_free
            )
        except Exception:
            continue

        class_str = cap_class.value if hasattr(cap_class, "value") else str(cap_class).lower()
        if class_str not in _PROBEABLE:
            continue

        upstream = str(row.get("owned_by", "")) or None
        is_rate_limited = provider in _RATE_LIMITED_PROVIDERS

        route = Route(
            provider="omniroute",
            model=route_id,
            base_url=(os.environ.get("OMNIROUTE_BASE_URL", "http://localhost:20128") + "/v1"),
            auth=f"env:{next((v for v in ('OMNIROUTE_API_KEY', 'VERDICT_OMNIROUTE_API_KEY', 'OMNIROUTE_ACCESS_TOKEN') if os.environ.get(v)), 'OMNIROUTE_API_KEY')}",
            source="omniroute_catalog",
            upstream=upstream,
            billing=class_str,
            max_parallel=1 if is_rate_limited else None,
        )
        result.append((route, class_str, cap_fact))

    return result
