"""Read OmniRoute's passive connection and traffic evidence without model probes.

Traffic evidence records historical proof, not dispatch permission. Callers must
also require a usable provider and respect ``cooled_models`` at dispatch time.
"""

from __future__ import annotations

import json
import math
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any, Literal
from urllib.parse import urlencode
from urllib.request import Request

from verdict.credentials_store import resolve_api_key
from verdict.http_safety import open_no_redirect

# BOD-345/S04 discovery bounds (same contract as verdict.free_tier_admit): a
# 20s per-HTTP-read timeout, a 30s aggregate deadline shared across the six
# GETs, a 16 MiB cumulative decoded-byte budget and a 50,000 raw-row cap per
# source. Any bound exceeded fails that source closed (``source_errors``),
# never a partial or truncated payload treated as healthy. The per-read bound
# relies on the socket timeout passed to ``open_no_redirect``; a server that
# trickles data just under that timeout is only caught by the aggregate
# wall-clock deadline between chunks, not by strict read cancellation.
_CHUNK_READ_BYTES = 64 * 1024
_PER_READ_TIMEOUT_SECONDS = 20.0
_AGGREGATE_DEADLINE_SECONDS = 30.0
_DECODED_BYTE_BUDGET = 16 * 1024 * 1024
_RAW_ROW_LIMIT = 50_000
_SOURCE_LIST_FIELD = {
    "providers": "connections",
    "resilience": "connections",
    "limits": "caches",
    "cooldowns": "items",
    "health_matrix": "providers",
    "v1_models": "data",
}


class _BoundedReadError(Exception):
    """A source exceeded a discovery bound; it is never served as partial-healthy."""


