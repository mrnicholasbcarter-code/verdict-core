"""verdict.discovery.registry — proof registry for probed free/subscription routes.

Stores the results of ``run_refresh`` in ``$VERDICT_HOME/discovery/registry.json``
with a 12-hour TTL.

Registry rules:
- A missing or stale (older than TTL) registry reads as ``None``; callers treat
  that as "unknown", never "healthy".
- Writes are atomic (write-tmp then os.replace) to prevent torn reads.
- Only routes classified FREE or SUBSCRIPTION *by a fact* are probed; the
  capacity class and deciding fact are stored in each entry and are not
  re-decided here.
- Per-provider rate quirks (OVH ~2/min, Kilo 200/hour, pollinations 1/45 s,
  llm7 burst limits) are respected via ``Route.max_parallel=1`` on each
  rate-limited route and a wall-clock budget on the refresh.
- A 429 response sets the pool's breaker to "open" for the remainder of the
  refresh, skipping further routes in that pool.
"""

from __future__ import annotations

import datetime
import json
import os
import time
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from verdict.discovery.model import RATE_LIMITED, UP, ProbeResult, Route
from verdict.discovery.probe import probe_all
from verdict.discovery.toolprobe import TOOL_OK, tool_probe_all
from verdict.paths import verdict_home

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

REGISTRY_TTL_S: int = 43200  # 12 hours
REGISTRY_SCHEMA = "verdict.discovery.registry/v1"

# Agent-size context padding for the tool probe (BOD-299 / freescan D3 evidence)
AGENT_CONTEXT_TOKENS: int = 18_000

# Provider pool keys whose 429 opens the breaker for the full refresh run
# (stops further probes to that pool in this run)
BREAKER_OPEN = "open"
BREAKER_CLOSED = "closed"


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass
class RegistryEntry:
    """One probed route in the registry."""

    route_key: str  # provider/model (stable join key)
    capacity_class: str  # "free" or "subscription" (passed in, not decided here)
    capacity_fact: str  # e.g. "plan_free", "free_tier_summary", "zero_price"
    base_url: str
    auth: str  # sanitized: "none", "env:NAME", or "literal" (not "literal:xxx")
    upstream: str | None  # gateway upstream provider, when known
    answered: bool  # probe returned HTTP 200 with usable content
    tool_ok: bool  # tool call at agent size was correct
    served_model: str | None  # from x-omniroute-model / x-omniroute-provider headers
    latency_ms: int | None
    retry_after_s: float | None  # for 429 responses
    breaker_state: str  # BREAKER_CLOSED or BREAKER_OPEN
    checked_at: str  # ISO-8601 datetime

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> RegistryEntry:
        return cls(
            route_key=str(d["route_key"]),
            capacity_class=str(d["capacity_class"]),
            capacity_fact=str(d["capacity_fact"]),
            base_url=str(d["base_url"]),
            auth=str(d["auth"]),
            upstream=d.get("upstream"),
            answered=bool(d.get("answered", False)),
            tool_ok=bool(d.get("tool_ok", False)),
            served_model=d.get("served_model"),
            latency_ms=d.get("latency_ms"),
            retry_after_s=d.get("retry_after_s"),
            breaker_state=str(d.get("breaker_state", BREAKER_CLOSED)),
            checked_at=str(d["checked_at"]),
        )


@dataclass
class DiscoveryRegistry:
    """Full contents of the registry file."""

    schema: str
    written_at: str  # ISO-8601
    ttl_s: int
    entries: list[RegistryEntry] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "written_at": self.written_at,
            "ttl_s": self.ttl_s,
            "entries": [e.to_dict() for e in self.entries],
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> DiscoveryRegistry:
        return cls(
            schema=str(d.get("schema", REGISTRY_SCHEMA)),
            written_at=str(d["written_at"]),
            ttl_s=int(d.get("ttl_s", REGISTRY_TTL_S)),
            entries=[RegistryEntry.from_dict(e) for e in d.get("entries", [])],
        )


# ---------------------------------------------------------------------------
# Registry path
# ---------------------------------------------------------------------------


def registry_path(env: Mapping[str, str] | None = None) -> Path:
    """``$VERDICT_HOME/discovery/registry.json``."""
    return verdict_home(env) / "discovery" / "registry.json"


# ---------------------------------------------------------------------------
# Load / save
# ---------------------------------------------------------------------------


