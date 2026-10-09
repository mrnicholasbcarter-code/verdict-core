"""Bounded verification refresh coordinator (BOD-292, unit 3).

One safe, bounded refresh path that keeps at-rest health current the moment a
consumer needs it, without probing thousands of routes or spending metered
quota without consent.

Operator decisions (2026-10-08) this module implements:

* health evidence is auto-refreshed the moment a consumer needs it (verified
  view open, Prime picker preview/apply for the involved ids, selection before
  dispatch);
* consumers WAIT for the bounded refresh (progress only, no provisional list)
  then render from reloaded evidence; fresh needed entries => zero calls and no
  wait;
* auto-refresh only probes prepaid FREE/SUBSCRIPTION routes; METERED/UNKNOWN
  need explicit digest-bound consent elsewhere; ``VERDICT_AUTO_REFRESH=0``
  disables auto-refresh;
* autocomplete never probes or waits (it never calls this module).

The coordinator is a single-flight owner: one nonblocking cross-process job
lock beside the configured health-cache plus an atomic in-progress marker. A
second trigger joins and waits on the in-flight job rather than queueing a
second sweep. Every cache write is serialized through
``HealthCache.merge_and_save`` so a daemon prober and a joined worker cannot
clobber each other.

The projection itself (unit 1's ``verdict/orchestration/verified_models.py``)
is pure and read-only. This module does not import it; it depends only on the
narrow :class:`VerifiedRow` structural protocol defined here so the coordinator
is testable with fixtures. Unit 2 integrates the real projection.
"""

from __future__ import annotations

import fcntl
import json
import os
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from verdict.orchestration.health_cache import (
    CATEGORY_AUTH,
    CATEGORY_PAYMENT,
    CATEGORY_PERMISSION,
    CATEGORY_RATE_LIMITED,
    HealthCache,
    ScopedCooldown,
    format_datetime,
    negative_seconds,
)
from verdict.prove_at_rest import AdmittedRoute, FullProbeOutcome, ProbeExchange, probe_full

# ---------------------------------------------------------------------------
# Narrow input contract from unit 1's pure projection (fixture-testable).
# ---------------------------------------------------------------------------

# Status strings produced by unit 1. Only STALE/UNVERIFIED (and eligible
# half-open UNVERIFIED) are refreshable candidates; the rest are never probed.
STATUS_VERIFIED = "VERIFIED"
STATUS_STALE = "STALE"
STATUS_FAILED = "FAILED"
STATUS_UNAVAILABLE = "UNAVAILABLE"
STATUS_UNVERIFIED = "UNVERIFIED"
STATUS_INVENTORY_ONLY = "INVENTORY_ONLY"
STATUS_EXCLUDED = "EXCLUDED"

# Capacity classes (mirror of contracts.CapacityClass values; kept local so the
# coordinator reads the projection's plain strings without a hard dependency).
CAPACITY_FREE = "free"
CAPACITY_SUBSCRIPTION = "subscription"
CAPACITY_METERED = "metered"
CAPACITY_UNKNOWN = "unknown"
_PREPAID = frozenset({CAPACITY_FREE, CAPACITY_SUBSCRIPTION})


@runtime_checkable
class VerifiedRow(Protocol):
    """The minimal projection-row shape the coordinator consumes.

    Unit 1 emits richer rows; the coordinator reads only these fields. Any
    object (dataclass, namedtuple, or a small fixture) exposing them works.
    """

    route_id: str
    provider: str
    status: str
    capacity_class: str
    refreshable: bool
    refresh_reason: str | None
    last_success_at: datetime | None
    rank_hint: int | None


@dataclass(frozen=True)
class RowInput:
    """Concrete :class:`VerifiedRow` for callers/tests that want a value type."""

    route_id: str
    provider: str
    status: str
    capacity_class: str
    refreshable: bool = False
    refresh_reason: str | None = None
    last_success_at: datetime | None = None
    rank_hint: int | None = None
    pool: str | None = None
    capacity_evidence: str | None = None


# ---------------------------------------------------------------------------
# Configuration + limits
# ---------------------------------------------------------------------------

DEFAULT_MAX_ROUTES = 20
DEFAULT_MAX_REQUESTS = 40
DEFAULT_WALL_SECONDS = 120.0
DEFAULT_CONCURRENCY = 4

HARD_MAX_ROUTES = 50
HARD_MAX_REQUESTS = 100
HARD_MAX_WALL_SECONDS = 120.0
HARD_MAX_CONCURRENCY = 4

DEFAULT_PAGE_SIZE = 50
DEBOUNCE_SECONDS = 10.0
REQUESTS_PER_FULL_PROBE = 2
PER_CALL_TIMEOUT_CAP = 15.0

ENV_AUTO_REFRESH = "VERDICT_AUTO_REFRESH"
ENV_MAX_ROUTES = "VERDICT_REFRESH_MAX_ROUTES"
ENV_MAX_REQUESTS = "VERDICT_REFRESH_MAX_REQUESTS"
ENV_WALL_SECONDS = "VERDICT_REFRESH_WALL_SECONDS"
ENV_CONCURRENCY = "VERDICT_REFRESH_CONCURRENCY"


class RefreshConfigError(ValueError):
    """Raised when refresh configuration is invalid (before any model call)."""


