"""Read OmniRoute's passive connection, traffic and live candidate evidence without model probes.

Seven bounded GETs: five management reads under ``/api``, plus the inference
catalog (``/v1/models``) and the live auto-combo candidate pool
(``/v1/auto-combo/auto/candidates``), both read with the inference key.
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
# 20s per-HTTP-read timeout, a 30s aggregate deadline shared across the seven
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
    "candidates": "candidates",
}
_INFERENCE_SOURCES = frozenset({"v1_models", "candidates"})


class _BoundedReadError(Exception):
    """A source exceeded a discovery bound; it is never served as partial-healthy."""


class _DecodedBudget:
    """Cumulative decoded-byte budget shared across the seven evidence GETs."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self._used = 0

    def charge(self, nbytes: int) -> bool:
        if self._used + nbytes > self.limit:
            return False
        self._used += nbytes
        return True


@dataclass(frozen=True)
class QuotaWindow:
    """One reported quota window, kept apart from the connection verdict.

    ``scope`` is the explicit upstream kind, or ``unresolved`` when the payload
    does not name a connection-wide scope. ``state`` describes this window only.
    """

    name: str
    scope: str
    state: Literal["unknown", "available", "exhausted"]
    remaining_percent: float | None
    reset_at: str | None


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
    quota_windows: tuple[QuotaWindow, ...] = ()


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
class LiveCandidate:
    """One route in OmniRoute's live auto-combo candidate pool.

    ``reachable`` is True only when at least one connection row for the route
    is usable; ``blocked_reasons`` names why the other rows are not.
    """

    route_id: str
    provider: str
    connection_ids: tuple[str, ...]
    reachable: bool
    blocked_reasons: tuple[str, ...] = ()


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
    route_capacity_evidence: Mapping[str, str] = field(default_factory=dict)
    live_candidates: Mapping[str, LiveCandidate] = field(default_factory=dict)
    model_cooldowns: tuple[Mapping[str, Any], ...] = ()


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
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (OverflowError, ValueError):
        return None
    return number if math.isfinite(number) else None


# Verdict freshness policy, not an upstream API TTL; no additional scheduler.
QUOTA_MAX_SYNC_INTERVALS = 2


def _window_remaining(window: dict[str, Any]) -> float | None:
    remaining = _number(window.get("remainingPercentage"))
    if remaining is None:
        remaining = _number(window.get("remaining"))
    if remaining is None:
        used, total = _number(window.get("used")), _number(window.get("total"))
        if used is not None and total is not None and total > 0:
            remaining = total - used
    return remaining if remaining is not None and math.isfinite(remaining) else None


def _retained_windows(quotas: Any) -> tuple[QuotaWindow, ...]:
    """Keep every object window, including ones the connection verdict ignores."""
    if not isinstance(quotas, dict):
        return ()
    kept = []
    for name in sorted(key for key in quotas if isinstance(key, str)):
        window = quotas[name]
        if not isinstance(window, dict):
            continue
        scope_row = window.get("claudeQuota")
        kind = scope_row.get("kind") if isinstance(scope_row, dict) else None
        scope = kind if isinstance(kind, str) else "unresolved"
        reset = _date(window.get("resetAt"))
        kept.append(
            QuotaWindow(
                name,
                scope,
                "unknown",
                _window_remaining(window),
                reset.isoformat() if reset is not None else None,
            )
        )
    return tuple(kept)


def _window_state(
    window: dict[str, Any], now: datetime
) -> Literal["unknown", "available", "exhausted"]:
    """The same global-window rules, applied to one window instead of the connection."""
    scope = window.get("claudeQuota")
    if not isinstance(scope, dict) or scope.get("kind") not in {"session", "weekly_all"}:
        return "unknown"
    reset = _date(window.get("resetAt"))
    if reset is not None and reset <= now:
        return "unknown"
    if window.get("unlimited") is True:
        return "available"
    if window.get("fractionReported") is False or reset is None:
        return "unknown"
    remaining = _window_remaining(window)
    if remaining is None:
        return "unknown"
    if remaining <= 0:
        if scope.get("active") is True and scope.get("severity") in {None, "critical"}:
            return "exhausted"
        return "unknown"
    return "available"