def load_registry(env: Mapping[str, str] | None = None) -> DiscoveryRegistry | None:
    """Load the registry if it exists and is within its TTL.

    Returns ``None`` when the file is missing, unreadable, corrupt, or stale.
    A ``None`` result MUST be treated as unknown, never as healthy.
    """
    path = registry_path(env)
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, PermissionError):
        return None
    try:
        d = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(d, dict) or "written_at" not in d:
        return None
    try:
        reg = DiscoveryRegistry.from_dict(d)
    except (KeyError, TypeError, ValueError):
        return None

    # Check TTL against wall clock
    try:
        written = datetime.datetime.fromisoformat(reg.written_at)
        age_s = (datetime.datetime.now(datetime.timezone.utc) - written).total_seconds()
        if age_s > reg.ttl_s:
            return None  # stale
    except (ValueError, OverflowError):
        return None

    return reg


def save_registry(registry: DiscoveryRegistry, env: Mapping[str, str] | None = None) -> None:
    """Write registry atomically.  Creates ``$VERDICT_HOME/discovery/`` if needed."""
    path = registry_path(env)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps(registry.to_dict(), indent=2), encoding="utf-8")
        os.replace(tmp, path)
    finally:
        import contextlib

        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Per-pool breaker (tracks open pools during a refresh run)
# ---------------------------------------------------------------------------


class _Breaker:
    """In-memory per-pool breaker for one refresh run.

    A pool is identified by ``upstream`` (for OmniRoute routes) or
    ``provider`` (for direct routes).  When a 429 arrives the pool is opened
    for the rest of the run; no further routes in that pool are probed.
    """

    def __init__(self) -> None:
        self._open: set[str] = set()

    def pool_key(self, route: Route) -> str:
        return route.upstream or route.provider

    def is_open(self, route: Route) -> bool:
        return self.pool_key(route) in self._open

    def open_pool(self, route: Route) -> None:
        self._open.add(self.pool_key(route))

    def state(self, route: Route) -> str:
        return BREAKER_OPEN if self.is_open(route) else BREAKER_CLOSED


# ---------------------------------------------------------------------------
# Main refresh entry point
# ---------------------------------------------------------------------------