@dataclass(frozen=True)
class RefreshConfig:
    """Validated bounds for one refresh job.

    Defaults may be lowered or raised within the hard maxima. ``auto_refresh``
    gates automatic triggers only; an explicit manual plan/execute path builds
    its own config and does not consult ``VERDICT_AUTO_REFRESH``.
    """

    max_routes: int = DEFAULT_MAX_ROUTES
    max_requests: int = DEFAULT_MAX_REQUESTS
    wall_seconds: float = DEFAULT_WALL_SECONDS
    concurrency: int = DEFAULT_CONCURRENCY
    auto_refresh: bool = True
    page_size: int = DEFAULT_PAGE_SIZE
    debounce_seconds: float = DEBOUNCE_SECONDS

    def __post_init__(self) -> None:
        if not (1 <= self.max_routes <= HARD_MAX_ROUTES):
            raise RefreshConfigError(
                f"max_routes must be in 1..{HARD_MAX_ROUTES}, got {self.max_routes}"
            )
        if not (1 <= self.max_requests <= HARD_MAX_REQUESTS):
            raise RefreshConfigError(
                f"max_requests must be in 1..{HARD_MAX_REQUESTS}, got {self.max_requests}"
            )
        if not (0 < self.wall_seconds <= HARD_MAX_WALL_SECONDS):
            raise RefreshConfigError(
                f"wall_seconds must be in (0, {HARD_MAX_WALL_SECONDS}], got {self.wall_seconds}"
            )
        if not (1 <= self.concurrency <= HARD_MAX_CONCURRENCY):
            raise RefreshConfigError(
                f"concurrency must be in 1..{HARD_MAX_CONCURRENCY}, got {self.concurrency}"
            )
        if self.page_size < 1:
            raise RefreshConfigError("page_size must be >= 1")
        if self.debounce_seconds < 0:
            raise RefreshConfigError("debounce_seconds must be >= 0")


def _parse_int(env: Mapping[str, str], name: str, default: int, *, hard_max: int) -> int:
    raw = env.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw.strip())
    except ValueError as exc:
        raise RefreshConfigError(f"{name} must be an integer, got {raw!r}") from exc
    if not (1 <= value <= hard_max):
        raise RefreshConfigError(f"{name} must be in 1..{hard_max}, got {value}")
    return value


def _parse_float(env: Mapping[str, str], name: str, default: float, *, hard_max: float) -> float:
    raw = env.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw.strip())
    except ValueError as exc:
        raise RefreshConfigError(f"{name} must be a number, got {raw!r}") from exc
    if not (0 < value <= hard_max):
        raise RefreshConfigError(f"{name} must be in (0, {hard_max}], got {value}")
    return value


def config_from_env(env: Mapping[str, str] | None = None) -> RefreshConfig:
    """Build and validate a :class:`RefreshConfig` from the environment.

    Invalid values raise :class:`RefreshConfigError` BEFORE any call is made.
    """
    source = env if env is not None else os.environ
    auto_raw = source.get(ENV_AUTO_REFRESH, "1").strip().lower()
    auto = auto_raw not in {"0", "false", "no", "off"}
    return RefreshConfig(
        max_routes=_parse_int(source, ENV_MAX_ROUTES, DEFAULT_MAX_ROUTES, hard_max=HARD_MAX_ROUTES),
        max_requests=_parse_int(
            source, ENV_MAX_REQUESTS, DEFAULT_MAX_REQUESTS, hard_max=HARD_MAX_REQUESTS
        ),
        wall_seconds=_parse_float(
            source, ENV_WALL_SECONDS, DEFAULT_WALL_SECONDS, hard_max=HARD_MAX_WALL_SECONDS
        ),
        concurrency=_parse_int(
            source, ENV_CONCURRENCY, DEFAULT_CONCURRENCY, hard_max=HARD_MAX_CONCURRENCY
        ),
        auto_refresh=auto,
    )


# ---------------------------------------------------------------------------
# Snapshot + outcome types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RefreshSnapshot:
    """Everything the coordinator needs from a reloaded projection snapshot.

    ``rows`` are the projection rows (one per canonical route). ``now`` is the
    snapshot clock. ``generation`` is an opaque evidence-generation token used
    for debounce/marker matching. The coordinator reads rows read-only; it
    never mutates the snapshot.
    """

    rows: tuple[RowInput, ...]
    generation: str = ""

    def by_id(self) -> dict[str, RowInput]:
        return {row.route_id: row for row in self.rows}


# Trigger kinds, in priority order used when several rows compete for the cap.
TRIGGER_EXPLICIT = "explicit"  # picker / selection supplied exact ids
CONSUMER_VERIFIED_VIEW = "verified_view"
CONSUMER_PICKER = "picker"
CONSUMER_SELECTION = "selection"

# Outcome kinds.
OUTCOME_REUSED_FRESH = "reused_fresh"  # needed entries already fresh: zero calls, no wait
OUTCOME_AUTO_DISABLED = "auto_refresh_disabled"  # VERDICT_AUTO_REFRESH=0
OUTCOME_COMPLETED = "completed"  # job ran to completion within caps
OUTCOME_CAPPED = "capped"  # a cap fired; some rows left unrefreshed
OUTCOME_CANCELLED = "cancelled"  # shared cancel requested
OUTCOME_DEBOUNCED = "debounced"  # matching trigger within the debounce window
OUTCOME_JOINED = "joined"  # joined an in-flight job owned by another trigger
OUTCOME_NOTHING_ELIGIBLE = "nothing_eligible"  # no refreshable prepaid candidate