class _DecodedBudget:
    """Cumulative decoded-byte budget shared across the six evidence GETs."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self._used = 0

    def charge(self, nbytes: int) -> bool:
        if self._used + nbytes > self.limit:
            return False
        self._used += nbytes
        return True


@dataclass(frozen=True)
class ConnectionHealth:
    connection_id: str
    provider: str
    usable: bool
    reason: str | None
    last_tested: datetime | None
    test_status: str | None
    quota_state: Literal["unknown", "available", "exhausted"] = "unknown"
    quota_reason: str | None = None
    quota_observed_at: datetime | None = None


@dataclass(frozen=True)
class TrafficEvidence:
    route_id: str
    provider: str
    status: str
    requests: int
    successes: int
    last_request_at: datetime | None
    last_status: int | None = None


@dataclass(frozen=True)
class OmniRouteEvidence:
    fetched_at: datetime
    connections: tuple[ConnectionHealth, ...]
    usable_providers: frozenset[str]
    proven_by_traffic: Mapping[str, TrafficEvidence]
    cooled_models: frozenset[str]
    source_errors: tuple[str, ...]
    route_identities: Mapping[str, tuple[str, str]] = field(default_factory=dict)
    route_capacities: Mapping[str, str] = field(default_factory=dict)
    inventory_route_ids: frozenset[str] = frozenset()
    traffic_by_route: Mapping[str, TrafficEvidence] = field(default_factory=dict)


def _date(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return result if result.tzinfo else result.replace(tzinfo=timezone.utc)


def _rows(value: Any) -> list[dict[str, Any]]:
    return [row for row in value if isinstance(row, dict)] if isinstance(value, list) else []


def _number(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


# Verdict freshness policy, not an upstream API TTL; no additional scheduler.
QUOTA_MAX_SYNC_INTERVALS = 2


def _quota_evidence(
    cache: dict[str, Any] | None, interval_minutes: Any, now: datetime
) -> tuple[Literal["unknown", "available", "exhausted"], str | None, datetime | None]:
    if cache is None:
        return "unknown", "cache_missing", None
    observed = _date(cache.get("fetchedAt"))
    interval = _number(interval_minutes)
    if interval is None or not math.isfinite(interval) or interval <= 0:
        return "unknown", "sync_interval_unknown", observed
    if observed is None or observed > now:
        return "unknown", "observation_time_unknown", observed
    age_minutes = (now - observed).total_seconds() / 60
    if age_minutes > QUOTA_MAX_SYNC_INTERVALS * interval:
        return "unknown", "observation_stale", observed
    quotas = cache.get("quotas")
    if not isinstance(quotas, dict) or not quotas or cache.get("message"):
        return "unknown", "quota_unreported", observed
    unknown = False
    for window in quotas.values():
        if not isinstance(window, dict):
            unknown = True
            continue
        # Only native explicit-global metadata establishes connection scope.
        # Generic names such as weekly/credits are not a scope contract.
        scope = window.get("claudeQuota")
        if not isinstance(scope, dict) or scope.get("kind") not in {"session", "weekly_all"}:
            unknown = True
            continue
        reset = _date(window.get("resetAt"))
        if reset is not None and reset <= now:
            unknown = True
            continue
        if window.get("unlimited") is True:
            continue
        if window.get("fractionReported") is False or reset is None:
            unknown = True
            continue
        remaining = _number(window.get("remainingPercentage"))
        if remaining is None:
            remaining = _number(window.get("remaining"))
        if remaining is None:
            used, total = _number(window.get("used")), _number(window.get("total"))
            if used is not None and total is not None and total > 0:
                remaining = total - used
        if remaining is None or not math.isfinite(remaining):
            unknown = True
        elif remaining <= 0:
            # Claude current-limit percentages can be predictive display data.
            # Native preflight requires active critical metadata and a future reset.
            if (
                scope.get("active") is True
                and scope.get("severity") in {None, "critical"}
                and reset is not None
            ):
                return "exhausted", "global_window_exhausted", observed
            unknown = True
    if cache.get("modelQuotas"):
        unknown = True
    if unknown:
        return "unknown", "window_scope_or_value_unknown", observed
    return "available", None, observed


def _connection_reason(
    row: dict[str, Any],
    live: dict[str, Any] | None,
    quota_state: Literal["unknown", "available", "exhausted"],
    breakers: dict[str, str],
    now: datetime,
    missing: bool,
) -> str | None:
    if row.get("isActive") is not True:
        return "inactive"
    if row.get("testStatus") not in {"active", "passed"}:
        return "test_not_passed"
    if missing:
        return "availability_unknown"
    if live is None:
        return "connection_evidence_missing"
    if live.get("connectionStatus") != "healthy":
        return "connection_not_healthy"
    until = _date(live.get("rateLimitedUntil"))
    if (until and until > now) or (_number(live.get("cooldownRemainingMs")) or 0) > 0:
        return "cooling_down"
    breaker = live.get("breaker")
    state = breaker.get("state") if isinstance(breaker, dict) else breakers.get(row["provider"])
    if state is not None and state != "CLOSED":
        return "breaker_not_closed"
    if quota_state == "exhausted":
        return "quota_exhausted"
    return None


def parse_omniroute_evidence(
    *,
    providers: Any,
    resilience: Any,
    limits: Any,
    cooldowns: Any,
    health_matrix: Any,
    v1_models: Any,
    now: datetime,
) -> OmniRouteEvidence:
    """Parse audited API shapes; missing source payloads never grant availability.

    Sparse or null quota rows remain unknown, independently of connection health.
    Only fresh, reported, explicitly global blocking windows exclude connections.
    Model-scoped or unresolved windows remain the gateway's request-time concern.
    Catalog aliases are retained only when owned_by + root match exactly.
    """
    sources = {
        "providers": (providers, "connections", list),
        "resilience": (resilience, "connections", list),
        "limits": (limits, "caches", dict),
        "cooldowns": (cooldowns, "items", list),
        "health_matrix": (health_matrix, "providers", list),
        "v1_models": (v1_models, "data", list),
    }
    errors = tuple(
        name
        for name, (payload, key, kind) in sources.items()
        if not isinstance(payload, dict) or not isinstance(payload.get(key), kind)
    )
    live = (
        {
            row["id"]: row
            for row in _rows(resilience.get("connections"))
            if isinstance(row.get("id"), str)
        }
        if "resilience" not in errors
        else {}
    )
    breakers = (
        {
            row["name"]: str(row.get("state", "UNKNOWN"))
            for row in _rows(resilience.get("breakers"))
            if isinstance(row.get("name"), str)
        }
        if "resilience" not in errors
        else {}
    )
    caches = limits["caches"] if "limits" not in errors else {}
    missing = bool(set(errors) & {"providers", "resilience", "limits", "cooldowns"})
    connections = []
    for row in _rows(providers.get("connections")) if "providers" not in errors else []:
        connection_id, provider = row.get("id"), row.get("provider")
        if not isinstance(connection_id, str) or not isinstance(provider, str):
            continue
        cache = caches.get(connection_id)
        quota_state, quota_reason, observed = _quota_evidence(
            cache if isinstance(cache, dict) else None,
            limits.get("intervalMinutes") if "limits" not in errors else None,
            now,
        )
        if "limits" in errors:
            quota_reason = "source_unavailable"
        reason = _connection_reason(
            row, live.get(connection_id), quota_state, breakers, now, missing
        )
        connections.append(
            ConnectionHealth(
                connection_id,
                provider,
                reason is None,
                reason,
                _date(row.get("lastTested")),
                row.get("testStatus") if isinstance(row.get("testStatus"), str) else None,
                quota_state,
                quota_reason,
                observed,
            )
        )
    from verdict.orchestration.eligibility import capacity_class_of

    usable_ids = {c.connection_id for c in connections if c.usable}
    provider_rows = _rows(providers.get("connections")) if "providers" not in errors else []
    identities: dict[str, tuple[str, str]] = {}
    capacities: dict[str, str] = {}
    routes: dict[tuple[str, str], list[str]] = {}
    inventory: set[str] = set()
    for row in _rows(v1_models.get("data")) if "v1_models" not in errors else []:
        route_id, provider = row.get("id"), row.get("owned_by", row.get("provider"))
        if isinstance(route_id, str) and route_id:
            inventory.add(route_id)
        if not isinstance(route_id, str) or not isinstance(provider, str):
            continue
        model = row.get("root") or route_id.partition("/")[2]
        if isinstance(model, str) and model:
            # Only the catalog's explicit root establishes shared model identity.
            if isinstance(row.get("root"), str) and row["root"]:
                identities[route_id] = (provider, row["root"])
            classes = {
                capacity_class_of(conn, row, route_id=route_id)[0].value
                for conn in provider_rows
                if conn.get("id") in usable_ids and conn.get("provider") == provider
            }
            capacities[route_id] = next(iter(classes)) if len(classes) == 1 else "unknown"
            routes.setdefault((provider, model), []).append(route_id)
            routes.setdefault((provider, route_id), []).append(route_id)
    cooled: set[str] = set()
    for row in _rows(cooldowns.get("items")) if "cooldowns" not in errors else []:
        model, provider = row.get("model"), row.get("provider")
        if isinstance(model, str):
            cooled.add(model)
            if isinstance(provider, str):
                cooled.update(routes.get((provider, model), []))
    proven: dict[str, TrafficEvidence] = {}
    traffic_by_route: dict[str, TrafficEvidence] = {}
    for group in _rows(health_matrix.get("providers")) if "health_matrix" not in errors else []:
        provider = group.get("provider")
        if not isinstance(provider, str):
            continue
        for account in _rows(group.get("accounts")):
            for row in _rows(account.get("models")):
                model = row.get("model")
                if not isinstance(model, str):
                    continue
                successes = _number(row.get("successes")) or 0
                requests = _number(row.get("requests")) or 0
                last = _number(row.get("lastStatus"))
                status = row.get("status")
                is_proven = successes > 0 and (
                    status in {"healthy", "degraded"} or (last is not None and 200 <= last < 400)
                )
                for route_id in routes.get((provider, model), []):
                    candidate = TrafficEvidence(
                        route_id,
                        provider,
                        str(status or "unknown"),
                        int(requests),
                        int(successes),
                        _date(row.get("lastRequestAt")),
                        int(last) if last is not None else None,
                    )
                    targets = [traffic_by_route, proven] if is_proven else [traffic_by_route]
                    for target in targets:
                        previous = target.get(route_id)
                        if previous is None or (
                            candidate.last_request_at or datetime.min.replace(tzinfo=timezone.utc)
                        ) > (previous.last_request_at or datetime.min.replace(tzinfo=timezone.utc)):
                            target[route_id] = candidate
    return OmniRouteEvidence(
        now,
        tuple(connections),
        frozenset(c.provider for c in connections if c.usable),
        MappingProxyType(proven),
        frozenset(cooled),
        errors,
        MappingProxyType(identities),
        MappingProxyType(capacities),
        frozenset(inventory),
        MappingProxyType(traffic_by_route),
    )


def _read_chunks_bounded(response: Any, *, budget: _DecodedBudget, deadline: float) -> bytes:
    """Read a response body in fixed chunks, never past the byte budget or deadline.

    Each chunk read is itself bounded by the socket timeout already passed to
    ``open_no_redirect``; a server that trickles bytes just under that
    per-read timeout is only caught here, between chunks, by the aggregate
    wall-clock deadline. This is an honest wall-clock/byte bound, not strict
    read cancellation.
    """
    chunks: list[bytes] = []
    while True:
        if time.monotonic() > deadline:
            raise _BoundedReadError("aggregate discovery deadline exceeded")
        chunk = response.read(_CHUNK_READ_BYTES)
        if not chunk:
            break
        if not budget.charge(len(chunk)):
            raise _BoundedReadError(f"exceeded the decoded byte budget ({budget.limit} bytes)")
        chunks.append(chunk)
    return b"".join(chunks)


def _request_json_bounded(
    base_url: str,
    name: str,
    path: str,
    api_key: str | None,
    timeout: float,
    *,
    budget: _DecodedBudget,
    deadline: float,
    row_limit: int,
) -> Any:
    """GET one evidence source under the shared S04 discovery bounds.

    Reads the body in fixed chunks against the cumulative decoded-byte
    budget and the aggregate deadline, then caps the source's own list field
    at ``row_limit`` raw rows. Any bound exceeded raises ``_BoundedReadError``;
    the caller records that source as failed, never as a truncated success.
    """
    if time.monotonic() > deadline:
        raise _BoundedReadError("aggregate discovery deadline exceeded")
    headers = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    request = Request(base_url.rstrip("/") + path, headers=headers)
    with open_no_redirect(request, timeout=timeout) as response:
        raw = _read_chunks_bounded(response, budget=budget, deadline=deadline)
    payload = json.loads(raw)
    field = _SOURCE_LIST_FIELD.get(name)
    rows = payload.get(field) if field is not None and isinstance(payload, dict) else None
    if isinstance(rows, list) and len(rows) > row_limit:
        raise _BoundedReadError(f"{name} exceeded the raw row limit {row_limit}")
    return payload


def fetch_omniroute_evidence(
    base_url: str,
    *,
    api_key: str | None,
    timeout: float = 30.0,
    health_range: str = "7d",
    aggregate_deadline_seconds: float = _AGGREGATE_DEADLINE_SECONDS,
    decoded_byte_budget: int = _DECODED_BYTE_BUDGET,
    raw_row_limit: int = _RAW_ROW_LIMIT,
) -> OmniRouteEvidence:
    """GET the six evidence sources, isolating failures by source name only.

    Each read is bounded by the S04 discovery bounds (per-read timeout,
    aggregate deadline, cumulative decoded-byte budget, raw-row cap). A
    source exceeding any bound is recorded as a source error, never served
    partial.
    """
    key = api_key if api_key is not None else resolve_api_key()
    paths = {
        "providers": "/api/providers",
        "resilience": "/api/resilience/connections",
        "limits": "/api/usage/provider-limits",
        "cooldowns": "/api/resilience/model-cooldowns",
        "health_matrix": "/api/providers/health-matrix?" + urlencode({"range": health_range}),
        "v1_models": "/v1/models",
    }
    budget = _DecodedBudget(max(1, decoded_byte_budget))
    deadline = time.monotonic() + aggregate_deadline_seconds
    payloads: dict[str, Any] = {}
    for name, path in paths.items():
        try:
            payloads[name] = _request_json_bounded(
                base_url,
                name,
                path,
                key,
                timeout,
                budget=budget,
                deadline=deadline,
                row_limit=raw_row_limit,
            )
        except (OSError, ValueError, _BoundedReadError):
            # Exception bodies and URLs can echo credentials. Retain only source names.
            payloads[name] = None
    return parse_omniroute_evidence(**payloads, now=datetime.now(timezone.utc))