def _with_window_states(
    windows: tuple[QuotaWindow, ...], quotas: Mapping[str, Any], now: datetime
) -> tuple[QuotaWindow, ...]:
    return tuple(
        QuotaWindow(
            window.name,
            window.scope,
            _window_state(quotas[window.name], now)
            if isinstance(quotas.get(window.name), dict)
            else "unknown",
            window.remaining_percent,
            window.reset_at,
        )
        for window in windows
    )


def _quota_evidence(
    cache: dict[str, Any] | None, interval_minutes: Any, now: datetime
) -> tuple[
    Literal["unknown", "available", "exhausted"],
    str | None,
    datetime | None,
    tuple[QuotaWindow, ...],
]:
    if cache is None:
        return "unknown", "cache_missing", None, ()
    observed = _date(cache.get("fetchedAt"))
    windows = _retained_windows(cache.get("quotas"))
    interval = _number(interval_minutes)
    if interval is None or not math.isfinite(interval) or interval <= 0:
        return "unknown", "sync_interval_unknown", observed, windows
    if observed is None or observed > now:
        return "unknown", "observation_time_unknown", observed, windows
    age_minutes = (now - observed).total_seconds() / 60
    if age_minutes > QUOTA_MAX_SYNC_INTERVALS * interval:
        return "unknown", "observation_stale", observed, windows
    quotas = cache.get("quotas")
    if not isinstance(quotas, dict) or not quotas or cache.get("message"):
        return "unknown", "quota_unreported", observed, windows
    resolved = _with_window_states(windows, quotas, now)
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
        remaining = _window_remaining(window)
        if remaining is None:
            unknown = True
        elif remaining <= 0:
            # Claude current-limit percentages can be predictive display data.
            # Native preflight requires active critical metadata and a future reset.
            if (
                scope.get("active") is True
                and scope.get("severity") in {None, "critical"}
                and reset is not None
            ):
                return "exhausted", "global_window_exhausted", observed, resolved
            unknown = True
    if cache.get("modelQuotas"):
        unknown = True
    if unknown:
        return "unknown", "window_scope_or_value_unknown", observed, resolved
    return "available", None, observed, resolved


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


def _candidate_row_reasons(row: dict[str, Any]) -> list[str]:
    checks = [
        (row.get("reachable") is True, "unreachable"),
        (row.get("excluded") is False, "excluded"),
        (row.get("breakerState") == "CLOSED", "breaker_not_closed"),
        (row.get("connectionCooldown") is False, "connection_cooldown"),
        (row.get("modelLocked") is False, "model_locked"),
        (
            "freeAccessExclusion" in row and row["freeAccessExclusion"] is None,
            "free_access_excluded",
        ),
    ]
    return [reason for ok, reason in checks if not ok]


def _live_candidates(rows: list[Any]) -> dict[str, LiveCandidate]:
    grouped: dict[str, tuple[str, set[str], bool, set[str]]] = {}
    for row in _rows(rows):
        route_id = row.get("modelStr")
        if not isinstance(route_id, str):
            route_id = row.get("model")
        provider, connection_id = row.get("provider"), row.get("connectionId")
        if not (
            isinstance(route_id, str)
            and route_id
            and isinstance(provider, str)
            and isinstance(connection_id, str)
        ):
            continue
        reasons = _candidate_row_reasons(row)
        first_provider, connection_ids, reachable, blocked = grouped.get(
            route_id, (provider, set(), False, set())
        )
        connection_ids.add(connection_id)
        blocked.update(reasons)
        grouped[route_id] = (first_provider, connection_ids, reachable or not reasons, blocked)
    return {
        route_id: LiveCandidate(
            route_id, provider, tuple(sorted(connection_ids)), reachable, tuple(sorted(blocked))
        )
        for route_id, (provider, connection_ids, reachable, blocked) in grouped.items()
    }