# Per-route refresh reasons for rows that were NOT refreshed this job.
REASON_ROUTE_CAP = "route_cap"
REASON_REQUEST_CAP = "request_cap"
REASON_WALL_CAP = "wall_cap"
REASON_BUCKET = "bucket"
REASON_JOINED_NOT_COVERED = "joined_job_not_covered"
REASON_PROVIDER_STOP = "provider_scope_stopped"
REASON_NOT_TESTED = "not_tested"
REASON_CANCELLED = "cancelled"


@dataclass(frozen=True)
class RouteOutcome:
    """What happened to one candidate route in this job."""

    route_id: str
    provider: str
    probed: bool
    verified: bool
    category: str | None = None
    http_status: int | None = None
    refresh_reason: str | None = None
    requests_made: int = 0


@dataclass(frozen=True)
class ProgressEvent:
    """One bounded progress tick fed to the consumer's UI thread.

    No worker mutates UI or cache directly; the coordinator emits these in
    monotonic ``sequence`` order so a consumer can render progress strictly
    BEFORE the final outcome.
    """

    sequence: int
    job_id: str
    probed: int
    total: int
    verified: int
    failed: int
    unavailable: int
    requests_made: int
    requests_reserved: int
    elapsed_seconds: float
    per_provider: Mapping[str, int]
    last_reason: str | None
    kind: str = "progress"  # "progress" | "final"


@dataclass(frozen=True)
class RefreshOutcome:
    """The result a consumer renders AFTER the bounded job ends.

    ``outcome`` is one of the ``OUTCOME_*`` constants. ``route_outcomes`` maps
    canonical route id to its :class:`RouteOutcome`. ``complete`` is False when
    a cap/cancel/busy left candidates untested (the consumer must not treat a
    stale row as fresh). ``requests_made`` never exceeds the request cap.
    """

    outcome: str
    job_id: str
    consumer: str
    probed: int
    verified: int
    failed: int
    unavailable: int
    requests_made: int
    elapsed_seconds: float
    route_outcomes: Mapping[str, RouteOutcome]
    cap_reason: str | None = None
    complete: bool = True
    note: str | None = None

    @property
    def made_calls(self) -> bool:
        return self.requests_made > 0


# ---------------------------------------------------------------------------
# Candidate selection (read-only; no selector.select)
# ---------------------------------------------------------------------------


def _is_refreshable(row: RowInput) -> bool:
    """A row is an automatic candidate only when prepaid and stale/unverified.

    Fresh (VERIFIED) rows never re-probe. METERED/UNKNOWN never auto-probe.
    FAILED/UNAVAILABLE/INVENTORY_ONLY/EXCLUDED are not candidates. The row's own
    ``refreshable`` flag (from unit 1, which already applied the active-gate
    checks) must also be set.
    """
    if row.capacity_class not in _PREPAID:
        return False
    if row.status not in {STATUS_STALE, STATUS_UNVERIFIED}:
        return False
    return bool(row.refreshable)


def _priority(row: RowInput) -> int:
    """Lower sorts first. Priority bands per design section 5.

    0: previously validated success now stale (STALE with a last_success_at)
    1: half-open negatives surfaced as UNVERIFIED with a success history
    2: never-verified admitted routes (UNVERIFIED, no success history)
    """
    if row.status == STATUS_STALE:
        return 0
    if row.last_success_at is not None:
        return 1
    return 2


def _rank_hint(row: RowInput) -> tuple[int, str]:
    """Canonical ladder rank hint; unknown rank (None) sorts last, then route id."""
    hint = row.rank_hint
    return (hint if hint is not None else 1_000_000_000, row.route_id)


def select_candidates(
    snapshot: RefreshSnapshot, *, needed_ids: Sequence[str], explicit: bool, config: RefreshConfig
) -> list[RowInput]:
    """Ordered, capped candidate list. Only supplied ids may enter the plan.

    Priority: explicit consumer ids (picker/selection) first, then previously
    validated success now stale, then half-open negatives, then never-verified
    admitted routes in canonical ladder rank order. Within each priority band,
    round-robin by provider with a stable route tie-break and unknown rank last.
    Inventory/capacity alone never makes a route eligible.
    """
    by_id = snapshot.by_id()
    wanted = [by_id[rid] for rid in dict.fromkeys(needed_ids) if rid in by_id]
    candidates = [row for row in wanted if _is_refreshable(row)]
    if explicit:
        # Explicit ids are their own top band; preserve priority then rank
        # inside it but keep all supplied refreshable ids ahead of nothing.
        banded: dict[int, list[RowInput]] = {}
        for row in candidates:
            banded.setdefault(_priority(row), []).append(row)
    else:
        banded = {}
        for row in candidates:
            banded.setdefault(_priority(row), []).append(row)
    ordered: list[RowInput] = []
    for band in sorted(banded):
        ordered.extend(_round_robin_by_provider(banded[band]))
    return ordered[: config.max_routes]


def _round_robin_by_provider(rows: Sequence[RowInput]) -> list[RowInput]:
    """Spread rows across providers; stable rank/route tie-break within each."""
    groups: dict[str, list[RowInput]] = {}
    for row in sorted(rows, key=_rank_hint):
        groups.setdefault(row.provider, []).append(row)
    queues = list(groups.values())
    if not queues:
        return []
    depth = max(len(q) for q in queues)
    out: list[RowInput] = []
    for i in range(depth):
        for q in queues:
            if i < len(q):
                out.append(q[i])
    return out


# ---------------------------------------------------------------------------
# Single-flight job lock + in-progress marker (cross-process)
# ---------------------------------------------------------------------------


