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
- A wall-clock budget (``wall_budget_s``) limits total refresh time.  Routes
  that cannot be reached before the budget expires are marked not-probed with
  ``probe_skip_reason="wall_budget_exhausted"`` or
  ``tool_skip_reason="wall_budget_exhausted"`` rather than starting a probe
  with an inadequate timeout.
- A 429 response (typed as ``RATE_LIMITED`` from both probe and tool_probe)
  sets the pool's breaker to "open" for the remainder of the refresh, skipping
  further routes in that pool.
"""

from __future__ import annotations

import datetime
import json
import os
import threading
import time
from collections import defaultdict
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from verdict.discovery.model import ERROR, NO_KEY, RATE_LIMITED, UP, ProbeResult, Route
from verdict.discovery.probe import probe_one
from verdict.discovery.toolprobe import TOOL_NOT_PROBED, TOOL_OK, tool_probe
from verdict.paths import verdict_home

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

REGISTRY_TTL_S: int = 43200  # 12 hours
REGISTRY_SCHEMA = "verdict.discovery.registry/v1"

# Skip reasons where carry-forward applies: route was not reached this run.
# key_missing is excluded — it is fresh negative evidence (probe returned NO_KEY).
_CARRY_SKIP_REASONS: frozenset[str] = frozenset({"wall_budget_exhausted", "pool_breaker_open"})

_UTC = datetime.timezone.utc

# Agent-size context padding for the tool probe (BOD-299 / freescan D3 evidence)
AGENT_CONTEXT_TOKENS: int = 18_000

# Provider pool keys whose 429 opens the breaker for the full refresh run
# (stops further probes to that pool in this run)
BREAKER_OPEN = "open"
BREAKER_CLOSED = "closed"

# Minimum remaining wall budget to start a new probe.  Routes that arrive with
# less than this many seconds remaining are marked wall_budget_exhausted instead
# of being started with an inadequate timeout.
_MIN_PROBE_BUDGET_S: float = 5.0


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass
class RegistryEntry:
    """One probed route in the registry."""

    route_key: str  # provider/model (stable join key)
    capacity_class: str  # "free", "subscription", or "unknown" (passed in, not decided here)
    capacity_fact: str  # e.g. "plan_free", "free_tier_summary", "zero_price"
    base_url: str
    auth: str  # sanitized: "none", "env:NAME", or "literal" (not "literal:xxx")
    upstream: str | None  # gateway upstream provider, when known
    answered: bool  # probe returned HTTP 200 with usable content
    tool_ok: bool | None  # True=proved, False=measured fail, None=not probed (no measurement)
    tool_skip_reason: (
        str | None
    )  # e.g. "wall_budget_exhausted", "not_answered" (when tool_ok is None)
    served_model: str | None  # from x-omniroute-model / x-omniroute-provider headers
    latency_ms: int | None
    retry_after_s: float | None  # for 429 responses
    breaker_state: str  # BREAKER_CLOSED or BREAKER_OPEN
    checked_at: str  # ISO-8601 datetime
    probe_skip_reason: str | None = (
        None  # set when probe was not attempted; e.g. "pool_breaker_open"
    )

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
            tool_ok=None if d.get("tool_ok") is None else bool(d["tool_ok"]),
            tool_skip_reason=d.get("tool_skip_reason"),
            probe_skip_reason=d.get("probe_skip_reason"),
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
    #: Number of entries carried forward from the previous registry this run.
    #: Zero when the registry was written without a previous one (first run, or
    #: after a gap), or when every route was probed.
    carried: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "written_at": self.written_at,
            "ttl_s": self.ttl_s,
            "entries": [e.to_dict() for e in self.entries],
            "carried": self.carried,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> DiscoveryRegistry:
        return cls(
            schema=str(d.get("schema", REGISTRY_SCHEMA)),
            written_at=str(d["written_at"]),
            ttl_s=int(d.get("ttl_s", REGISTRY_TTL_S)),
            entries=[RegistryEntry.from_dict(e) for e in d.get("entries", [])],
            carried=int(d.get("carried", 0)),
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
    except (OSError, PermissionError, UnicodeDecodeError):
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

    # Check TTL against wall clock.  Naive timestamps (no tzinfo) are treated as
    # stale: a naive written_at would cause "aware - naive" TypeError and cannot
    # be compared safely to the current UTC time.
    try:
        written = datetime.datetime.fromisoformat(reg.written_at)
        if written.tzinfo is None:
            return None  # naive timestamp — treat as stale
        age_s = (datetime.datetime.now(datetime.timezone.utc) - written).total_seconds()
        if age_s > reg.ttl_s:
            return None  # stale
    except (ValueError, OverflowError, TypeError):
        return None

    return reg


_GATEWAY_PREFIX = "omniroute/"


def _canonical_route_key(route_id: str) -> str:
    """Strip the gateway prefix (``omniroute/``) from a route id.

    Registry ``route_key`` values never carry this prefix; ladder route ids
    sometimes do.  Stripping ensures the lookup finds the right entry.
    """
    text = str(route_id or "").strip()
    if text.lower().startswith(_GATEWAY_PREFIX):
        return text[len(_GATEWAY_PREFIX) :]
    return text


def tool_ok_proof(
    route_id: str, registry: DiscoveryRegistry | None, now: datetime.datetime
) -> dict[str, Any]:
    """Check whether the registry holds a fresh, live ``tool_ok=True`` proof.

    Returns ``{"qualified": True, "source": "discovery_registry", ...}`` only
    when ALL of the following hold:

    - *registry* is not None (absent or stale registries are already filtered
      by :func:`load_registry`).
    - An entry for the canonical route key exists.
    - ``capacity_class == "free"`` — subscription routes never prove free.
    - ``answered is True`` — a live HTTP probe returned usable content.
    - ``tool_ok is True`` — the tool call was measured and passed.
    - ``served_model`` is not None and matches the model segment of the route
      key — ``None`` means the gateway did not confirm the served model, so
      the proof cannot be tied to a specific model identity.
    - ``entry.checked_at`` is tz-aware, non-future, and within the registry TTL.

    Any failed check returns ``{"qualified": False, "reason": "<code>", ...}``.
    Exceptions inside the check return ``qualified=False`` rather than
    propagating — a buggy entry never admits, it just falls back to the
    normal ``no_agentic_probe`` path.

    This function is **pure** (no I/O) and may be called per-route in a
    hot path.  Pass a pre-loaded *registry* from :func:`load_registry`; do
    not call it inside this function.
    """
    if registry is None:
        return {"qualified": False, "reason": "registry_absent", "entry": None}

    canonical = _canonical_route_key(route_id)

    entry: RegistryEntry | None = None
    for e in registry.entries:
        if e.route_key == canonical:
            entry = e
            break

    if entry is None:
        return {"qualified": False, "reason": "not_in_registry", "entry": None}

    # U1: FREE and UNKNOWN capacity both qualify via registry proof.
    # Subscription, metered, and any other class are never promoted.
    if entry.capacity_class not in {"free", "unknown"}:
        return {"qualified": False, "reason": "not_free_capacity", "entry": entry}

    # Must have been answered by a live HTTP probe.
    if not entry.answered:
        return {"qualified": False, "reason": "not_answered", "entry": entry}

    # tool_ok is tri-state: None = not measured, False = measured fail.
    if entry.tool_ok is None:
        return {"qualified": False, "reason": "not_probed", "entry": entry}
    if not entry.tool_ok:
        return {"qualified": False, "reason": "measured_fail", "entry": entry}

    # served_model must be present and match the route's model segment.
    # None means the gateway did not echo back the model identity; that
    # is insufficient to bind the proof to this route.
    if entry.served_model is None:
        return {"qualified": False, "reason": "served_model_absent", "entry": entry}
    model_segment = canonical.split("/", 1)[1] if "/" in canonical else canonical
    if entry.served_model != model_segment:
        return {"qualified": False, "reason": "served_model_mismatch", "entry": entry}

    # Entry-level freshness: checked_at must be tz-aware, non-future, and
    # within the registry TTL.  (Registry-level TTL was checked by
    # load_registry, but individual entries may have been probed earlier.)
    # Cap against the code constant so a file that declares an enormous
    # ttl_s cannot extend freshness beyond what the operator deployed.
    import math as _math

    _file_ttl = registry.ttl_s
    if not isinstance(_file_ttl, (int, float)) or not _math.isfinite(_file_ttl) or _file_ttl <= 0:
        _file_ttl = REGISTRY_TTL_S
    effective_ttl = min(_file_ttl, REGISTRY_TTL_S)
    try:
        checked = datetime.datetime.fromisoformat(entry.checked_at)
        if checked.tzinfo is None:
            return {"qualified": False, "reason": "naive_timestamp", "entry": entry}
        age_s = (now - checked).total_seconds()
        if age_s < 0:
            # Future timestamp — clock skew or test anomaly; not proof.
            return {"qualified": False, "reason": "future_timestamp", "entry": entry}
        if age_s > effective_ttl:
            return {"qualified": False, "reason": "entry_stale", "entry": entry}
    except (ValueError, OverflowError, TypeError):
        return {"qualified": False, "reason": "invalid_timestamp", "entry": entry}

    return {"qualified": True, "reason": None, "source": "discovery_registry", "entry": entry}


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


def _load_registry_raw(env: Mapping[str, str] | None = None) -> DiscoveryRegistry | None:
    """Load the registry for carry-forward *without* the registry-level TTL check.

    Returns the parsed registry even if it has passed its TTL.  Individual
    entry freshness is enforced by :func:`tool_ok_proof`'s per-entry
    ``checked_at`` comparison — a stale-at-file-level previous registry is still
    useful for rotation (oldest entries probe first) and carry-forward (entries
    that have not yet expired at the per-entry level are still valid proofs).

    Returns ``None`` only when the file is missing, unreadable, corrupt, or
    structurally invalid (not parseable as a :class:`DiscoveryRegistry`).
    """
    path = registry_path(env)
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, PermissionError, UnicodeDecodeError):
        return None
    try:
        d = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(d, dict) or "written_at" not in d:
        return None
    try:
        return DiscoveryRegistry.from_dict(d)
    except (KeyError, TypeError, ValueError):
        return None


def _checked_at_dt(entry: RegistryEntry) -> datetime.datetime:
    """Parse ``entry.checked_at`` to a timezone-aware datetime for sorting.

    Unparseable or timezone-naive timestamps fall back to :attr:`datetime.min`
    (UTC), which sorts before any real timestamp — effectively treating a
    corrupt entry as never-checked so it gets re-probed soonest.
    """
    _min = datetime.datetime.min.replace(tzinfo=_UTC)
    try:
        dt = datetime.datetime.fromisoformat(entry.checked_at)
        return dt if dt.tzinfo is not None else _min
    except (ValueError, OverflowError):
        return _min


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
    previous: DiscoveryRegistry | None = None,
) -> DiscoveryRegistry:
    """Probe routes and return a DiscoveryRegistry.

    ``routes`` is a list of ``(Route, capacity_class, capacity_fact)`` triples.
    Only FREE and SUBSCRIPTION routes should be passed in; this function never
    re-classifies and never probes unknown or priced routes.

    ``dry_run=True`` returns a registry with entries pre-populated as
    unanswered (no probes made).  Useful for ``--dry-run`` output.

    ``wall_budget_s`` caps total wall time; routes not reached by the deadline
    appear in the registry as unanswered with ``detail="budget_exhausted"``.

    ``previous`` is the registry from the last run, used for two purposes:

    1. **Rotation** — routes are sorted within each pool by their
       ``checked_at`` in *previous*: never-checked first, then oldest first.
       Routes late in the catalog that were previously skipped move to the
       front of subsequent runs.

    2. **Carry-forward** — when a liveness probe is not attempted this run
       (``wall_budget_exhausted`` or ``pool_breaker_open``), the entry from
       *previous* is kept verbatim — including its original ``checked_at`` —
       so :func:`tool_ok_proof`'s per-entry freshness window (bounded by
       ``REGISTRY_TTL_S``) continues to tick down correctly.  Measured
       failures (``tool_ok=False``) are carried too; only ``key_missing``
       skips are written fresh (the missing key is new evidence).

       Entries in *previous* for routes that are no longer in the catalog or
       are no longer classified FREE in this run's snapshot are dropped.
    """
    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
    breaker = _Breaker()
    deadline = time.monotonic() + wall_budget_s
    entries: list[RegistryEntry] = []

    # Build lookup for carry-forward and rotation.
    previous_by_key: dict[str, RegistryEntry] = (
        {e.route_key: e for e in previous.entries} if previous is not None else {}
    )

    # Rotate: sort routes by checked_at from previous within each pool.
    # Never-checked routes sort before any previously-checked route so they are
    # probed first; among previously-checked, the oldest checked_at goes first.
    # Python's sort is stable, so ties (same checked_at or same never-checked)
    # preserve the original catalog order within that group.
    if previous_by_key:

        def _sort_key(item: tuple[Route, str, str]) -> tuple[int, datetime.datetime]:
            prev = previous_by_key.get(item[0].key)
            if prev is None:
                return (0, datetime.datetime.min.replace(tzinfo=_UTC))
            return (1, _checked_at_dt(prev))

        routes = sorted(routes, key=_sort_key)

    if dry_run:
        for route, cap_class, cap_fact in routes:
            entries.append(_make_entry(route, cap_class, cap_fact, None, None, now_iso, breaker))
        return DiscoveryRegistry(
            schema=REGISTRY_SCHEMA, written_at=now_iso, ttl_s=REGISTRY_TTL_S, entries=entries
        )

    # Group routes by pool key.  Dedupe by route.key across pools: keep only
    # the first occurrence so that two routes sharing a key (same provider/model,
    # different base_url or source) never cause one to inherit the other's proof.
    pool_groups: dict[str, list[tuple[Route, str, str]]] = defaultdict(list)
    for item in routes:
        pool_groups[breaker.pool_key(item[0])].append(item)

    seen_keys: set[str] = set()
    active_pool_items: dict[str, list[tuple[Route, str, str]]] = {}

    for pool_key, pool_routes in pool_groups.items():
        for item in pool_routes:
            route = item[0]
            if route.key not in seen_keys:
                seen_keys.add(route.key)
                active_pool_items.setdefault(pool_key, []).append(item)
            # else: duplicate key — silently dropped

    if not active_pool_items:
        reg = DiscoveryRegistry(
            schema=REGISTRY_SCHEMA, written_at=now_iso, ttl_s=REGISTRY_TTL_S, entries=[]
        )
        save_registry(reg, env)
        return reg

    # First stage: liveness probe.
    # Each pool's routes are probed one-at-a-time (serial within pool).  The
    # breaker is checked before every individual probe: a 429 on route[0] opens
    # the pool immediately so routes[1..n] are recorded as not_probed with
    # reason "pool_breaker_open" without being contacted.
    # Different pools run in parallel across a small thread pool.
    _breaker_lock = threading.Lock()
    _result_lock = threading.Lock()
    # U1: routes whose probe returned a non-zero cost are stored here so the
    # tool-probe stage skips them (a charged liveness probe is proof enough).
    _metered_lock = threading.Lock()
    _metered_new: set[str] = set()  # canonical keys (no omniroute/ prefix)
    probe_items_out: list[tuple[Route, str, str]] = []
    probe_results_out: list[ProbeResult] = []
    # Each entry is (item, skip_reason); skip_reason is "pool_breaker_open" or
    # "wall_budget_exhausted" depending on why the liveness probe was skipped.
    mid_pool_live_skipped: list[tuple[tuple[Route, str, str], str]] = []

    def _probe_pool_serial(pool_items: list[tuple[Route, str, str]]) -> None:
        local_probed: list[tuple[tuple[Route, str, str], ProbeResult]] = []
        local_skipped: list[tuple[tuple[Route, str, str], str]] = []
        for item in pool_items:
            route = item[0]
            with _breaker_lock:
                is_open = breaker.is_open(route)
            if is_open:
                local_skipped.append((item, "pool_breaker_open"))
            else:
                remaining = deadline - time.monotonic()
                if remaining < _MIN_PROBE_BUDGET_S:
                    # Not enough time for a useful probe; skip rather than
                    # starting with an inadequate timeout.
                    local_skipped.append((item, "wall_budget_exhausted"))
                else:
                    try:
                        r = probe_one(route, timeout=min(probe_timeout, remaining))
                    except Exception as exc:
                        r = ProbeResult(route=route, status=ERROR, detail=str(exc)[:200])
                    if r.status == NO_KEY:
                        # Key was never set — this is a configuration gap, not a
                        # network failure.  Record as skipped so probe_skip_reason
                        # is set and tool_ok stays None.
                        local_skipped.append((item, "key_missing"))
                    else:
                        if r.status == RATE_LIMITED:
                            with _breaker_lock:
                                breaker.open_pool(route)
                        # U1: a non-zero cost on an UP probe proves the route
                        # charges per-request — persist a METERED override.
                        if r.status == UP and r.observed_cost is not None and r.observed_cost > 0:
                            _rk = route.key
                            if _rk.startswith(_GATEWAY_PREFIX):
                                _rk = _rk[len(_GATEWAY_PREFIX) :]
                            try:
                                from verdict.discovery.metered import (
                                    add_metered_override as _add_metered,
                                )

                                _add_metered(_rk, r.observed_cost, "discovery_observed_charge", env)
                            except Exception:
                                pass
                            with _metered_lock:
                                _metered_new.add(_rk)
                        local_probed.append((item, r))
        with _result_lock:
            for item, r in local_probed:
                probe_items_out.append(item)
                probe_results_out.append(r)
            mid_pool_live_skipped.extend(local_skipped)

    pools = list(active_pool_items.values())
    if pools:
        with ThreadPoolExecutor(max_workers=min(len(pools), 32)) as executor:
            list(executor.map(_probe_pool_serial, pools))

    # Second stage: tool probe at agent size for UP routes.
    # Routes are grouped by pool and processed serially within each pool so the
    # breaker can fire mid-pool here too.  Budget is checked before each probe.
    up_results = [r for r in probe_results_out if r.status == UP]
    up_pool_groups: dict[str, list[ProbeResult]] = defaultdict(list)
    for r in up_results:
        up_pool_groups[breaker.pool_key(r.route)].append(r)

    remaining_budget = deadline - time.monotonic()
    tool_results: dict[str, tuple[str, str | None, int | None, str | None]] = {}

    if remaining_budget <= 0:
        # Budget spent before tool stage — mark all UP routes as not_probed.
        for r in up_results:
            tool_results[r.route.key] = (TOOL_NOT_PROBED, "wall_budget_exhausted", None, None)
    else:
        # Each pool's routes are probed one-at-a-time (serial within pool) so the
        # breaker can fire mid-pool.  Different pools run concurrently.
        _tool_result_lock = threading.Lock()

        def _tool_probe_pool_serial(pool_results: list[ProbeResult]) -> None:
            local: list[tuple[str, tuple[str, str | None, int | None, str | None]]] = []
            for r in pool_results:
                # U1: skip tool probe for routes proven to charge per-request.
                _rk_canonical = r.route.key
                if _rk_canonical.startswith(_GATEWAY_PREFIX):
                    _rk_canonical = _rk_canonical[len(_GATEWAY_PREFIX) :]
                if _rk_canonical in _metered_new:
                    local.append((r.route.key, (TOOL_NOT_PROBED, "metered_override", None, None)))
                    continue
                with _breaker_lock:
                    is_open = breaker.is_open(r.route)
                if is_open:
                    local.append((r.route.key, (TOOL_NOT_PROBED, "pool_breaker_open", None, None)))
                elif time.monotonic() >= deadline:
                    local.append(
                        (r.route.key, (TOOL_NOT_PROBED, "wall_budget_exhausted", None, None))
                    )
                else:
                    remaining = deadline - time.monotonic()
                    if remaining < _MIN_PROBE_BUDGET_S:
                        # Not enough time for a useful probe; skip rather than
                        # starting with an inadequate timeout.
                        local.append(
                            (r.route.key, (TOOL_NOT_PROBED, "wall_budget_exhausted", None, None))
                        )
                    else:
                        t = tool_probe(
                            r.route,
                            timeout=min(probe_timeout, remaining),
                            context_tokens=context_tokens,
                        )
                        if t[0] == RATE_LIMITED:
                            with _breaker_lock:
                                breaker.open_pool(r.route)
                        local.append((r.route.key, t))
            with _tool_result_lock:
                for key, val in local:
                    tool_results[key] = val

        up_pool_list = list(up_pool_groups.values())
        if up_pool_list:
            with ThreadPoolExecutor(max_workers=min(len(up_pool_list), 32)) as executor:
                list(executor.map(_tool_probe_pool_serial, up_pool_list))

    # Build registry entries for all probed routes.
    # Use parallel iteration (zip) rather than a key-based dict so that proof
    # is always attached to the exact route it was measured on.
    for (route, cap_class, cap_fact), pr in zip(probe_items_out, probe_results_out, strict=True):
        tr = tool_results.get(route.key)
        entries.append(_make_entry(route, cap_class, cap_fact, pr, tr, now_iso, breaker))

    # Routes skipped mid-run: either the pool breaker fired or the wall budget
    # ran out before the liveness probe could be started.
    # For carry-eligible skip reasons, re-use the previous registry entry
    # verbatim (including its original checked_at) so that tool_ok_proof's
    # per-entry freshness window continues to tick down from the original
    # probe time rather than resetting to now.  Measured failures are carried
    # too — only key_missing skips are written fresh (new negative evidence).
    carried_count = 0
    for item, skip_reason in mid_pool_live_skipped:
        route, cap_class, cap_fact = item
        prev_entry = previous_by_key.get(route.key)
        if skip_reason in _CARRY_SKIP_REASONS and prev_entry is not None:
            entries.append(prev_entry)
            carried_count += 1
        else:
            entries.append(
                _make_entry(
                    route,
                    cap_class,
                    cap_fact,
                    None,
                    None,
                    now_iso,
                    breaker,
                    skip_reason=skip_reason,
                )
            )

    registry = DiscoveryRegistry(
        schema=REGISTRY_SCHEMA,
        written_at=now_iso,
        ttl_s=REGISTRY_TTL_S,
        entries=entries,
        carried=carried_count,
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
    *,
    skip_reason: str | None = None,
) -> RegistryEntry:
    """Build a RegistryEntry from probe and tool results.

    ``tool_ok`` is tri-state:
      - ``True``  — tool call was measured and passed.
      - ``False`` — tool call was measured and failed.
      - ``None``  — no measurement was made (see ``tool_skip_reason``).

    Callers must treat ``None`` as *not proved*, never as a failure.  Old
    registry files that lack the field are loaded as ``None`` by ``from_dict``.

    ``skip_reason`` overrides the default "not_answered" reason when ``tool``
    is ``None``; use ``"pool_breaker_open"`` for routes skipped due to a
    per-pool breaker trip.
    """
    answered = probe is not None and probe.status == UP
    retry_after_s: float | None = probe.retry_after_s if probe is not None else None
    latency_ms: int | None = None
    served_model: str | None = None
    probe_skip_reason: str | None = skip_reason if probe is None else None

    # Determine tri-state tool outcome.
    if tool is None:
        # Route was not UP (not answered), so tool stage was never attempted.
        tool_ok: bool | None = None
        tool_skip_reason: str | None = skip_reason or "not_answered"
    elif tool[0] == TOOL_NOT_PROBED:
        # Stage was skipped due to budget exhaustion (before or during the stage).
        tool_ok = None
        tool_skip_reason = tool[1] or "wall_budget_exhausted"
    elif tool[0] == TOOL_OK:
        tool_ok = True
        tool_skip_reason = None
    else:
        # Measured failure (no_tool_call, bad_tool_call, tool_error, rate_limited).
        tool_ok = False
        tool_skip_reason = None

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
        tool_skip_reason=tool_skip_reason,
        probe_skip_reason=probe_skip_reason,
        served_model=served_model,
        latency_ms=latency_ms,
        retry_after_s=retry_after_s,
        breaker_state=breaker.state(route),
        checked_at=checked_at,
    )