def run_refresh(
    routes: list[tuple[Route, str, str]],
    *,
    context_tokens: int = AGENT_CONTEXT_TOKENS,
    probe_timeout: float = 30.0,
    wall_budget_s: float = 300.0,
    env: Mapping[str, str] | None = None,
    dry_run: bool = False,
) -> DiscoveryRegistry:
    """Probe routes and return a DiscoveryRegistry.

    ``routes`` is a list of ``(Route, capacity_class, capacity_fact)`` triples.
    Only FREE and SUBSCRIPTION routes should be passed in; this function never
    re-classifies and never probes unknown or priced routes.

    ``dry_run=True`` returns a registry with entries pre-populated as
    unanswered (no probes made).  Useful for ``--dry-run`` output.

    ``wall_budget_s`` caps total wall time; routes not reached by the deadline
    appear in the registry as unanswered with ``detail="budget_exhausted"``.
    """
    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
    breaker = _Breaker()
    deadline = time.monotonic() + wall_budget_s
    entries: list[RegistryEntry] = []

    if dry_run:
        for route, cap_class, cap_fact in routes:
            entries.append(_make_entry(route, cap_class, cap_fact, None, None, now_iso, breaker))
        return DiscoveryRegistry(
            schema=REGISTRY_SCHEMA, written_at=now_iso, ttl_s=REGISTRY_TTL_S, entries=entries
        )

    # Group routes by pool key to respect per-pool sequencing
    # (rate-limited providers use max_parallel=1 on the Route, which the probe
    # fan-out respects via per-host semaphores).
    pool_groups: dict[str, list[tuple[Route, str, str]]] = defaultdict(list)
    for item in routes:
        pool_groups[breaker.pool_key(item[0])].append(item)

    # Build a flat probe list, skipping already-open pools
    probe_items: list[tuple[Route, str, str]] = []
    skipped: list[tuple[Route, str, str]] = []
    for pool_routes in pool_groups.values():
        for item in pool_routes:
            if breaker.is_open(item[0]):
                skipped.append(item)
            else:
                probe_items.append(item)

    if not probe_items:
        reg = DiscoveryRegistry(
            schema=REGISTRY_SCHEMA, written_at=now_iso, ttl_s=REGISTRY_TTL_S, entries=[]
        )
        save_registry(reg, env)
        return reg

    # Dedupe by route.key — keep first occurrence so that two routes sharing a
    # key (same provider/model, different base_url or source) never cause one to
    # inherit the other's proof.  Duplicates are silently dropped; they would
    # otherwise produce a conflated result_by_key dict entry.
    seen_keys: set[str] = set()
    deduped_items: list[tuple[Route, str, str]] = []
    for item in probe_items:
        if item[0].key not in seen_keys:
            seen_keys.add(item[0].key)
            deduped_items.append(item)
    probe_items = deduped_items

    just_routes = [r for r, _, _ in probe_items]

    # First stage: liveness probe
    remaining_budget = deadline - time.monotonic()
    probe_results: list[ProbeResult] = probe_all(
        just_routes,
        timeout=min(probe_timeout, max(5.0, remaining_budget / max(1, len(just_routes)))),
        concurrency=max(1, len(just_routes)),
        per_host=1,  # per-pool concurrency of 1 for rate-limited providers
    )

    # Open pools on 429
    for result in probe_results:
        if result.status == RATE_LIMITED:
            breaker.open_pool(result.route)

    # Second stage: tool probe at agent size for UP routes (via tool_probe_all)
    up_results = [r for r in probe_results if r.status == UP and not breaker.is_open(r.route)]
    remaining_budget = deadline - time.monotonic()
    tool_results: dict[str, tuple[str, str | None, int | None, str | None]] = tool_probe_all(
        up_results,
        timeout=min(probe_timeout, max(5.0, remaining_budget)),
        concurrency=1,  # per-pool rate respect: tool probes are heavy, serialise them
        per_host=1,
        context_tokens=context_tokens,
    )
    # Open breaker for any tool_error 429s
    for route_key, (t_status, t_detail, _, _) in tool_results.items():
        if t_status == "tool_error" and "429" in str(t_detail):
            matching = next((r.route for r in probe_results if r.route.key == route_key), None)
            if matching is not None:
                breaker.open_pool(matching)

    # Build registry entries for all probed routes.
    # Use parallel iteration (zip) rather than a key-based dict so that proof
    # is always attached to the exact route it was measured on; a dict would
    # allow two routes with the same key to inherit each other's result.
    for (route, cap_class, cap_fact), pr in zip(probe_items, probe_results, strict=True):
        tr = tool_results.get(route.key)
        entries.append(_make_entry(route, cap_class, cap_fact, pr, tr, now_iso, breaker))

    # Skipped (breaker open) routes
    for route, cap_class, cap_fact in skipped:
        entries.append(_make_entry(route, cap_class, cap_fact, None, None, now_iso, breaker))

    registry = DiscoveryRegistry(
        schema=REGISTRY_SCHEMA, written_at=now_iso, ttl_s=REGISTRY_TTL_S, entries=entries
    )
    save_registry(registry, env)
    return registry


def _sanitize_auth(auth: str) -> str:
    """Strip credential values from an auth string before storing."""
    if auth.startswith("literal:"):
        return "literal"
    return auth


def _make_entry(
    route: Route,
    cap_class: str,
    cap_fact: str,
    probe: ProbeResult | None,
    tool: tuple[str, str | None, int | None, str | None] | None,
    checked_at: str,
    breaker: _Breaker,
) -> RegistryEntry:
    """Build a RegistryEntry from probe and tool results."""
    answered = probe is not None and probe.status == UP
    tool_ok = tool is not None and tool[0] == TOOL_OK
    retry_after_s: float | None = probe.retry_after_s if probe is not None else None
    latency_ms: int | None = None
    served_model: str | None = None

    if probe is not None and probe.latency_ms is not None:
        latency_ms = probe.latency_ms
    if tool is not None and tool[2] is not None:
        # Use tool probe latency when available (more representative)
        latency_ms = tool[2]
    if probe is not None and probe.answered_model is not None:
        served_model = probe.answered_model
    if tool is not None and tool[3] is not None:
        served_model = tool[3]  # tool probe may carry a more specific served model

    return RegistryEntry(
        route_key=route.key,
        capacity_class=cap_class,
        capacity_fact=cap_fact,
        base_url=route.base_url,
        auth=_sanitize_auth(route.auth),
        upstream=route.upstream,
        answered=answered,
        tool_ok=tool_ok,
        served_model=served_model,
        latency_ms=latency_ms,
        retry_after_s=retry_after_s,
        breaker_state=breaker.state(route),
        checked_at=checked_at,
    )