class _JobLock:
    """A nonblocking cross-process exclusive lock beside the health-cache.

    ``acquire`` returns True when this process owns the job, False when another
    live owner holds it (the caller then JOINs via the marker). The lock file
    stays open for the whole job; closing it (or process death) releases it so
    a stale marker with no live lock is safely discarded.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._handle: Any | None = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            return False
        self._handle = handle
        return True

    def release(self) -> None:
        if self._handle is not None:
            try:
                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
            finally:
                self._handle.close()
                self._handle = None

    def live(self) -> bool:
        """True when some process currently holds the lock (we probe non-blockingly)."""
        if self._handle is not None:
            return True
        if not self.path.exists():
            return False
        probe = self.path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(probe.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return True  # someone else holds it
        else:
            fcntl.flock(probe.fileno(), fcntl.LOCK_UN)
            return False
        finally:
            probe.close()


@dataclass
class _Marker:
    """Atomic in-progress / result marker. Holds no credentials."""

    path: Path

    def write(self, payload: Mapping[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(tmp, self.path)

    def read(self) -> dict[str, Any] | None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return raw if isinstance(raw, dict) else None


# ---------------------------------------------------------------------------
# Coordinator
# ---------------------------------------------------------------------------


def _provider_token_key(row: RowInput) -> str:
    return row.provider if not row.pool else f"{row.provider}/{row.pool}"


def _admitted_route(row: RowInput) -> AdmittedRoute:
    capacity = row.capacity_class if row.capacity_class in _PREPAID else CAPACITY_FREE
    return AdmittedRoute(
        route_id=row.route_id,
        provider=row.provider,
        capacity=capacity,
        pool=row.pool,
        capacity_evidence=row.capacity_evidence,
    )


class _Scopes:
    """Tracks per-provider scope stops for the cycle."""

    def __init__(self) -> None:
        self._stopped: dict[str, str] = {}
        self._stopped_routes: dict[str, str] = {}

    def provider_stopped(self, provider: str) -> str | None:
        return self._stopped.get(provider)

    def route_stopped(self, route_id: str) -> str | None:
        return self._stopped_routes.get(route_id)

    def stop_provider(self, provider: str, category: str) -> None:
        self._stopped.setdefault(provider, category)

    def stop_route(self, route_id: str, category: str) -> None:
        self._stopped_routes.setdefault(route_id, category)


# Categories that stop a whole provider scope for the cycle.
_PROVIDER_STOP_CATEGORIES = frozenset(
    {CATEGORY_AUTH, CATEGORY_PAYMENT, CATEGORY_PERMISSION, CATEGORY_RATE_LIMITED}
)


@dataclass
class RefreshCoordinator:
    """The single owner that runs one bounded full-probe job.

    All I/O is injected so tests drive it deterministically: the health-cache
    (and its directory), a timeout-respecting ``transport``, a ``clock`` and
    ``monotonic`` source, and the lock/marker paths (defaulting beside the
    cache). ``refresh_for_consumer`` is the single public entry point.
    """

    cache: HealthCache
    transport: Callable[[str, str, float], ProbeExchange] | None = None
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(timezone.utc))
    monotonic: Callable[[], float] = field(default=time.monotonic)
    sleep: Callable[[float], None] = field(default=time.sleep)
    lock_path: Path | None = None
    marker_path: Path | None = None
    _seq: int = field(default=0, init=False, repr=False)

    def _paths(self) -> tuple[Path, Path]:
        base = self.cache.path.parent
        lock = self.lock_path or (base / "verified-refresh.lock")
        marker = self.marker_path or (base / "verified-refresh.json")
        return lock, marker

    # -- public entry point ------------------------------------------------

    def refresh_for_consumer(
        self,
        snapshot: RefreshSnapshot,
        *,
        consumer: str,
        needed_ids: Sequence[str],
        config: RefreshConfig,
        explicit: bool = False,
        on_progress: Callable[[ProgressEvent], None] | None = None,
        cancel: Callable[[], bool] | None = None,
        job_id: str | None = None,
    ) -> RefreshOutcome:
        """Refresh the needed prepaid stale/unverified rows, waiting for the job.

        Returns a :class:`RefreshOutcome` only after the bounded job ends.
        ``explicit`` marks picker/selection triggers whose ids are top priority.
        ``cancel`` lets the consumer request shared cancellation. ``on_progress``
        receives bounded progress ticks in monotonic order BEFORE the final
        result. Fresh-only needed entries bypass the lock/join entirely.
        """
        cancel = cancel or (lambda: False)
        by_id = snapshot.by_id()
        wanted = [by_id[rid] for rid in dict.fromkeys(needed_ids) if rid in by_id]

        # Disabled auto-refresh: snapshot view, labelled, no implicit consent.
        if not config.auto_refresh and not explicit:
            return self._empty(
                OUTCOME_AUTO_DISABLED, consumer, job_id or "disabled", wanted, REASON_NOT_TESTED
            )

        # The full eligible set (uncapped) lets us label rows cut by the route
        # cap with ``route_cap`` even though they never enter the probed plan.
        all_eligible = select_candidates(
            snapshot,
            needed_ids=needed_ids,
            explicit=explicit,
            config=replace(config, max_routes=HARD_MAX_ROUTES),
        )
        candidates = all_eligible[: config.max_routes]
        cut_by_route_cap = all_eligible[config.max_routes :]

        # Fresh-only bypass, checked BEFORE the lock/join: if no needed entry is
        # a refreshable candidate, return immediately with zero calls, no wait.
        if not candidates:
            return self._empty(
                OUTCOME_REUSED_FRESH if wanted else OUTCOME_NOTHING_ELIGIBLE,
                consumer,
                job_id or "fresh",
                wanted,
                None,
            )

        lock_path, marker_path = self._paths()
        lock = _JobLock(lock_path)
        marker = _Marker(marker_path)

        # Debounce: a matching page/id trigger completed within the window
        # returns the prior result with a debounce note (no calls). Picker/
        # selection still perform their exact fresh checks above.
        prior = marker.read()
        if prior is not None and self._debounce_match(prior, candidates, config, lock):
            return self._from_marker(prior, consumer, OUTCOME_DEBOUNCED, note="debounced")

        if lock.acquire():
            try:
                return self._run_owned_job(
                    candidates,
                    cut_by_route_cap=cut_by_route_cap,
                    consumer=consumer,
                    config=config,
                    marker=marker,
                    on_progress=on_progress,
                    cancel=cancel,
                    job_id=job_id or self._new_job_id(),
                )
            finally:
                lock.release()
        else:
            return self._join_and_wait(
                candidates,
                consumer=consumer,
                config=config,
                marker=marker,
                lock=lock,
                on_progress=on_progress,
                cancel=cancel,
            )

    # -- owned job ---------------------------------------------------------

    def _run_owned_job(
        self,
        candidates: list[RowInput],
        *,
        cut_by_route_cap: Sequence[RowInput] = (),
        consumer: str,
        config: RefreshConfig,
        marker: _Marker,
        on_progress: Callable[[ProgressEvent], None] | None,
        cancel: Callable[[], bool],
        job_id: str,
    ) -> RefreshOutcome:
        started = self.monotonic()
        deadline = started + config.wall_seconds
        total = len(candidates)
        outcomes: dict[str, RouteOutcome] = {}
        scopes = _Scopes()
        requests_made = 0
        verified = failed = unavailable = probed = 0
        per_provider: dict[str, int] = {}
        cap_reason: str | None = None
        cancelled = False
        self._seq = 0

        timeout_cap = min(PER_CALL_TIMEOUT_CAP, config.wall_seconds)

        self._write_marker(
            marker, job_id, consumer, candidates, started, deadline, outcomes, running=True
        )

        for row in candidates:
            if cancel():
                cancelled = True
                break
            remaining = deadline - self.monotonic()
            if remaining <= 0:
                cap_reason = REASON_WALL_CAP
                break
            if requests_made + REQUESTS_PER_FULL_PROBE > config.max_requests:
                cap_reason = REASON_REQUEST_CAP
                break
            stop = scopes.provider_stopped(row.provider)
            if stop is not None:
                outcomes[row.route_id] = RouteOutcome(
                    row.route_id,
                    row.provider,
                    False,
                    False,
                    category=stop,
                    refresh_reason=REASON_PROVIDER_STOP,
                )
                continue
            if scopes.route_stopped(row.route_id) is not None:
                outcomes[row.route_id] = RouteOutcome(
                    row.route_id, row.provider, False, False, refresh_reason=REASON_PROVIDER_STOP
                )
                continue
            # Per-provider token bucket: whole-probe (2-request) reservation.
            now = self.clock()
            if (
                self.cache.bucket_remaining(row.provider, now, pool=row.pool)
                < REQUESTS_PER_FULL_PROBE
            ):
                outcomes[row.route_id] = RouteOutcome(
                    row.route_id, row.provider, False, False, refresh_reason=REASON_BUCKET
                )
                continue
            self.cache.consume(row.provider, now, pool=row.pool, amount=REQUESTS_PER_FULL_PROBE)

            per_call_timeout = min(timeout_cap, max(0.001, deadline - self.monotonic()))
            outcome = self._probe_one(row, per_call_timeout, deadline=deadline, cancel=cancel)
            # Release the unused half of the reservation after a short probe.
            if outcome.requests_made < REQUESTS_PER_FULL_PROBE:
                self._release_tokens(row, REQUESTS_PER_FULL_PROBE - outcome.requests_made, now)
            requests_made += outcome.requests_made

            route_out = self._persist_and_classify(row, outcome, scopes)
            outcomes[row.route_id] = route_out
            if route_out.probed:
                probed += 1
                per_provider[row.provider] = per_provider.get(row.provider, 0) + 1
                if route_out.verified:
                    verified += 1
                elif route_out.category in _PROVIDER_STOP_CATEGORIES or (
                    route_out.http_status in {401, 402, 403, 429}
                ):
                    unavailable += 1
                else:
                    failed += 1
            elif outcome.no_write_reason in {"cancelled"}:
                cancelled = True

            self._emit(
                on_progress,
                job_id,
                probed,
                total,
                verified,
                failed,
                unavailable,
                requests_made,
                min(config.max_requests, (probed + 1) * REQUESTS_PER_FULL_PROBE),
                started,
                per_provider,
                route_out.category,
            )
            self._write_marker(
                marker, job_id, consumer, candidates, started, deadline, outcomes, running=True
            )
            if cancelled:
                break

        # Mark in-plan rows never reached by a cap/cancel.
        for row in candidates:
            if row.route_id not in outcomes:
                reason = cap_reason or (REASON_CANCELLED if cancelled else REASON_ROUTE_CAP)
                outcomes[row.route_id] = RouteOutcome(
                    row.route_id, row.provider, False, False, refresh_reason=reason
                )
        # Eligible rows cut by the route cap are reported but never probed.
        for row in cut_by_route_cap:
            if row.route_id not in outcomes:
                outcomes[row.route_id] = RouteOutcome(
                    row.route_id, row.provider, False, False, refresh_reason=REASON_ROUTE_CAP
                )

        elapsed = self.monotonic() - started
        route_capped = bool(cut_by_route_cap)
        if cancelled:
            result_kind = OUTCOME_CANCELLED
        elif cap_reason is not None:
            result_kind = OUTCOME_CAPPED
        elif route_capped:
            result_kind = OUTCOME_CAPPED
            cap_reason = REASON_ROUTE_CAP
        elif any(
            o.refresh_reason in {REASON_BUCKET, REASON_PROVIDER_STOP} for o in outcomes.values()
        ):
            result_kind = OUTCOME_CAPPED
            cap_reason = cap_reason or REASON_BUCKET
        else:
            result_kind = OUTCOME_COMPLETED
        complete = result_kind == OUTCOME_COMPLETED

        outcome_obj = RefreshOutcome(
            outcome=result_kind,
            job_id=job_id,
            consumer=consumer,
            probed=probed,
            verified=verified,
            failed=failed,
            unavailable=unavailable,
            requests_made=requests_made,
            elapsed_seconds=elapsed,
            route_outcomes=outcomes,
            cap_reason=cap_reason,
            complete=complete,
        )
        self._write_marker(
            marker,
            job_id,
            consumer,
            candidates,
            started,
            deadline,
            outcomes,
            running=False,
            finished_at=self._wall_now(),
            result=result_kind,
        )
        self._emit_final(
            on_progress,
            job_id,
            probed,
            total,
            verified,
            failed,
            unavailable,
            requests_made,
            started,
            per_provider,
        )
        return outcome_obj

    def _probe_one(
        self, row: RowInput, timeout_seconds: float, *, deadline: float, cancel: Callable[[], bool]
    ) -> FullProbeOutcome:
        if self.transport is None:
            # No transport => nothing can be probed; treat as not-tested.
            return FullProbeOutcome(row.route_id, None, 0, False, True, "not_tested", "not_tested")

        def deadline_ok() -> bool:
            return self.monotonic() < deadline

        return probe_full(
            _admitted_route(row),
            self.transport,
            timeout_seconds=timeout_seconds,
            deadline_ok=deadline_ok,
            cancelled=cancel,
        )

    def _persist_and_classify(
        self, row: RowInput, outcome: FullProbeOutcome, scopes: _Scopes
    ) -> RouteOutcome:
        """Serialize cache write + scope bookkeeping; return the row outcome."""
        if outcome.no_write:
            # Partial / cancelled / deadline / request-infra: leave prior proof.
            reason = {
                "cancelled": REASON_CANCELLED,
                "deadline": REASON_WALL_CAP,
                "partial_not_tested": REASON_NOT_TESTED,
            }.get(outcome.no_write_reason, REASON_NOT_TESTED)
            if outcome.no_write_reason not in {"cancelled", "deadline", "partial_not_tested"}:
                # context_length / gateway_busy: request/infra scope, no poison.
                reason = outcome.no_write_reason
            return RouteOutcome(
                row.route_id,
                row.provider,
                False,
                False,
                category=outcome.category,
                http_status=outcome.http_status,
                refresh_reason=reason,
                requests_made=outcome.requests_made,
            )

        result = outcome.result
        assert result is not None  # no_write is False here
        now = self.clock()
        category = result.category
        retry_after = outcome.retry_after_seconds
        # Determine scope stop BEFORE the write so a provider block persists even
        # if a sibling later succeeds; the write records the scoped cooldown too.
        provider_stop = category in _PROVIDER_STOP_CATEGORIES
        model_scoped = outcome.http_status == 403 and not provider_stop

        def _mutate(cache: HealthCache) -> None:
            cache.record(row.route_id, result, now)
            if provider_stop:
                seconds = negative_seconds(
                    category, http_status=outcome.http_status, retry_after_seconds=retry_after
                )
                cache.record_cooldown(
                    ScopedCooldown(
                        key=f"provider:{row.provider.lower()}",
                        category=category,
                        checked_at=now,
                        until=now + timedelta(seconds=seconds),
                        provider_id=row.provider,
                    )
                )
                if category == CATEGORY_RATE_LIMITED:
                    cache.zero_bucket(row.provider, now + timedelta(seconds=seconds), pool=row.pool)

        self.cache.merge_and_save(_mutate)

        if provider_stop:
            scopes.stop_provider(row.provider, category)
        elif model_scoped:
            scopes.stop_route(row.route_id, category)

        return RouteOutcome(
            row.route_id,
            row.provider,
            probed=True,
            verified=bool(result.healthy),
            category=category,
            http_status=outcome.http_status,
            refresh_reason=None,
            requests_made=outcome.requests_made,
        )

    def _release_tokens(self, row: RowInput, amount: int, now: datetime) -> None:
        """Return unused request reservations to the provider bucket."""
        if amount <= 0:
            return
        bucket = self.cache.bucket_for(row.provider, row.pool)
        for _ in range(amount):
            if bucket.timestamps:
                bucket.timestamps.pop()

    # -- join path ---------------------------------------------------------

    def _join_and_wait(
        self,
        candidates: list[RowInput],
        *,
        consumer: str,
        config: RefreshConfig,
        marker: _Marker,
        lock: _JobLock,
        on_progress: Callable[[ProgressEvent], None] | None,
        cancel: Callable[[], bool],
    ) -> RefreshOutcome:
        """Join an in-flight job: WAIT (bounded) and read its results.

        A joined consumer never queues a second sweep. Ids the in-flight job
        does not cover stay unrefreshed with ``joined_job_not_covered``.
        """
        started = self.monotonic()
        deadline = started + config.wall_seconds
        wanted = {row.route_id for row in candidates}
        while True:
            if cancel():
                break
            if not lock.live():
                break  # owner finished or crashed
            payload = marker.read()
            if payload is not None and not payload.get("running", True):
                break
            if self.monotonic() >= deadline:
                break
            self.sleep(min(0.05, max(0.0, deadline - self.monotonic())))

        payload = marker.read() or {}
        job_id = str(payload.get("job_id") or "joined")
        covered = payload.get("outcomes") or {}
        outcomes: dict[str, RouteOutcome] = {}
        verified = failed = unavailable = probed = 0
        for row in candidates:
            raw = covered.get(row.route_id) if isinstance(covered, Mapping) else None
            if isinstance(raw, Mapping) and raw.get("probed"):
                probed += 1
                is_verified = bool(raw.get("verified"))
                outcomes[row.route_id] = RouteOutcome(
                    row.route_id,
                    row.provider,
                    True,
                    is_verified,
                    category=raw.get("category"),
                    http_status=raw.get("http_status"),
                    refresh_reason=None,
                )
                if is_verified:
                    verified += 1
                else:
                    failed += 1
            else:
                outcomes[row.route_id] = RouteOutcome(
                    row.route_id,
                    row.provider,
                    False,
                    False,
                    refresh_reason=REASON_JOINED_NOT_COVERED,
                )
        cancelled = cancel()
        result_kind = OUTCOME_CANCELLED if cancelled else OUTCOME_JOINED
        complete = result_kind == OUTCOME_JOINED and all(
            o.refresh_reason is None for o in outcomes.values()
        )
        del wanted
        return RefreshOutcome(
            outcome=result_kind,
            job_id=job_id,
            consumer=consumer,
            probed=probed,
            verified=verified,
            failed=failed,
            unavailable=unavailable,
            requests_made=0,  # joined consumers make no calls themselves
            elapsed_seconds=self.monotonic() - started,
            route_outcomes=outcomes,
            cap_reason=None if complete else REASON_JOINED_NOT_COVERED,
            complete=complete,
            note="joined",
        )

    # -- helpers -----------------------------------------------------------

    def _debounce_match(
        self,
        prior: Mapping[str, Any],
        candidates: list[RowInput],
        config: RefreshConfig,
        lock: _JobLock,
    ) -> bool:
        if prior.get("running", False):
            return False  # an in-flight job is JOINed, not debounced
        if lock.live():
            return False
        finished = prior.get("finished_wall")
        if not isinstance(finished, (int, float)):
            return False
        if (self._wall_now() - float(finished)) > config.debounce_seconds:
            return False
        prior_ids = prior.get("ids")
        if not isinstance(prior_ids, list):
            return False
        want = {row.route_id for row in candidates}
        return want.issubset(set(str(i) for i in prior_ids))

    def _from_marker(
        self, payload: Mapping[str, Any], consumer: str, outcome: str, *, note: str
    ) -> RefreshOutcome:
        covered = payload.get("outcomes") or {}
        outcomes: dict[str, RouteOutcome] = {}
        verified = failed = probed = 0
        if isinstance(covered, Mapping):
            for rid, raw in covered.items():
                if not isinstance(raw, Mapping):
                    continue
                is_probed = bool(raw.get("probed"))
                is_verified = bool(raw.get("verified"))
                outcomes[str(rid)] = RouteOutcome(
                    str(rid),
                    str(raw.get("provider") or ""),
                    is_probed,
                    is_verified,
                    category=raw.get("category"),
                    http_status=raw.get("http_status"),
                )
                if is_probed:
                    probed += 1
                    if is_verified:
                        verified += 1
                    else:
                        failed += 1
        return RefreshOutcome(
            outcome=outcome,
            job_id=str(payload.get("job_id") or "debounced"),
            consumer=consumer,
            probed=probed,
            verified=verified,
            failed=failed,
            unavailable=0,
            requests_made=0,
            elapsed_seconds=0.0,
            route_outcomes=outcomes,
            complete=True,
            note=note,
        )

    def _empty(
        self,
        outcome: str,
        consumer: str,
        job_id: str,
        wanted: Sequence[RowInput],
        reason: str | None,
    ) -> RefreshOutcome:
        outcomes = {
            row.route_id: RouteOutcome(
                row.route_id, row.provider, False, False, refresh_reason=reason
            )
            for row in wanted
        }
        return RefreshOutcome(
            outcome=outcome,
            job_id=job_id,
            consumer=consumer,
            probed=0,
            verified=0,
            failed=0,
            unavailable=0,
            requests_made=0,
            elapsed_seconds=0.0,
            route_outcomes=outcomes,
            complete=True,
        )

    def _write_marker(
        self,
        marker: _Marker,
        job_id: str,
        consumer: str,
        candidates: Sequence[RowInput],
        started: float,
        deadline: float,
        outcomes: Mapping[str, RouteOutcome],
        *,
        running: bool,
        finished_at: float | None = None,
        result: str | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "job_id": job_id,
            "owner": consumer,
            "ids": [row.route_id for row in candidates],
            "start_monotonic": started,
            "deadline_monotonic": deadline,
            "running": running,
            "sequence": self._seq,
            "outcomes": {
                rid: {
                    "provider": o.provider,
                    "probed": o.probed,
                    "verified": o.verified,
                    "category": o.category,
                    "http_status": o.http_status,
                    "refresh_reason": o.refresh_reason,
                }
                for rid, o in outcomes.items()
            },
        }
        if finished_at is not None:
            payload["finished_wall"] = finished_at
        if result is not None:
            payload["result"] = result
        marker.write(payload)

    def _emit(
        self,
        on_progress: Callable[[ProgressEvent], None] | None,
        job_id: str,
        probed: int,
        total: int,
        verified: int,
        failed: int,
        unavailable: int,
        requests_made: int,
        requests_reserved: int,
        started: float,
        per_provider: Mapping[str, int],
        last_reason: str | None,
    ) -> None:
        if on_progress is None:
            return
        self._seq += 1
        on_progress(
            ProgressEvent(
                sequence=self._seq,
                job_id=job_id,
                probed=probed,
                total=total,
                verified=verified,
                failed=failed,
                unavailable=unavailable,
                requests_made=requests_made,
                requests_reserved=requests_reserved,
                elapsed_seconds=self.monotonic() - started,
                per_provider=dict(per_provider),
                last_reason=last_reason,
            )
        )

    def _emit_final(
        self,
        on_progress: Callable[[ProgressEvent], None] | None,
        job_id: str,
        probed: int,
        total: int,
        verified: int,
        failed: int,
        unavailable: int,
        requests_made: int,
        started: float,
        per_provider: Mapping[str, int],
    ) -> None:
        if on_progress is None:
            return
        self._seq += 1
        on_progress(
            ProgressEvent(
                sequence=self._seq,
                job_id=job_id,
                probed=probed,
                total=total,
                verified=verified,
                failed=failed,
                unavailable=unavailable,
                requests_made=requests_made,
                requests_reserved=requests_made,
                elapsed_seconds=self.monotonic() - started,
                per_provider=dict(per_provider),
                last_reason=None,
                kind="final",
            )
        )

    def _new_job_id(self) -> str:
        return format_datetime(self.clock()).replace(":", "").replace("-", "")

    def _wall_now(self) -> float:
        return self.clock().timestamp()


def refresh_for_consumer(
    snapshot: RefreshSnapshot,
    *,
    consumer: str,
    needed_ids: Sequence[str],
    config: RefreshConfig,
    cache: HealthCache,
    transport: Callable[[str, str, float], ProbeExchange] | None = None,
    clock: Callable[[], datetime] | None = None,
    monotonic: Callable[[], float] | None = None,
    sleep: Callable[[float], None] | None = None,
    explicit: bool = False,
    on_progress: Callable[[ProgressEvent], None] | None = None,
    cancel: Callable[[], bool] | None = None,
    lock_path: Path | None = None,
    marker_path: Path | None = None,
    job_id: str | None = None,
) -> RefreshOutcome:
    """Module-level convenience wrapper over :class:`RefreshCoordinator`.

    Matches the design signature; all I/O is injected for hermetic tests.
    """
    coordinator = RefreshCoordinator(
        cache=cache,
        transport=transport,
        clock=clock or (lambda: datetime.now(timezone.utc)),
        monotonic=monotonic or time.monotonic,
        sleep=sleep or time.sleep,
        lock_path=lock_path,
        marker_path=marker_path,
    )
    return coordinator.refresh_for_consumer(
        snapshot,
        consumer=consumer,
        needed_ids=needed_ids,
        config=config,
        explicit=explicit,
        on_progress=on_progress,
        cancel=cancel,
        job_id=job_id,
    )


__all__ = [
    "CAPACITY_FREE",
    "CAPACITY_METERED",
    "CAPACITY_SUBSCRIPTION",
    "CAPACITY_UNKNOWN",
    "CONSUMER_PICKER",
    "CONSUMER_SELECTION",
    "CONSUMER_VERIFIED_VIEW",
    "DEBOUNCE_SECONDS",
    "DEFAULT_CONCURRENCY",
    "DEFAULT_MAX_REQUESTS",
    "DEFAULT_MAX_ROUTES",
    "DEFAULT_PAGE_SIZE",
    "DEFAULT_WALL_SECONDS",
    "ENV_AUTO_REFRESH",
    "ENV_CONCURRENCY",
    "ENV_MAX_REQUESTS",
    "ENV_MAX_ROUTES",
    "ENV_WALL_SECONDS",
    "HARD_MAX_CONCURRENCY",
    "HARD_MAX_REQUESTS",
    "HARD_MAX_ROUTES",
    "HARD_MAX_WALL_SECONDS",
    "OUTCOME_AUTO_DISABLED",
    "OUTCOME_CANCELLED",
    "OUTCOME_CAPPED",
    "OUTCOME_COMPLETED",
    "OUTCOME_DEBOUNCED",
    "OUTCOME_JOINED",
    "OUTCOME_NOTHING_ELIGIBLE",
    "OUTCOME_REUSED_FRESH",
    "REASON_BUCKET",
    "REASON_CANCELLED",
    "REASON_JOINED_NOT_COVERED",
    "REASON_NOT_TESTED",
    "REASON_PROVIDER_STOP",
    "REASON_REQUEST_CAP",
    "REASON_ROUTE_CAP",
    "REASON_WALL_CAP",
    "ProgressEvent",
    "RefreshConfig",
    "RefreshConfigError",
    "RefreshCoordinator",
    "RefreshOutcome",
    "RefreshSnapshot",
    "RouteOutcome",
    "RowInput",
    "VerifiedRow",
    "config_from_env",
    "refresh_for_consumer",
    "select_candidates",
]