def parse_omniroute_evidence(
    *,
    providers: Any,
    resilience: Any,
    limits: Any,
    cooldowns: Any,
    health_matrix: Any,
    v1_models: Any,
    candidates: Any = None,
    now: datetime,
) -> OmniRouteEvidence:
    """Parse audited API shapes; missing source payloads never grant availability.

    Sparse or null quota rows remain unknown, independently of connection health.
    Only fresh, reported, explicitly global blocking windows exclude connections.
    Model-scoped or unresolved windows remain the gateway's request-time concern.
    Catalog aliases are retained only when owned_by + root match exactly.
    A live candidate is reachable only when one of its rows is usable.
    """
    sources = {
        "providers": (providers, "connections", list),
        "resilience": (resilience, "connections", list),
        "limits": (limits, "caches", dict),
        "cooldowns": (cooldowns, "items", list),
        "health_matrix": (health_matrix, "providers", list),
        "v1_models": (v1_models, "data", list),
        "candidates": (candidates, "candidates", list),
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
        quota_state, quota_reason, observed, quota_windows = _quota_evidence(
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
                quota_windows,
            )
        )
    from verdict.orchestration.eligibility import _classify_capacity

    usable_ids = {c.connection_id for c in connections if c.usable}
    provider_rows = _rows(providers.get("connections")) if "providers" not in errors else []
    identities: dict[str, tuple[str, str]] = {}
    capacities: dict[str, str] = {}
    capacity_rules: dict[str, str] = {}
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
            classifications = [
                _classify_capacity(conn, row, route_id=route_id, openrouter_free_set=None)
                for conn in provider_rows
                if conn.get("id") in usable_ids and conn.get("provider") == provider
            ]
            classes = {item[0].value for item in classifications}
            rules = {item[2] for item in classifications}
            capacities[route_id] = next(iter(classes)) if len(classes) == 1 else "unknown"
            capacity_rules[route_id] = next(iter(rules)) if len(rules) == 1 else "unknown"
            routes.setdefault((provider, model), []).append(route_id)
            routes.setdefault((provider, route_id), []).append(route_id)
    cooled: set[str] = set()
    model_cooldowns = []
    for row in _rows(cooldowns.get("items")) if "cooldowns" not in errors else []:
        model, provider = row.get("model"), row.get("provider")
        if isinstance(model, str):
            cooled.add(model)
            matched = (
                tuple(sorted(routes.get((provider, model), [])))
                if isinstance(provider, str)
                else ()
            )
            if isinstance(provider, str):
                cooled.update(matched)
            remaining = _number(row.get("remainingMs"))
            model_cooldowns.append(
                {
                    "provider": provider if isinstance(provider, str) else None,
                    "model": model,
                    "remaining_ms": int(remaining) if remaining is not None else None,
                    "routes": matched,
                }
            )
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
        MappingProxyType(capacity_rules),
        MappingProxyType(
            _live_candidates(candidates["candidates"]) if "candidates" not in errors else {}
        ),
        tuple(model_cooldowns),
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
    management_token: str | None = None,
    timeout: float = 30.0,
    health_range: str = "7d",
    aggregate_deadline_seconds: float = _AGGREGATE_DEADLINE_SECONDS,
    decoded_byte_budget: int = _DECODED_BYTE_BUDGET,
    raw_row_limit: int = _RAW_ROW_LIMIT,
) -> OmniRouteEvidence:
    """GET the seven evidence sources, isolating failures by source name only.

    Each read is bounded by the S04 discovery bounds (per-read timeout,
    aggregate deadline, cumulative decoded-byte budget, raw-row cap). A
    source exceeding any bound is recorded as a source error, never served
    partial.
    """
    inference_key = api_key if api_key is not None else resolve_api_key()
    management_key = management_token.strip() if management_token is not None else None
    paths = {
        "providers": "/api/providers",
        "resilience": "/api/resilience/connections",
        "limits": "/api/usage/provider-limits",
        "cooldowns": "/api/resilience/model-cooldowns",
        "health_matrix": "/api/providers/health-matrix?" + urlencode({"range": health_range}),
        "v1_models": "/v1/models",
        "candidates": "/v1/auto-combo/auto/candidates",
    }
    budget = _DecodedBudget(max(1, decoded_byte_budget))
    deadline = time.monotonic() + aggregate_deadline_seconds
    payloads: dict[str, Any] = {}
    for name, path in paths.items():
        try:
            inference = name in _INFERENCE_SOURCES
            if inference and not inference_key:
                payloads[name] = None
                continue
            if not inference and not management_key and not inference_key:
                payloads[name] = None
                continue
            credential = inference_key if inference else (management_key or inference_key)
            payloads[name] = _request_json_bounded(
                base_url,
                name,
                path,
                credential,
                timeout,
                budget=budget,
                deadline=deadline,
                row_limit=raw_row_limit,
            )
        except (OSError, ValueError, _BoundedReadError):
            # Exception bodies and URLs can echo credentials. Retain only source names.
            payloads[name] = None
    return parse_omniroute_evidence(**payloads, now=datetime.now(timezone.utc))
