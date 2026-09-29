"""Prove-at-rest: the single prober that fills the health cache.

The daemon covers every admitted route in every capacity class. It spends
most of a cycle on FREE routes and only checks liveness for the other
classes. It writes ``health-cache.json`` (see
``verdict.orchestration.health_cache``) and nothing else.

It does **not** write ``~/.verdict/orchestration-health.json``. That file
belongs to the selection ladder. Story 3 is what teaches the ladder to read
this cache, so this change leaves selection order and ranking untouched.

Old state
---------
Releases before this one persisted a cycle document at
``~/.verdict/prove-at-rest/state.json`` (override ``VERDICT_PROVE_AT_REST_STATE``).
That document is a different schema (one cycle of free-intersect-active proof
results plus passports). This daemon **ignores** it. It neither reads nor
deletes it. ``load_healthy_passports`` still reads it, unchanged, for callers
that have not moved.

Cycle order
-----------
1. half-open negatives (a negative whose ``until`` has elapsed);
2. stale healthy entries;
3. never-probed FREE routes, round-robin by provider/pool;
4. SUBSCRIPTION, METERED and UNKNOWN, liveness (chat) only;
5. a small epsilon slice of cold providers (providers with no fresh entry).

Each cycle stops at ``max_requests`` (default 300) or ``max_wall_seconds``
(default 10 min), with concurrency 4. Every completed probe is persisted, so
a crash keeps partial progress. The cursor records which ordering pass the
cycle was in.

Two-step probe
--------------
Chat first: ``Reply with exactly: OK``. Then one required tool call. A route
is a coding worker only when the tool call succeeds (``tool_ok``). Other
capacity classes stop after the chat step.

Token buckets
-------------
One bucket per provider/pool, shared with real calls through
``HealthCache.consume``. A 429 zeroes that bucket until ``Retry-After``.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from verdict.orchestration.contracts import CapacityClass
from verdict.orchestration.eligibility import capacity_class_of
from verdict.orchestration.health_cache import (
    CATEGORY_AUTH,
    CATEGORY_CATALOG_STALE,
    CATEGORY_GONE,
    CATEGORY_MODEL_MISMATCH,
    CATEGORY_NOT_FOUND,
    CATEGORY_OK,
    CATEGORY_PAYMENT,
    CATEGORY_PERMISSION,
    CATEGORY_RATE_LIMITED,
    CATEGORY_TIMEOUT,
    CATEGORY_UPSTREAM,
    STATE_FRESH,
    STATE_NEGATIVE,
    STATE_STALE,
    STATE_UNPROBED,
    HealthCache,
    HealthCacheError,
    HealthEntry,
    ProbeResult,
    default_cache_path,
)

# The old cycle document stays readable. This module no longer writes it.
# ``ProveAtRestDaemon`` is the legacy free-intersect-active prover; the
# single prober above is ``Prober``. Both names are importable so existing
# callers keep working while the daemon command uses ``Prober``.
from verdict.prove_at_rest_legacy import (
    DEFAULT_INTERVAL_SECONDS,
    DEFAULT_PROBE_TIMEOUT_SECONDS,
    ENV_STATE_PATH,
    PROVE_AT_REST_SCHEMA_VERSION,
    STATUS_FAILED,
    STATUS_HEALTHY,
    STATUS_SKIPPED,
    ProofResult,
    ProveAtRestCycle,
    ProveAtRestDaemon,
    ProveAtRestError,
    ProveAtRestStore,
    default_state_path,
    load_healthy_passports,
    passport_from_probe,
)

CHAT_PROBE_MESSAGE = "Reply with exactly: OK"
TOOL_NAME = "verdict_probe_ping"
DEFAULT_MAX_REQUESTS = 300
DEFAULT_MAX_WALL_SECONDS = 600.0
DEFAULT_CONCURRENCY = 4
DEFAULT_EPSILON = 2
ENV_CACHE_PATH = "VERDICT_HEALTH_CACHE"

# Old on-disk document. Ignored, never migrated, never deleted.
LEGACY_STATE_RELATIVE = Path(".verdict") / "prove-at-rest" / "state.json"


class ProveError(HealthCacheError):
    """Raised when a prove-at-rest cycle cannot start."""


@dataclass(frozen=True)
class AdmittedRoute:
    """One admitted route the prober may visit. Built by the caller.

    ``capacity`` is the evidence-backed class (``free``, ``subscription``,
    ``metered``, ``unknown``). ``pool`` is an optional shared-quota pool.
    """

    route_id: str
    provider: str
    capacity: str
    pool: str | None = None
    capacity_evidence: str | None = None

    def __post_init__(self) -> None:
        if not self.route_id.strip() or not self.provider.strip():
            raise ProveError("route_id and provider must be non-empty")
        if self.capacity not in {item.value for item in CapacityClass}:
            raise ProveError(f"unknown capacity class: {self.capacity}")


@dataclass(frozen=True)
class ProbeExchange:
    """What the fake or live transport returns for one HTTP call."""

    http_status: int | None
    ok: bool
    chat_exact: bool = False
    tool_called: bool = False
    latency_ms: float | None = None
    retry_after_seconds: float | None = None
    error_category: str | None = None
    reported_model: str = ""


# A transport probes one route for one phase ("chat" or "tool") and must not
# touch the network unless the caller built a live transport.
ProbeTransportFn = Callable[[str, str, float], ProbeExchange]


@dataclass
class CycleStats:
    """Counters for one cycle. ``requests`` counts chat and tool calls."""

    requests: int = 0
    probed: int = 0
    fresh: int = 0
    negative: int = 0
    skipped_bucket: int = 0
    stopped_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "requests": self.requests,
            "probed": self.probed,
            "fresh": self.fresh,
            "negative": self.negative,
            "skipped_bucket": self.skipped_bucket,
            "stopped_reason": self.stopped_reason,
        }


def category_for(exchange: ProbeExchange) -> str:
    """Map a transport result onto a cache category."""
    status = exchange.http_status
    if exchange.ok:
        return CATEGORY_OK
    named = exchange.error_category or ""
    if status == 429 or named in {"rate_limited", "rate_limit"}:
        return CATEGORY_RATE_LIMITED
    if status == 401 or named in {"auth", "unauthorized", "authentication"}:
        return CATEGORY_AUTH
    if status == 402 or named in {"payment_required", "quota_exhausted"}:
        return CATEGORY_PAYMENT
    if status == 403 or named in {"forbidden", "permission"}:
        return CATEGORY_PERMISSION
    if status == 404 or named == "not_found":
        return CATEGORY_NOT_FOUND
    if status == 410 or named in {"gone", "http_410"}:
        return CATEGORY_GONE
    if named == "catalog_stale":
        return CATEGORY_CATALOG_STALE
    if status is None or named == "timeout":
        return CATEGORY_TIMEOUT
    if status >= 500 or named in {"upstream", "upstream_error"}:
        return CATEGORY_UPSTREAM
    return named or "http_error"


def chat_payload(route_id: str) -> dict[str, Any]:
    """First step: a chat completion that must answer OK. No tools."""
    return {
        "model": route_id,
        "messages": [{"role": "user", "content": CHAT_PROBE_MESSAGE}],
        "max_tokens": 8,
        "temperature": 0,
        "stream": False,
    }


def tool_payload(route_id: str) -> dict[str, Any]:
    """Second step: the model must call the single required tool."""
    return {
        "model": route_id,
        "messages": [
            {"role": "user", "content": f"Call the tool {TOOL_NAME} with argument value ok."}
        ],
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": TOOL_NAME,
                    "parameters": {
                        "type": "object",
                        "properties": {"value": {"type": "string"}},
                        "required": ["value"],
                    },
                },
            }
        ],
        "tool_choice": {"type": "function", "function": {"name": TOOL_NAME}},
        "max_tokens": 64,
        "temperature": 0,
        "stream": False,
    }


def _provider_of(route_id: str) -> str:
    if "/" in route_id:
        return route_id.split("/", 1)[0]
    return route_id or "unknown"


def _pool_of(route: AdmittedRoute) -> str | None:
    return route.pool


def order_cycle(
    routes: Sequence[AdmittedRoute],
    cache: HealthCache,
    now: datetime,
    *,
    epsilon: int = DEFAULT_EPSILON,
) -> list[tuple[AdmittedRoute, str]]:
    """Order one cycle. Each item is ``(route, kind)``.

    ``kind`` is ``full`` (chat then tool) or ``liveness`` (chat only).
    """
    by_id = {route.route_id: route for route in routes}
    half_open: list[AdmittedRoute] = []
    stale: list[AdmittedRoute] = []
    seen: set[str] = set()
    for route in routes:
        lookup = cache.lookup(route.route_id, now)
        entry = lookup.entry
        if entry is None:
            continue
        if lookup.state == STATE_NEGATIVE:
            continue
        if not entry.healthy and lookup.state == STATE_UNPROBED:
            # Negative whose ``until`` has elapsed: half-open.
            half_open.append(route)
            seen.add(route.route_id)
        elif entry.healthy and lookup.state == STATE_UNPROBED:
            # Usable window elapsed: probe again with the never-probed group.
            continue
        elif lookup.state == STATE_STALE:
            stale.append(route)
            seen.add(route.route_id)

    free_new = [
        route
        for route in routes
        if route.capacity == CapacityClass.FREE.value
        and route.route_id not in seen
        and cache.lookup(route.route_id, now).state == STATE_UNPROBED
    ]
    other_new = [
        route
        for route in routes
        if route.capacity != CapacityClass.FREE.value
        and route.route_id not in seen
        and cache.lookup(route.route_id, now).state == STATE_UNPROBED
    ]

    ordered: list[tuple[AdmittedRoute, str]] = []

    def _kind_for(route: AdmittedRoute) -> str:
        """Derive probe kind from capacity class (defect 3 fix for half-open/stale)."""
        if route.capacity in {
            CapacityClass.SUBSCRIPTION.value,
            CapacityClass.METERED.value,
            CapacityClass.UNKNOWN.value,
        }:
            return "liveness"
        return "full"

    ordered.extend((route, _kind_for(route)) for route in half_open)
    ordered.extend((route, _kind_for(route)) for route in stale)
    ordered.extend((route, "full") for route in _round_robin(free_new))
    ordered.extend((route, "liveness") for route in other_new)
    ordered.extend(
        _epsilon_slice(
            routes,
            cache,
            now,
            seen=set(item[0].route_id for item in ordered),
            epsilon=epsilon,
            by_id=by_id,
        )
    )
    return ordered


def _round_robin(routes: Sequence[AdmittedRoute]) -> list[AdmittedRoute]:
    """Spread never-probed FREE routes across provider/pool buckets."""
    groups: dict[str, list[AdmittedRoute]] = {}
    for route in routes:
        key = route.provider if not route.pool else f"{route.provider}/{route.pool}"
        groups.setdefault(key, []).append(route)
    queues = list(groups.values())
    if not queues:
        return []
    depth = max(len(queue) for queue in queues)
    out: list[AdmittedRoute] = []
    for index in range(depth):
        for queue in queues:
            if index < len(queue):
                out.append(queue[index])
    return out


def _epsilon_slice(
    routes: Sequence[AdmittedRoute],
    cache: HealthCache,
    now: datetime,
    *,
    seen: set[str],
    epsilon: int,
    by_id: Mapping[str, AdmittedRoute],
) -> list[tuple[AdmittedRoute, str]]:
    """One liveness probe for each provider that has no fresh entry.

    Capped at ``epsilon`` providers. A provider is cold when none of its
    cached entries is fresh. Routes already ordered in this cycle are skipped,
    so the slice never repeats work the earlier passes already cover.
    """
    fresh_providers: set[str] = set()
    for entry in cache.routes().values():
        if entry.state_at(now) == STATE_FRESH and entry.healthy:
            fresh_providers.add(_provider_of(entry.route_id))
    out: list[tuple[AdmittedRoute, str]] = []
    taken: set[str] = set()
    for route in routes:
        if len(out) >= max(0, epsilon):
            break
        if route.provider in fresh_providers or route.provider in taken:
            continue
        if route.route_id in seen:
            continue
        taken.add(route.provider)
        out.append((by_id[route.route_id], "liveness"))
    return out


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class Prober:
    """One cycle, or a loop of cycles, over an injected admitted-route source."""

    cache: HealthCache
    routes_loader: Callable[[], Sequence[AdmittedRoute]]
    transport: ProbeTransportFn
    max_requests: int = DEFAULT_MAX_REQUESTS
    max_wall_seconds: float = DEFAULT_MAX_WALL_SECONDS
    concurrency: int = DEFAULT_CONCURRENCY
    probe_timeout_seconds: float = 15.0
    epsilon: int = DEFAULT_EPSILON
    interval_seconds: float = DEFAULT_INTERVAL_SECONDS
    clock: Callable[[], datetime] = field(default=_now)
    monotonic: Callable[[], float] = field(default=time.monotonic)
    sleep: Callable[[float], None] = field(default=time.sleep)
    on_cycle_error: Callable[[Exception], None] | None = None
    _stop: threading.Event = field(default_factory=threading.Event, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.max_requests < 1:
            raise ProveError("max_requests must be >= 1")
        if self.max_wall_seconds <= 0:
            raise ProveError("max_wall_seconds must be positive")
        if self.concurrency < 1:
            raise ProveError("concurrency must be >= 1")
        if self.probe_timeout_seconds <= 0:
            raise ProveError("probe_timeout_seconds must be positive")

    def stop(self) -> None:
        self._stop.set()

    def run_once(self) -> CycleStats:
        """Probe until the request cap or the wall cap, persisting as it goes."""
        started = self.monotonic()
        now = self.clock()
        routes = list(self.routes_loader())
        ordered = order_cycle(routes, self.cache, now, epsilon=self.epsilon)
        stats = CycleStats()
        # Defect 2 fix: resume by route_id set, not by index.
        # If routes reorder or disappear between cycles the index-based cursor
        # would skip unprobed routes; a probed-ids set is order-independent.
        cursor = self.cache.cursor
        probed_ids: set[str] = set()
        if cursor.get("cycle_open"):
            raw_ids = cursor.get("probed_ids") or []
            if isinstance(raw_ids, list):
                probed_ids = set(str(rid) for rid in raw_ids if isinstance(rid, str))
        pending = [item for item in ordered if item[0].route_id not in probed_ids]
        batch_size = self.concurrency

        def over_budget(extra: int = 0) -> str:
            if stats.requests + extra > self.max_requests:
                return "request_cap"
            if self.monotonic() - started >= self.max_wall_seconds:
                return "wall_cap"
            return ""

        while pending:
            reason = over_budget()
            if reason:
                stats.stopped_reason = reason
                self._persist_cursor(probed_ids, open_cycle=True)
                self.cache.save()
                break
            batch = pending[:batch_size]
            pending = pending[batch_size:]
            self._persist_cursor(probed_ids, open_cycle=True)
            self.cache.save()
            newly_probed = self._run_batch(batch, stats, started)
            probed_ids.update(newly_probed)
            self._persist_cursor(probed_ids, open_cycle=True)
            self.cache.save()
            if stats.stopped_reason:
                # Cap fired inside the batch: do not drain remaining pending.
                break
        else:
            stats.stopped_reason = stats.stopped_reason or "complete"
            self.cache.clear_cursor()
            self.cache.save()
        return stats

    def _persist_cursor(self, probed_ids: set[str], *, open_cycle: bool) -> None:
        self.cache.set_cursor({"cycle_open": open_cycle, "probed_ids": sorted(probed_ids)})

    def _run_batch(
        self, batch: Sequence[tuple[AdmittedRoute, str]], stats: CycleStats, started: float
    ) -> set[str]:
        """Probe every route in *batch*; return the set of route_ids started.

        Returns only the route_ids actually dispatched.  When a cap fires
        mid-batch the remaining routes are excluded so the caller can update
        the probed-ids cursor correctly (defect 2 fix).
        """
        probed: set[str] = set()
        for route, kind in batch:
            if stats.requests >= self.max_requests:
                stats.stopped_reason = stats.stopped_reason or "request_cap"
                return probed
            if self.monotonic() - started >= self.max_wall_seconds:
                stats.stopped_reason = "wall_cap"
                return probed
            before = stats.requests
            completed = self._probe_route(route, kind, stats, started=started)
            if stats.requests != before:
                self.cache.save()
            if not completed:
                # The probe stopped part-way (a cap or bucket fired between the
                # chat and the tool call). The route was left unchanged, so it
                # stays pending and the next cycle resumes with it.
                return probed
            probed.add(route.route_id)
        return probed

    @staticmethod
    def _model_identity_matches(route_id: str, reported: str) -> bool:
        """True when *reported* is the expected model, tolerating provider-prefix.

        Rules (defect 4 fix):
        - Empty reported: not a mismatch (gateway chose not to echo).
        - Reported HAS a provider prefix: provider must equal the route's
          provider (exact match); suffix alone is insufficient.
        - Reported has NO provider prefix: suffix match is accepted (gateway
          stripped the prefix).
        """
        if not reported:
            return True
        # The gateway either echoes the full route id or strips exactly the
        # gateway provider segment ("cc/claude-x" -> "claude-x",
        # "nvidia/moonshotai/kimi-k3" -> "moonshotai/kimi-k3").  Anything else,
        # including a different provider with the same model suffix, is a
        # mismatch.
        route_suffix = route_id.split("/", 1)[1] if "/" in route_id else route_id
        return reported in (route_id, route_suffix)

    def _probe_route(
        self, route: AdmittedRoute, kind: str, stats: CycleStats, *, started: float
    ) -> bool:
        now = self.clock()
        if not self.cache.consume(route.provider, now, pool=route.pool):
            stats.skipped_bucket += 1
            return True  # no token: skipped this cycle, entry unchanged
        stats.requests += 1
        try:
            chat = self.transport(route.route_id, "chat", self.probe_timeout_seconds)
        except TimeoutError:
            chat = ProbeExchange(http_status=None, ok=False, error_category="timeout")
        self._note_rate_limit(route, chat, now)
        chat_ok = bool(chat.ok and chat.chat_exact and chat.http_status == 200)
        tool_ok = False
        latency = chat.latency_ms
        status = chat.http_status
        category = category_for(chat)
        observed = chat

        # Validate chat-phase model identity (defect 3 fix): check right after
        # chat so we do not make the tool call if the gateway returned the wrong
        # model.  An empty reported_model is treated as "matches" (provider
        # chose not to echo).
        if chat_ok and not self._model_identity_matches(route.route_id, chat.reported_model):
            category = CATEGORY_MODEL_MISMATCH
            status = chat.http_status
            chat_ok = False

        if chat_ok and kind == "full" and stats.requests >= self.max_requests:
            # Request cap hit between chat and tool: chat succeeded but tool was
            # never tested. Leave the route unchanged; do NOT record a negative
            # (defect 1 fix: request-cap path).
            stats.stopped_reason = stats.stopped_reason or "request_cap"
            return False
        if chat_ok and kind == "full":
            # Check wall deadline before making the second HTTP call (defect 5).
            if self.monotonic() - started >= self.max_wall_seconds:
                # Wall cap hit between chat and tool: chat succeeded but tool was
                # never tested. Leave the route unchanged (do not record a negative).
                stats.stopped_reason = "wall_cap"
                return False
            now_tool = self.clock()
            if self.cache.consume(route.provider, now_tool, pool=route.pool):
                stats.requests += 1
                try:
                    tool = self.transport(route.route_id, "tool", self.probe_timeout_seconds)
                except TimeoutError:
                    tool = ProbeExchange(http_status=None, ok=False, error_category="timeout")
                self._note_rate_limit(route, tool, now_tool)
                tool_ok = bool(tool.ok and tool.tool_called and tool.http_status == 200)
                if tool.latency_ms is not None:
                    latency = (latency or 0.0) + tool.latency_ms
                # Validate tool-phase model identity too.
                if tool_ok and not self._model_identity_matches(
                    route.route_id, tool.reported_model
                ):
                    tool_ok = False
                    category = CATEGORY_MODEL_MISMATCH
                    status = tool.http_status
                    observed = tool
                elif not tool_ok:
                    status = tool.http_status
                    category = category_for(tool)
                    observed = tool
            else:
                # No bucket token for the tool call: chat succeeded but tool was
                # never tested. Leave the route unchanged; do NOT record a
                # negative (defect 1 fix).
                stats.skipped_bucket += 1
                return True  # no tool token: entry unchanged, retried next cycle
        if chat_ok and (tool_ok or kind == "liveness"):
            category = CATEGORY_OK
            status = 200
        # Defect 5 fix: compute identity from the observed reported_model.
        # An empty reported_model means the gateway did not echo the id back;
        # record it as "not_reported" so status output can distinguish it from
        # a route whose identity was actively verified.
        _reported = observed.reported_model
        if not _reported:
            _identity = "not_reported"
        elif self._model_identity_matches(route.route_id, _reported):
            _identity = "verified"
        else:
            _identity = "mismatch"
        result = ProbeResult(
            category=category,
            chat_ok=chat_ok,
            tool_ok=tool_ok,
            latency_ms=latency,
            http_status=status,
            retry_after_seconds=observed.retry_after_seconds,
            pool=route.pool,
            capacity_evidence=route.capacity_evidence,
            identity=_identity,
        )
        # Liveness success is healthy for the cache state machine but not a
        # coding worker: tool_ok stays false. ``ProbeResult.healthy`` requires
        # tool_ok, so record it as a successful liveness entry directly.
        if chat_ok and kind == "liveness":
            self._record_liveness(route, result, now)
        else:
            self.cache.record(route.route_id, result, now)
        stats.probed += 1
        entry = self.cache.entry(route.route_id)
        if entry is not None and entry.healthy:
            stats.fresh += 1
        elif entry is not None and not entry.healthy:
            stats.negative += 1
        return True

    def _record_liveness(self, route: AdmittedRoute, result: ProbeResult, now: datetime) -> None:
        """Chat-only success: fresh for liveness, but not a coding worker."""
        self.cache.record_liveness(
            route.route_id,
            latency_ms=result.latency_ms,
            pool=route.pool,
            capacity_evidence=route.capacity_evidence,
            now=now,
            identity=result.identity,
        )

    def _note_rate_limit(
        self, route: AdmittedRoute, exchange: ProbeExchange, now: datetime
    ) -> None:
        if category_for(exchange) != CATEGORY_RATE_LIMITED:
            return
        seconds = exchange.retry_after_seconds if exchange.retry_after_seconds else 60.0
        self.cache.zero_bucket(route.provider, now + timedelta(seconds=seconds), pool=route.pool)

    def run_forever(self) -> CycleStats | None:
        """Loop ``run_once`` until ``stop``. A cycle error retries next interval."""
        self._stop.clear()
        last: CycleStats | None = None
        while not self._stop.is_set():
            try:
                last = self.run_once()
            except Exception as exc:
                if self.on_cycle_error is not None:
                    self.on_cycle_error(exc)
            remaining = float(self.interval_seconds)
            while remaining > 0 and not self._stop.is_set():
                step = min(0.25, remaining)
                self.sleep(step)
                remaining -= step
        return last


# ---------------------------------------------------------------------------
# Admitted-set loading (same evidence path as `verdict eligibility`)
# ---------------------------------------------------------------------------


def routes_from_evidence(
    inventory_rows: Sequence[Mapping[str, Any]],
    connections: Sequence[Mapping[str, Any]],
    *,
    now: datetime,
    state_dir: Path | None = None,
) -> tuple[AdmittedRoute, ...]:
    """Admit exactly the way ``verdict eligibility`` does, without probing.

    Uses ``admit`` over the live inventory and connections. Routes that fail
    admission are not probed. Capacity class comes from the same ladder
    classifier eligibility uses, so this prober and ``verdict eligibility``
    agree on FREE versus SUBSCRIPTION.
    """
    from verdict.admission import admit, default_runtime_evidence

    evidence = default_runtime_evidence(now=now, state_dir=state_dir)
    admitted = admit(inventory_rows, connections, evidence, now=now)
    by_id: dict[str, Mapping[str, Any]] = {str(row.get("id", "")): row for row in inventory_rows}
    conn_by_provider: dict[str, Mapping[str, Any]] = {}
    for item in connections:
        name = str(item.get("provider", "")).lower()
        if name and name not in conn_by_provider:
            conn_by_provider[name] = item
    out: list[AdmittedRoute] = []
    for record in admitted.records:
        if not record.admitted:
            continue
        row: Mapping[str, Any] = by_id.get(record.route_id, {})
        provider = record.provider or _provider_of(record.route_id)
        conn: Mapping[str, Any] | None = conn_by_provider.get(provider.lower())
        capacity, plan = capacity_class_of(conn, row)
        out.append(
            AdmittedRoute(
                route_id=record.route_id,
                provider=provider,
                capacity=capacity.value,
                pool=None,
                capacity_evidence=plan or None,
            )
        )
    return tuple(out)


def load_admitted_routes(
    gateway: str, *, api_key: str | None, now: datetime | None = None, timeout: float = 30.0
) -> tuple[AdmittedRoute, ...]:
    """Fetch inventory and connections, then admit. Same GETs as eligibility."""
    from verdict.orchestration.run import fetch_connections, fetch_inventory

    current = now or _now()
    origin = gateway.rstrip("/")
    if origin.endswith("/v1"):
        origin = origin[: -len("/v1")]
    rows = fetch_inventory(origin, api_key=api_key, timeout=timeout)
    connections = fetch_connections(origin, api_key=api_key, timeout=timeout)
    return routes_from_evidence(rows, connections, now=current)


# ---------------------------------------------------------------------------
# Live transport and daemon construction
# ---------------------------------------------------------------------------


def _exchange_from_body(
    status: int | None,
    body: Mapping[str, Any] | None,
    *,
    phase: str,
    latency_ms: float | None,
    retry_after: float | None,
) -> ProbeExchange:
    if not isinstance(body, Mapping):
        body = {}
    # Capture the reported model identity before deciding ok (defect 3 fix).
    reported_model: str = str(body.get("model", "")) if isinstance(body, Mapping) else ""
    choices = body.get("choices")
    message: Mapping[str, Any] = {}
    if isinstance(choices, list) and choices and isinstance(choices[0], Mapping):
        raw_message = choices[0].get("message")
        if isinstance(raw_message, Mapping):
            message = raw_message
    content = message.get("content")
    text = content.strip() if isinstance(content, str) else ""
    tool_calls = message.get("tool_calls")
    called = False
    if isinstance(tool_calls, list):
        for call in tool_calls:
            if not isinstance(call, Mapping):
                continue
            fn = call.get("function")
            name = fn.get("name") if isinstance(fn, Mapping) else None
            if name == TOOL_NAME:
                called = True
    ok = status == 200
    return ProbeExchange(
        http_status=status,
        ok=ok,
        chat_exact=(phase == "chat" and text == "OK"),
        tool_called=(phase == "tool" and called),
        latency_ms=latency_ms,
        retry_after_seconds=retry_after,
        error_category=None,
        reported_model=reported_model,
    )


def live_transport(base_url: str, *, api_key: str | None) -> ProbeTransportFn:
    """OpenAI-compatible transport. Built only for a consented live daemon."""
    import json
    import urllib.error
    import urllib.request

    endpoint = base_url.rstrip("/")
    if endpoint.endswith("/v1"):
        endpoint = endpoint[: -len("/v1")]
    endpoint = endpoint + "/v1/chat/completions"

    def transport(route_id: str, phase: str, timeout_seconds: float) -> ProbeExchange:
        payload = chat_payload(route_id) if phase == "chat" else tool_payload(route_id)
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        request = urllib.request.Request(
            endpoint,
            data=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        started = time.monotonic()
        try:
            with urllib.request.urlopen(request, timeout=timeout_seconds) as response:  # nosec B310
                raw = response.read(1_048_576)
                elapsed = (time.monotonic() - started) * 1000.0
                parsed = json.loads(raw) if raw else {}
                return _exchange_from_body(
                    response.status,
                    parsed if isinstance(parsed, Mapping) else {},
                    phase=phase,
                    latency_ms=elapsed,
                    retry_after=None,
                )
        except urllib.error.HTTPError as exc:
            elapsed = (time.monotonic() - started) * 1000.0
            retry_after = None
            header = exc.headers.get("Retry-After") if exc.headers else None
            if header:
                try:
                    retry_after = float(header)
                except ValueError:
                    retry_after = None
            return ProbeExchange(
                http_status=exc.code,
                ok=False,
                latency_ms=elapsed,
                retry_after_seconds=retry_after,
                error_category=category_for(ProbeExchange(http_status=exc.code, ok=False)),
            )
        except TimeoutError:
            return ProbeExchange(http_status=None, ok=False, error_category="timeout")

    return transport


@dataclass
class ProverDaemon:
    """Long-running prober wired to a gateway. Writes only the health cache."""

    prober: Prober
    consented: bool

    def run_once(self) -> CycleStats:
        if not self.consented:
            raise ProveError(
                "live prove-at-rest requires explicit consent; pass --allow-live-probe"
            )
        return self.prober.run_once()

    def run_forever(self) -> CycleStats | None:
        if not self.consented:
            raise ProveError(
                "live prove-at-rest requires explicit consent; pass --allow-live-probe"
            )
        return self.prober.run_forever()

    def stop(self) -> None:
        self.prober.stop()


def build_live_daemon(
    *,
    state_path: Path | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
    interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
    probe_timeout_seconds: float = DEFAULT_PROBE_TIMEOUT_SECONDS,
    allow_live_probe: bool = False,
    max_requests: int = DEFAULT_MAX_REQUESTS,
    max_wall_seconds: float = DEFAULT_MAX_WALL_SECONDS,
    concurrency: int = DEFAULT_CONCURRENCY,
    transport: ProbeTransportFn | None = None,
    cache_path: Path | None = None,
) -> ProverDaemon:
    """Construct the daemon. ``state_path`` is accepted and ignored.

    The legacy cycle file (``state_path`` / ``VERDICT_PROVE_AT_REST_STATE``)
    is not read and not written. The daemon writes ``cache_path`` or
    ``VERDICT_HEALTH_CACHE`` or ``~/.verdict/health-cache.json``.
    """
    del state_path  # legacy document: ignored, see module docstring
    from verdict.free_tier_admit import normalize_omniroute_origin, omniroute_endpoint_from_env

    if base_url and base_url.strip():
        origin = normalize_omniroute_origin(base_url.strip())
        key = api_key
    else:
        found = omniroute_endpoint_from_env()
        if found is None:
            raise ProveError(
                "OmniRoute endpoint required; set OMNIROUTE_BASE_URL or pass --base-url"
            )
        origin = normalize_omniroute_origin(found[0])
        key = api_key if api_key is not None else found[1]

    def loader() -> tuple[AdmittedRoute, ...]:
        from verdict.orchestration.run import resolve_api_key

        resolved = key if key is not None else resolve_api_key()
        return load_admitted_routes(origin, api_key=resolved)

    cache = HealthCache(cache_path or default_cache_path())
    chosen = transport if transport is not None else live_transport(origin, api_key=key)
    prober = Prober(
        cache=cache,
        routes_loader=loader,
        transport=chosen,
        max_requests=max_requests,
        max_wall_seconds=max_wall_seconds,
        concurrency=concurrency,
        probe_timeout_seconds=probe_timeout_seconds,
        interval_seconds=interval_seconds,
    )
    return ProverDaemon(prober=prober, consented=allow_live_probe)


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------


def status_report(cache: HealthCache, *, now: datetime | None = None) -> dict[str, Any]:
    """Counts by state and class, top healthy coding workers, cold providers."""
    current = now or _now()
    by_state = {STATE_FRESH: 0, STATE_STALE: 0, STATE_NEGATIVE: 0, STATE_UNPROBED: 0}
    by_class: dict[str, int] = {}
    workers: list[HealthEntry] = []
    providers: dict[str, set[str]] = {}
    for entry in cache.routes().values():
        state = entry.state_at(current)
        by_state[state] = by_state.get(state, 0) + 1
        evidence = entry.capacity_evidence or "unknown"
        if state in {STATE_FRESH, STATE_STALE}:
            by_class[evidence] = by_class.get(evidence, 0) + 1
        provider = _provider_of(entry.route_id)
        providers.setdefault(provider, set()).add(state)
        if state in {STATE_FRESH, STATE_STALE} and entry.tool_ok:
            workers.append(entry)
    workers.sort(key=lambda item: (item.latency_ms is None, item.latency_ms or 0.0))
    cold = sorted(
        provider
        for provider, states in providers.items()
        if STATE_FRESH not in states and STATE_STALE not in states
    )
    # Providers that appear only as negatives, plus providers we were told
    # about through buckets but have no fresh route.
    return {
        "cache_path": str(cache.path),
        "schema_version": "1",
        "counts_by_state": by_state,
        "healthy_by_capacity_evidence": by_class,
        "top_healthy_coding_workers": [
            {
                "route_id": entry.route_id,
                "latency_ms": entry.latency_ms,
                "pool": entry.pool,
                "capacity_evidence": entry.capacity_evidence,
                "checked_at": entry.checked_at.isoformat().replace("+00:00", "Z"),
                # Defect 5 fix: surface identity so callers can filter out
                # routes whose identity was never verified.
                "identity": entry.identity or "unknown",
            }
            for entry in workers[:10]
        ],
        "cold_providers": cold,
        "legacy_state": "ignored",
    }


__all__ = [
    "CHAT_PROBE_MESSAGE",
    "DEFAULT_CONCURRENCY",
    "DEFAULT_EPSILON",
    "DEFAULT_INTERVAL_SECONDS",
    "DEFAULT_MAX_REQUESTS",
    "DEFAULT_MAX_WALL_SECONDS",
    "DEFAULT_PROBE_TIMEOUT_SECONDS",
    "ENV_CACHE_PATH",
    "ENV_STATE_PATH",
    "LEGACY_STATE_RELATIVE",
    "PROVE_AT_REST_SCHEMA_VERSION",
    "STATUS_FAILED",
    "STATUS_HEALTHY",
    "STATUS_SKIPPED",
    "TOOL_NAME",
    "AdmittedRoute",
    "CycleStats",
    "ProbeExchange",
    "Prober",
    "ProofResult",
    "ProveAtRestCycle",
    "ProveAtRestDaemon",
    "ProveAtRestError",
    "ProveAtRestStore",
    "ProveError",
    "ProverDaemon",
    "build_live_daemon",
    "category_for",
    "chat_payload",
    "default_state_path",
    "load_admitted_routes",
    "load_healthy_passports",
    "order_cycle",
    "passport_from_probe",
    "routes_from_evidence",
    "status_report",
    "tool_payload",
]
