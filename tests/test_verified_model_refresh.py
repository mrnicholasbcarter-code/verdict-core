"""Bounded verification refresh coordinator tests (BOD-292 unit 3).

Everything is injected: transports, clocks, cache paths in tmp_path, lock and
marker paths. No real gateway or model is ever contacted.
"""

from __future__ import annotations

import contextlib
import threading
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, cast

import pytest

from verdict.actions.model_refresh import (
    PLAN_SCHEMA,
    action_models_refresh_execute,
    action_models_refresh_plan,
    plan_digest,
)
from verdict.orchestration.health_cache import CATEGORY_OK, HealthCache, ProbeResult, usable_until
from verdict.orchestration.verified_refresh import (
    CAPACITY_FREE,
    CAPACITY_METERED,
    CAPACITY_SUBSCRIPTION,
    DEFAULT_MAX_REQUESTS,
    HARD_MAX_ROUTES,
    OUTCOME_AUTO_DISABLED,
    OUTCOME_CANCELLED,
    OUTCOME_CAPPED,
    OUTCOME_COMPLETED,
    OUTCOME_DEBOUNCED,
    OUTCOME_JOINED,
    OUTCOME_NOTHING_ELIGIBLE,
    OUTCOME_REUSED_FRESH,
    REASON_JOINED_NOT_COVERED,
    REASON_PROVIDER_STOP,
    REASON_REQUEST_CAP,
    REASON_ROUTE_CAP,
    REQUESTS_PER_FULL_PROBE,
    ProbeExchange,
    RefreshConfig,
    RefreshConfigError,
    RefreshCoordinator,
    RefreshSnapshot,
    RouteOutcome,
    RowInput,
    config_from_env,
    refresh_for_consumer,
    select_candidates,
)

NOW = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _row(
    route_id: str,
    *,
    status: str = "UNVERIFIED",
    capacity: str = CAPACITY_FREE,
    refreshable: bool = True,
    provider: str | None = None,
    last_success_at: datetime | None = None,
    rank_hint: int | None = None,
    pool: str | None = None,
) -> RowInput:
    return RowInput(
        route_id=route_id,
        provider=provider or route_id.split("/", 1)[0],
        status=status,
        capacity_class=capacity,
        refreshable=refreshable,
        refresh_reason=None,
        last_success_at=last_success_at,
        rank_hint=rank_hint,
        pool=pool,
    )


class ScriptedTransport:
    """Injected transport replaying scripted exchanges per (route, phase).

    Records every call so tests can assert exact request counts and that no
    metered/unknown route was ever contacted.
    """

    def __init__(self, script: dict[tuple[str, str], ProbeExchange]):
        self.script = script
        self.calls: list[tuple[str, str]] = []
        self.lock = threading.Lock()

    def __call__(self, route_id: str, phase: str, timeout: float) -> ProbeExchange:
        with self.lock:
            self.calls.append((route_id, phase))
        ex = self.script.get((route_id, phase))
        if ex is None:
            # Default: a clean matching success.
            return _ok(route_id, phase)
        return ex

    def routes_called(self) -> set[str]:
        return {rid for rid, _ in self.calls}


def _ok(route_id: str, phase: str) -> ProbeExchange:
    suffix = route_id.split("/", 1)[1] if "/" in route_id else route_id
    return ProbeExchange(
        http_status=200,
        ok=True,
        chat_exact=(phase == "chat"),
        tool_called=(phase == "tool"),
        latency_ms=5.0,
        reported_model=suffix,
    )


def _fail(
    status: int, category: str | None = None, retry_after: float | None = None
) -> ProbeExchange:
    return ProbeExchange(
        http_status=status, ok=False, error_category=category, retry_after_seconds=retry_after
    )


def _coordinator(
    tmp_path: Path,
    transport: Callable[[str, str, float], ProbeExchange] | None,
    *,
    clock: Callable[[], datetime] | None = None,
    monotonic: Callable[[], float] | None = None,
) -> RefreshCoordinator:
    cache = HealthCache(tmp_path / "health-cache.json")

    def frozen_mono() -> float:
        return 0.0

    return RefreshCoordinator(
        cache=cache,
        transport=transport,
        clock=clock or (lambda: NOW),
        monotonic=monotonic or frozen_mono,
        sleep=lambda _s: None,
        lock_path=tmp_path / "refresh.lock",
        marker_path=tmp_path / "refresh.json",
    )


def _snapshot(rows: Sequence[RowInput], *, generation: str = "gen1") -> RefreshSnapshot:
    return RefreshSnapshot(rows=tuple(rows), generation=generation)


# ---------------------------------------------------------------------------
# Config validation
# ---------------------------------------------------------------------------


def test_defaults_and_hard_maxima() -> None:
    c = RefreshConfig()
    assert (c.max_routes, c.max_requests, c.wall_seconds, c.concurrency) == (20, 40, 120.0, 4)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_routes": 0},
        {"max_routes": HARD_MAX_ROUTES + 1},
        {"max_requests": 101},
        {"wall_seconds": 0},
        {"wall_seconds": 121},
        {"concurrency": 5},
        {"concurrency": 0},
    ],
)
def test_invalid_config_raises(kwargs: dict[str, object]) -> None:
    with pytest.raises(RefreshConfigError):
        RefreshConfig(**kwargs)  # type: ignore[arg-type]


def test_config_from_env_validates_before_calls() -> None:
    with pytest.raises(RefreshConfigError):
        config_from_env({"VERDICT_REFRESH_MAX_ROUTES": "999"})
    with pytest.raises(RefreshConfigError):
        config_from_env({"VERDICT_REFRESH_WALL_SECONDS": "notanumber"})
    cfg = config_from_env({"VERDICT_AUTO_REFRESH": "0", "VERDICT_REFRESH_MAX_ROUTES": "10"})
    assert cfg.auto_refresh is False
    assert cfg.max_routes == 10


def test_auto_refresh_disabled_returns_labelled_snapshot(tmp_path: Path) -> None:
    coord = _coordinator(tmp_path, ScriptedTransport({}))
    snap = _snapshot([_row("cc/a")])
    cfg = RefreshConfig(auto_refresh=False)
    out = coord.refresh_for_consumer(
        snap, consumer="verified_view", needed_ids=["cc/a"], config=cfg
    )
    assert out.outcome == OUTCOME_AUTO_DISABLED
    assert out.requests_made == 0


# ---------------------------------------------------------------------------
# Pure plan + fresh bypass
# ---------------------------------------------------------------------------


def test_plan_is_pure_zero_io_probes_writes(tmp_path: Path) -> None:
    rows = [
        {
            "route_id": "cc/a",
            "provider": "cc",
            "status": "STALE",
            "capacity_class": CAPACITY_FREE,
            "refreshable": True,
        }
    ]
    result = action_models_refresh_plan(
        snapshot_rows=rows, needed_ids=["cc/a"], now=NOW, consumer="verified_view"
    )
    assert result.ok
    plan = result.data
    assert plan["schema"] == PLAN_SCHEMA
    assert plan["requires_confirmation"] is True
    assert plan["estimated_requests"] == 2
    assert plan["plan_digest"] == plan_digest(plan)
    # No cache file, lock, or marker was created by planning.
    assert not any(tmp_path.iterdir())


def test_fresh_needed_entries_zero_calls_no_wait(tmp_path: Path) -> None:
    transport = ScriptedTransport({})
    coord = _coordinator(tmp_path, transport)
    # VERIFIED (fresh) rows are never candidates.
    snap = _snapshot([_row("cc/a", status="VERIFIED", refreshable=False)])
    out = coord.refresh_for_consumer(
        snap, consumer="verified_view", needed_ids=["cc/a"], config=RefreshConfig()
    )
    assert out.outcome == OUTCOME_REUSED_FRESH
    assert out.requests_made == 0
    assert transport.calls == []
    # No lock/marker written on the fresh bypass.
    assert not (tmp_path / "refresh.lock").exists()


def test_nothing_eligible_when_no_needed_ids(tmp_path: Path) -> None:
    coord = _coordinator(tmp_path, ScriptedTransport({}))
    snap = _snapshot([_row("cc/a")])
    out = coord.refresh_for_consumer(
        snap, consumer="verified_view", needed_ids=[], config=RefreshConfig()
    )
    assert out.outcome == OUTCOME_NOTHING_ELIGIBLE
    assert out.requests_made == 0


# ---------------------------------------------------------------------------
# Trigger => bounded prepaid probes only
# ---------------------------------------------------------------------------


def test_trigger_probes_only_prepaid_stale_unverified(tmp_path: Path) -> None:
    transport = ScriptedTransport({})
    coord = _coordinator(tmp_path, transport)
    rows = [
        _row("free/a", status="UNVERIFIED", capacity=CAPACITY_FREE),
        _row("sub/b", status="STALE", capacity=CAPACITY_SUBSCRIPTION),
        _row("metered/c", status="UNVERIFIED", capacity=CAPACITY_METERED, refreshable=False),
        _row("unk/d", status="UNVERIFIED", capacity="unknown", refreshable=False),
        _row("free/e", status="VERIFIED", capacity=CAPACITY_FREE, refreshable=False),
    ]
    snap = _snapshot(rows)
    out = coord.refresh_for_consumer(
        snap,
        consumer="verified_view",
        needed_ids=[r.route_id for r in rows],
        config=RefreshConfig(),
    )
    assert out.outcome == OUTCOME_COMPLETED
    called = transport.routes_called()
    assert "free/a" in called and "sub/b" in called
    # METERED/UNKNOWN never auto-probed; fresh VERIFIED never re-probed.
    assert "metered/c" not in called
    assert "unk/d" not in called
    assert "free/e" not in called
    assert out.verified == 2
    assert out.requests_made == 4  # two full probes, two requests each


def test_metered_unknown_never_probed_even_if_flagged(tmp_path: Path) -> None:
    transport = ScriptedTransport({})
    coord = _coordinator(tmp_path, transport)
    # Even if an upstream mislabels a metered row refreshable, capacity gate wins.
    rows = [_row("m/x", status="UNVERIFIED", capacity=CAPACITY_METERED, refreshable=True)]
    out = coord.refresh_for_consumer(
        _snapshot(rows), consumer="verified_view", needed_ids=["m/x"], config=RefreshConfig()
    )
    assert out.outcome == OUTCOME_REUSED_FRESH
    assert transport.calls == []


# ---------------------------------------------------------------------------
# Priority + provider round-robin
# ---------------------------------------------------------------------------


def test_candidate_priority_and_round_robin() -> None:
    rows = [
        _row("p1/stale", status="STALE", last_success_at=NOW - timedelta(hours=1), rank_hint=1),
        _row("p1/never", status="UNVERIFIED", rank_hint=2),
        _row(
            "p2/halfopen",
            status="UNVERIFIED",
            last_success_at=NOW - timedelta(hours=2),
            rank_hint=1,
        ),
        _row("p2/never", status="UNVERIFIED", rank_hint=3),
    ]
    snap = _snapshot(rows)
    ordered = select_candidates(
        snap, needed_ids=[r.route_id for r in rows], explicit=False, config=RefreshConfig()
    )
    ids = [r.route_id for r in ordered]
    # Band 0 (stale) first, then band 1 (half-open), then band 2 (never).
    assert ids[0] == "p1/stale"
    assert ids[1] == "p2/halfopen"
    assert set(ids[2:]) == {"p1/never", "p2/never"}


def test_only_supplied_ids_enter_plan() -> None:
    rows = [_row("cc/a"), _row("cc/b"), _row("cc/c")]
    snap = _snapshot(rows)
    ordered = select_candidates(
        snap, needed_ids=["cc/a", "cc/c"], explicit=True, config=RefreshConfig()
    )
    assert {r.route_id for r in ordered} == {"cc/a", "cc/c"}


# ---------------------------------------------------------------------------
# Caps
# ---------------------------------------------------------------------------


def test_route_cap_labels_unrefreshed_rows(tmp_path: Path) -> None:
    transport = ScriptedTransport({})
    coord = _coordinator(tmp_path, transport)
    rows = [_row(f"p{i}/r", provider=f"p{i}") for i in range(5)]
    cfg = RefreshConfig(max_routes=2)
    out = coord.refresh_for_consumer(
        _snapshot(rows), consumer="verified_view", needed_ids=[r.route_id for r in rows], config=cfg
    )
    # Only 2 routes probed; the other 3 were never in the plan => route_cap.
    assert out.probed == 2
    uncovered = [o for o in out.route_outcomes.values() if o.refresh_reason == REASON_ROUTE_CAP]
    assert len(uncovered) == 3


def test_request_cap_never_exceeded(tmp_path: Path) -> None:
    transport = ScriptedTransport({})
    coord = _coordinator(tmp_path, transport)
    rows = [_row(f"p{i}/r", provider=f"p{i}") for i in range(10)]
    cfg = RefreshConfig(max_routes=10, max_requests=5)
    out = coord.refresh_for_consumer(
        _snapshot(rows), consumer="verified_view", needed_ids=[r.route_id for r in rows], config=cfg
    )
    assert out.requests_made <= 5
    # 2 full probes = 4 requests; a third would exceed 5, so it stops at request_cap.
    assert out.cap_reason == REASON_REQUEST_CAP
    assert out.outcome == OUTCOME_CAPPED
    assert out.complete is False


def test_concurrency_reservations_never_exceed_request_cap(tmp_path: Path) -> None:
    # With atomic 2-request reservations, the actual request count must stay
    # within the cap regardless of how many routes are pending.
    transport = ScriptedTransport({})
    coord = _coordinator(tmp_path, transport)
    rows = [_row(f"p{i}/r", provider=f"p{i}") for i in range(20)]
    cfg = RefreshConfig(max_routes=20, max_requests=6, concurrency=4)
    out = coord.refresh_for_consumer(
        _snapshot(rows), consumer="verified_view", needed_ids=[r.route_id for r in rows], config=cfg
    )
    assert out.requests_made <= 6
    assert len(transport.calls) <= 6


# ---------------------------------------------------------------------------
# Provider scope stop
# ---------------------------------------------------------------------------


def test_provider_auth_failure_stops_that_provider(tmp_path: Path) -> None:
    transport = ScriptedTransport({("cc/a", "chat"): _fail(401)})
    coord = _coordinator(tmp_path, transport)
    rows = [
        _row("cc/a", provider="cc", rank_hint=1),
        _row("cc/b", provider="cc", rank_hint=2),
        _row("kr/c", provider="kr", rank_hint=1),
    ]
    out = coord.refresh_for_consumer(
        _snapshot(rows),
        consumer="verified_view",
        needed_ids=[r.route_id for r in rows],
        config=RefreshConfig(),
    )
    # cc/a failed 401 (provider-scope). cc/b must be stopped, not probed. kr/c proceeds.
    assert ("cc/b", "chat") not in transport.calls
    assert out.route_outcomes["cc/b"].refresh_reason == REASON_PROVIDER_STOP
    assert out.route_outcomes["kr/c"].verified is True
    # The provider cooldown is persisted to the cache.
    cache = HealthCache(tmp_path / "health-cache.json")
    assert cache.cooldown_for("provider:cc", NOW) is not None


def test_auth_gate_confirmed_outage_writes_nothing_and_stops_the_job(tmp_path: Path) -> None:
    """BOD-338 breaker preserved through the coordinator (prove-at-rest S05-B).

    An ``auth_gate`` hook is called for a CATEGORY_AUTH outcome BEFORE any
    write. When it confirms a gateway-key outage (True), the route's auth
    failure is NOT recorded (no provider cooldown, no negative cache entry)
    and the job stops with ``REASON_AUTH_OUTAGE``; a sibling never reached
    is left unprobed, not poisoned.
    """
    transport = ScriptedTransport({("cc/a", "chat"): _fail(401)})
    gate_calls: list[tuple[str, int | None]] = []

    def auth_gate(
        route_id: str, http_status: int | None, remaining_requests: int
    ) -> tuple[bool, int]:
        gate_calls.append((route_id, http_status))
        return True, 0  # confirmed outage, no live check spent

    coord = _coordinator(tmp_path, transport)
    coord.auth_gate = auth_gate
    rows = [
        _row("cc/a", provider="cc", rank_hint=1),
        _row("cc/b", provider="cc", rank_hint=2),
        _row("kr/c", provider="kr", rank_hint=1),
    ]
    out = coord.refresh_for_consumer(
        _snapshot(rows),
        consumer="verified_view",
        needed_ids=[r.route_id for r in rows],
        config=RefreshConfig(
            concurrency=1
        ),  # the sequential guarantee: nothing is dispatched after it
    )
    assert gate_calls == [("cc/a", 401)]
    assert out.route_outcomes["cc/a"].refresh_reason == "auth_outage"
    assert out.route_outcomes["cc/a"].probed is False
    # Confirmed outage stops the job: siblings are never reached or poisoned.
    assert ("cc/b", "chat") not in transport.calls
    assert ("kr/c", "chat") not in transport.calls
    cache = HealthCache(tmp_path / "health-cache.json")
    assert cache.entry("cc/a") is None
    assert cache.cooldown_for("provider:cc", NOW) is None


def test_auth_gate_denied_outage_records_the_real_auth_failure(tmp_path: Path) -> None:
    """A rejected outage (False, or no hook) writes the auth failure as today."""
    transport = ScriptedTransport({("cc/a", "chat"): _fail(401)})
    coord = _coordinator(tmp_path, transport)
    coord.auth_gate = lambda route_id, http_status, remaining_requests: (False, 0)
    rows = [_row("cc/a", provider="cc", rank_hint=1), _row("cc/b", provider="cc", rank_hint=2)]
    out = coord.refresh_for_consumer(
        _snapshot(rows),
        consumer="verified_view",
        needed_ids=[r.route_id for r in rows],
        config=RefreshConfig(),
    )
    assert out.route_outcomes["cc/b"].refresh_reason == REASON_PROVIDER_STOP
    cache = HealthCache(tmp_path / "health-cache.json")
    assert cache.cooldown_for("provider:cc", NOW) is not None


def test_auth_gate_fires_on_403_permission_category_too(tmp_path: Path) -> None:
    """A 403 classifies as CATEGORY_PERMISSION, not CATEGORY_AUTH -- the gate
    must still fire, matching Prober._buffer_auth_failure's own (401, 403)
    check rather than one specific category string.
    """
    transport = ScriptedTransport({("cc/a", "chat"): _fail(403)})
    gate_calls: list[tuple[str, int | None]] = []

    def auth_gate(
        route_id: str, http_status: int | None, remaining_requests: int
    ) -> tuple[bool, int]:
        gate_calls.append((route_id, http_status))
        return True, 0

    coord = _coordinator(tmp_path, transport)
    coord.auth_gate = auth_gate
    rows = [_row("cc/a", provider="cc", rank_hint=1), _row("cc/b", provider="cc", rank_hint=2)]
    out = coord.refresh_for_consumer(
        _snapshot(rows),
        consumer="verified_view",
        needed_ids=[r.route_id for r in rows],
        config=RefreshConfig(),
    )
    assert gate_calls == [("cc/a", 403)]
    assert out.route_outcomes["cc/a"].refresh_reason == "auth_outage"
    assert ("cc/b", "chat") not in transport.calls
    cache = HealthCache(tmp_path / "health-cache.json")
    assert cache.entry("cc/a") is None


def test_provider_stop_never_demotes_an_otherwise_complete_job(tmp_path: Path) -> None:
    """A real per-route/provider stop (401 on af/a) skips af/b (sibling,
    REASON_PROVIDER_STOP) but the rest of the plan succeeds -- the job's
    overall outcome is COMPLETED, not CAPPED, and af/b's own outcome still
    carries provider_scope_stopped.
    """
    transport = ScriptedTransport({("af/a", "chat"): _fail(401)})
    rows = [
        _row("af/a", provider="af", rank_hint=1),
        _row("af/b", provider="af", rank_hint=2),
        _row("kr/c", provider="kr", rank_hint=1),
    ]
    coord = _coordinator(tmp_path, transport)
    out = coord.refresh_for_consumer(
        _snapshot(rows),
        consumer="verified_view",
        needed_ids=[r.route_id for r in rows],
        config=RefreshConfig(),
    )
    assert out.outcome == OUTCOME_COMPLETED
    assert out.complete is True
    assert out.route_outcomes["af/b"].refresh_reason == REASON_PROVIDER_STOP
    assert out.route_outcomes["kr/c"].verified is True


def test_429_honors_retry_after_and_zeroes_bucket(tmp_path: Path) -> None:
    transport = ScriptedTransport({("cc/a", "chat"): _fail(429, "rate_limited", retry_after=120.0)})
    coord = _coordinator(tmp_path, transport)
    rows = [_row("cc/a", provider="cc", rank_hint=1), _row("cc/b", provider="cc", rank_hint=2)]
    out = coord.refresh_for_consumer(
        _snapshot(rows),
        consumer="verified_view",
        needed_ids=["cc/a", "cc/b"],
        config=RefreshConfig(),
    )
    cache = HealthCache(tmp_path / "health-cache.json")
    cd = cache.cooldown_for("provider:cc", NOW)
    assert cd is not None
    # Retry-After 120s drives the deadline.
    assert cd.until == NOW + timedelta(seconds=120.0)
    assert out.route_outcomes["cc/b"].refresh_reason == REASON_PROVIDER_STOP


def test_model_scoped_403_stops_only_that_route(tmp_path: Path) -> None:
    transport = ScriptedTransport({("cc/a", "chat"): _fail(403, "permission")})
    coord = _coordinator(tmp_path, transport)
    rows = [_row("cc/a", provider="cc", rank_hint=1), _row("cc/b", provider="cc", rank_hint=2)]
    out = coord.refresh_for_consumer(
        _snapshot(rows),
        consumer="verified_view",
        needed_ids=["cc/a", "cc/b"],
        config=RefreshConfig(),
    )
    # 403 is provider-scope by default in the cache classifier; assert cc/a failed
    # and the provider stop is applied (design: model-scoped 403 stays route only,
    # but a bare permission maps to provider scope). cc/a itself is not verified.
    assert out.route_outcomes["cc/a"].verified is False


# ---------------------------------------------------------------------------
# Partial proof => NOT_TESTED, no write
# ---------------------------------------------------------------------------


def test_partial_chat_without_tool_is_not_tested(tmp_path: Path) -> None:
    # chat succeeds, then the wall deadline fires before the tool call.
    clock_times = [NOW]
    mono = [0.0]

    def monotonic() -> float:
        return mono[0]

    transport = ScriptedTransport({("free/a", "chat"): _ok("free/a", "chat")})
    cache = HealthCache(tmp_path / "health-cache.json")
    coord = RefreshCoordinator(
        cache=cache,
        transport=transport,
        clock=lambda: NOW,
        monotonic=monotonic,
        sleep=lambda _s: None,
        lock_path=tmp_path / "refresh.lock",
        marker_path=tmp_path / "refresh.json",
    )

    # Set wall so the deadline passes between chat and tool by making
    # monotonic jump inside the transport.
    def chat_then_elapse(route_id: str, phase: str, timeout: float) -> ProbeExchange:
        if phase == "chat":
            mono[0] = 999.0  # deadline now elapsed
            return _ok(route_id, phase)
        return _ok(route_id, phase)

    coord.transport = chat_then_elapse
    out = coord.refresh_for_consumer(
        _snapshot([_row("free/a")]),
        consumer="verified_view",
        needed_ids=["free/a"],
        config=RefreshConfig(wall_seconds=10.0),
    )
    # The partial chat recorded no route health (not tested).
    reloaded = HealthCache(tmp_path / "health-cache.json")
    assert reloaded.entry("free/a") is None
    assert out.route_outcomes["free/a"].probed is False
    del clock_times


# ---------------------------------------------------------------------------
# Single-flight: join + debounce
# ---------------------------------------------------------------------------


def test_second_process_joins_not_second_sweep(tmp_path: Path) -> None:
    # Owner holds the lock and writes a finished marker; a joining caller reads it.
    lock_path = tmp_path / "refresh.lock"
    marker_path = tmp_path / "refresh.json"
    transport = ScriptedTransport({})
    owner = RefreshCoordinator(
        cache=HealthCache(tmp_path / "health-cache.json"),
        transport=transport,
        clock=lambda: NOW,
        monotonic=lambda: 0.0,
        sleep=lambda _s: None,
        lock_path=lock_path,
        marker_path=marker_path,
    )
    rows = [_row("cc/a"), _row("cc/b")]
    snap = _snapshot(rows)
    owner.refresh_for_consumer(
        snap, consumer="verified_view", needed_ids=["cc/a", "cc/b"], config=RefreshConfig()
    )
    assert transport.routes_called() == {"cc/a", "cc/b"}

    # A second coordinator, same marker, different id set -> uncovered id joins.
    joiner_transport = ScriptedTransport({})
    joiner = RefreshCoordinator(
        cache=HealthCache(tmp_path / "health-cache.json"),
        transport=joiner_transport,
        clock=lambda: NOW + timedelta(seconds=30),  # outside debounce window
        monotonic=lambda: 0.0,
        sleep=lambda _s: None,
        lock_path=lock_path,
        marker_path=marker_path,
    )
    # Force the join path by holding the lock from another thread.
    import fcntl

    held = lock_path.open("a+")
    fcntl.flock(held.fileno(), fcntl.LOCK_EX)
    try:
        out = joiner.refresh_for_consumer(
            _snapshot([_row("cc/a"), _row("cc/zzz")]),
            consumer="picker",
            needed_ids=["cc/a", "cc/zzz"],
            config=RefreshConfig(wall_seconds=1.0),
            explicit=True,
        )
    finally:
        fcntl.flock(held.fileno(), fcntl.LOCK_UN)
        held.close()
    assert out.outcome in {OUTCOME_JOINED, OUTCOME_CANCELLED, "lock_timeout"}
    # The joiner never probed anything itself.
    assert joiner_transport.calls == []
    # cc/zzz was not covered by the owner's job.
    assert out.route_outcomes["cc/zzz"].refresh_reason == REASON_JOINED_NOT_COVERED


def test_debounce_returns_prior_result_no_calls(tmp_path: Path) -> None:
    lock_path = tmp_path / "refresh.lock"
    marker_path = tmp_path / "refresh.json"
    t1 = ScriptedTransport({})
    coord = RefreshCoordinator(
        cache=HealthCache(tmp_path / "health-cache.json"),
        transport=t1,
        clock=lambda: NOW,
        monotonic=lambda: 0.0,
        sleep=lambda _s: None,
        lock_path=lock_path,
        marker_path=marker_path,
    )
    rows = [_row("cc/a"), _row("cc/b")]
    coord.refresh_for_consumer(
        _snapshot(rows),
        consumer="verified_view",
        needed_ids=["cc/a", "cc/b"],
        config=RefreshConfig(),
    )
    first_calls = len(t1.calls)
    assert first_calls == 4

    # A matching trigger a few seconds later is debounced: zero new calls.
    t2 = ScriptedTransport({})
    coord2 = RefreshCoordinator(
        cache=HealthCache(tmp_path / "health-cache.json"),
        transport=t2,
        clock=lambda: NOW + timedelta(seconds=3),
        monotonic=lambda: 0.0,
        sleep=lambda _s: None,
        lock_path=lock_path,
        marker_path=marker_path,
    )
    out = coord2.refresh_for_consumer(
        _snapshot(rows),
        consumer="verified_view",
        needed_ids=["cc/a", "cc/b"],
        config=RefreshConfig(),
    )
    assert out.outcome == OUTCOME_DEBOUNCED
    assert t2.calls == []


def test_debounce_window_expires(tmp_path: Path) -> None:
    lock_path = tmp_path / "refresh.lock"
    marker_path = tmp_path / "refresh.json"
    coord = RefreshCoordinator(
        cache=HealthCache(tmp_path / "health-cache.json"),
        transport=ScriptedTransport({}),
        clock=lambda: NOW,
        monotonic=lambda: 0.0,
        sleep=lambda _s: None,
        lock_path=lock_path,
        marker_path=marker_path,
    )
    rows = [_row("cc/a")]
    coord.refresh_for_consumer(
        _snapshot(rows), consumer="verified_view", needed_ids=["cc/a"], config=RefreshConfig()
    )
    # 30s later is past the 10s debounce window: a fresh job runs.
    t2 = ScriptedTransport({})
    coord2 = RefreshCoordinator(
        cache=HealthCache(tmp_path / "health-cache.json"),
        transport=t2,
        clock=lambda: NOW + timedelta(seconds=30),
        monotonic=lambda: 0.0,
        sleep=lambda _s: None,
        lock_path=lock_path,
        marker_path=marker_path,
    )
    out = coord2.refresh_for_consumer(
        _snapshot(rows), consumer="verified_view", needed_ids=["cc/a"], config=RefreshConfig()
    )
    assert out.outcome == OUTCOME_COMPLETED
    assert t2.calls  # it re-probed


# ---------------------------------------------------------------------------
# Cancel
# ---------------------------------------------------------------------------


def test_cancel_stops_further_dispatch_and_returns_last_known(tmp_path: Path) -> None:
    transport = ScriptedTransport({})
    coord = _coordinator(tmp_path, transport)
    cancelled = {"flag": False}

    def cancel() -> bool:
        return cancelled["flag"]

    rows = [_row(f"p{i}/r", provider=f"p{i}") for i in range(5)]

    # Cancel after the first probe by flipping the flag from on_progress.
    def on_progress(event: object) -> None:
        cancelled["flag"] = True

    out = coord.refresh_for_consumer(
        _snapshot(rows),
        consumer="verified_view",
        needed_ids=[r.route_id for r in rows],
        config=RefreshConfig(),
        on_progress=on_progress,
        cancel=cancel,
    )
    assert out.outcome == OUTCOME_CANCELLED
    assert out.complete is False
    # Fewer than all routes probed.
    assert out.probed < 5


# ---------------------------------------------------------------------------
# Cache persistence + reload
# ---------------------------------------------------------------------------


def test_results_persist_and_reload(tmp_path: Path) -> None:
    transport = ScriptedTransport({})
    coord = _coordinator(tmp_path, transport)
    coord.refresh_for_consumer(
        _snapshot([_row("free/a")]),
        consumer="verified_view",
        needed_ids=["free/a"],
        config=RefreshConfig(),
    )
    reloaded = HealthCache(tmp_path / "health-cache.json")
    entry = reloaded.entry("free/a")
    assert entry is not None and entry.healthy and entry.tool_ok
    assert entry.identity == "verified"
    assert entry.last_success_at == NOW


# ---------------------------------------------------------------------------
# Manual plan/execute refusals
# ---------------------------------------------------------------------------


def _plan_for(
    rows: list[dict[str, object]], ids: list[str], *, now: datetime = NOW
) -> dict[str, Any]:
    return cast(
        dict[str, Any],
        action_models_refresh_plan(
            snapshot_rows=rows,
            needed_ids=ids,
            now=now,
            consumer="manual",
            gateway_origin="http://gw",
            evidence_generation="gen1",
        ).data,
    )


def test_execute_refuses_without_confirmation(tmp_path: Path) -> None:
    rows = [
        {
            "route_id": "cc/a",
            "provider": "cc",
            "status": "STALE",
            "capacity_class": CAPACITY_FREE,
            "refreshable": True,
        }
    ]
    plan = _plan_for(rows, ["cc/a"])
    snap = _snapshot([_row("cc/a", status="STALE")], generation="gen1")
    cache = HealthCache(tmp_path / "health-cache.json")
    r = action_models_refresh_execute(plan=plan, snapshot=snap, cache=cache, now=NOW)
    assert not r.ok and r.data["reason"] == "not_confirmed"
    assert not (tmp_path / "health-cache.json").exists()


def test_execute_refuses_on_digest_mismatch(tmp_path: Path) -> None:
    rows = [
        {
            "route_id": "cc/a",
            "provider": "cc",
            "status": "STALE",
            "capacity_class": CAPACITY_FREE,
            "refreshable": True,
        }
    ]
    plan = dict(_plan_for(rows, ["cc/a"]))
    plan["needed_ids"] = ["cc/a", "cc/injected"]  # tamper without re-digesting
    snap = _snapshot([_row("cc/a", status="STALE")])
    cache = HealthCache(tmp_path / "health-cache.json")
    r = action_models_refresh_execute(
        confirmed=True, plan=plan, snapshot=snap, cache=cache, now=NOW
    )
    assert not r.ok and r.data["reason"] == "digest_mismatch"


def test_execute_refuses_on_expiry(tmp_path: Path) -> None:
    rows = [
        {
            "route_id": "cc/a",
            "provider": "cc",
            "status": "STALE",
            "capacity_class": CAPACITY_FREE,
            "refreshable": True,
        }
    ]
    plan = _plan_for(rows, ["cc/a"])
    snap = _snapshot([_row("cc/a", status="STALE")])
    cache = HealthCache(tmp_path / "health-cache.json")
    later = NOW + timedelta(seconds=121)
    r = action_models_refresh_execute(
        confirmed=True, plan=plan, snapshot=snap, cache=cache, now=later
    )
    assert not r.ok and r.data["reason"] == "plan_expired"


def test_execute_refuses_on_changed_identity(tmp_path: Path) -> None:
    rows = [
        {
            "route_id": "cc/a",
            "provider": "cc",
            "status": "STALE",
            "capacity_class": CAPACITY_FREE,
            "refreshable": True,
        }
    ]
    plan = _plan_for(rows, ["cc/a"])
    # Capacity changed since planning.
    snap = _snapshot([_row("cc/a", status="STALE", capacity=CAPACITY_SUBSCRIPTION)])
    cache = HealthCache(tmp_path / "health-cache.json")
    r = action_models_refresh_execute(
        confirmed=True, plan=plan, snapshot=snap, cache=cache, now=NOW
    )
    assert not r.ok and r.data["reason"] == "identity_changed"


def test_execute_confirmed_runs_within_two_requests_per_route(tmp_path: Path) -> None:
    rows = [
        {
            "route_id": "free/a",
            "provider": "free",
            "status": "STALE",
            "capacity_class": CAPACITY_FREE,
            "refreshable": True,
        }
    ]
    plan = _plan_for(rows, ["free/a"])
    # The plan is digest-bound to gateway "http://gw" and generation "gen1"
    # (see ``_plan_for``). Finding 3: execute re-reads the CURRENT endpoint and
    # generation and refuses a missing/mismatched binding, so the snapshot must
    # carry the same gateway/generation for a valid execute.
    snap = RefreshSnapshot(
        rows=(_row("free/a", status="STALE"),), generation="gen1", gateway_origin="http://gw"
    )
    cache = HealthCache(tmp_path / "health-cache.json")
    transport = ScriptedTransport({})
    r = action_models_refresh_execute(
        confirmed=True,
        plan=plan,
        snapshot=snap,
        cache=cache,
        now=NOW,
        transport=transport,
        clock=lambda: NOW,
        monotonic=lambda: 0.0,
        sleep=lambda _s: None,
        lock_path=tmp_path / "x.lock",
        marker_path=tmp_path / "x.json",
    )
    assert r.ok
    assert r.data["requests_made"] <= 2 * 1
    assert r.data["verified"] == 1


def test_module_level_refresh_for_consumer_wrapper(tmp_path: Path) -> None:
    transport = ScriptedTransport({})
    cache = HealthCache(tmp_path / "health-cache.json")
    out = refresh_for_consumer(
        _snapshot([_row("free/a")]),
        consumer="verified_view",
        needed_ids=["free/a"],
        config=RefreshConfig(),
        cache=cache,
        transport=transport,
        clock=lambda: NOW,
        monotonic=lambda: 0.0,
        sleep=lambda _s: None,
        lock_path=tmp_path / "refresh.lock",
        marker_path=tmp_path / "refresh.json",
    )
    assert out.outcome == OUTCOME_COMPLETED
    assert out.verified == 1


def test_route_outcome_and_probe_result_imports() -> None:
    # Sanity: the public types used by unit 2 integrate cleanly.
    assert RouteOutcome("x/y", "x", False, False).route_id == "x/y"
    r = ProbeResult(category=CATEGORY_OK, chat_ok=True, tool_ok=True)
    assert r.healthy
    assert usable_until(NOW) == NOW + timedelta(seconds=1800)
    assert DEFAULT_MAX_REQUESTS == 40
    assert REQUESTS_PER_FULL_PROBE == 2


# ---------------------------------------------------------------------------
# BOD-297 / S05 phase 1: cross-producer qualification budget, scope, fairness
# ---------------------------------------------------------------------------

SCOPE_A = "gateway=http://a/profile=x/consent=manual"
SCOPE_B = "gateway=http://b/profile=x/consent=manual"


def _scope_snapshot(
    rows: Sequence[RowInput], *, scope: str, generation: str = "rev1"
) -> RefreshSnapshot:
    return RefreshSnapshot(rows=tuple(rows), generation=generation, scope=scope)


def _scope_owner(
    directory: Path,
    transport: Callable[[str, str, float], ProbeExchange] | None,
    *,
    clock: Callable[[], datetime] | None = None,
) -> RefreshCoordinator:
    directory.mkdir(parents=True, exist_ok=True)
    return RefreshCoordinator(
        cache=HealthCache(directory / "health-cache.json"),
        transport=transport,
        clock=clock or (lambda: NOW),
        monotonic=lambda: 0.0,
        sleep=lambda _s: None,
        lock_path=directory / "refresh.lock",
        marker_path=directory / "refresh.json",
    )


def _fork_scope_owner(
    lock_path: Path,
    marker_path: Path,
    cache_path: Path,
    scope: str,
    started: Any,
    release: Any,
    result_q: Any,
) -> None:
    """A real second process running one owned job under the shared flock."""
    try:

        def transport(route_id: str, phase: str, timeout: float) -> ProbeExchange:
            started.set()
            release.wait(timeout=1.0)
            return _ok(route_id, phase)

        coord = RefreshCoordinator(
            cache=HealthCache(cache_path),
            transport=transport,
            lock_path=lock_path,
            marker_path=marker_path,
        )
        rows = [_row("cc/a"), _row("cc/b")]
        coord.refresh_for_consumer(
            _scope_snapshot(rows, scope=scope),
            consumer="picker",
            needed_ids=["cc/a", "cc/b"],
            config=RefreshConfig(wall_seconds=20.0),
            explicit=True,
        )
        result_q.put("done")
    except BaseException as exc:  # reported to the parent through the queue
        result_q.put(f"error: {exc!r}")


def test_cross_producer_qualification_budget_scope_crash_and_fairness(tmp_path: Path) -> None:
    """BOD-297/S05 phase 1: shared budgets, stable scope, caps before screening.

    Phase 1 lands the coordinator substrate every producer (manual refresh,
    census, live confirm) converges onto: one flock/marker owner, maintenance
    caps applied before any screening call, a stable configuration/profile/
    consent scope binding debounce and join reuse (a different gateway/profile
    never reuses outcomes; own evidence revisions do not invalidate the
    scope), and a real second process sharing the flock. Crash charge/refund,
    concurrency lanes, renewal slots and the durable backlog cursor follow
    with the S03 record shapes in phase 2.
    """
    import multiprocessing

    from verdict.orchestration.verified_refresh import (
        OUTCOME_LOCK_TIMEOUT,
        REASON_LOCK_TIMEOUT,
        maintenance_from_env,
    )

    # -- configuration knobs (A09 maintenance lane + qualification bounds) --
    base = config_from_env({})
    assert base.output_tokens == 4096
    assert base.request_timeout_seconds == 30.0
    assert base.max_per_provider is None  # interactive: no per-provider cap
    assert base.cooldown_seconds == 300.0
    assert base.maintenance is False
    bounded = maintenance_from_env({})
    assert bounded.maintenance is True
    assert bounded.max_routes == 4
    assert bounded.max_requests == 16
    assert bounded.wall_seconds == 600.0
    assert bounded.concurrency == 4
    assert bounded.debounce_seconds == 10.0
    # Env keeps precedence and the hard-max guard style.
    assert config_from_env({"VERDICT_REFRESH_MAX_PER_PROVIDER": "3"}).max_per_provider == 3
    with pytest.raises(RefreshConfigError):
        config_from_env({"VERDICT_REFRESH_MAX_PER_PROVIDER": "0"})
    with pytest.raises(RefreshConfigError):
        config_from_env({"VERDICT_REFRESH_OUTPUT_TOKENS": "0"})
    with pytest.raises(RefreshConfigError):
        maintenance_from_env({"VERDICT_REFRESH_MAX_ROUTES": "51"})
    assert bounded.max_per_provider == 2
    with pytest.raises(RefreshConfigError):
        RefreshConfig(wall_seconds=600.0)
    assert RefreshConfig(wall_seconds=600.0, maintenance=True).wall_seconds == 600.0

    # -- 9,000 fake ids: caps apply before any screening call (A12) --
    caps_dir = tmp_path / "caps"
    rows = [
        RowInput(
            route_id=f"p{i % 900}/m{i}",
            provider=f"p{i % 900}",
            status="UNVERIFIED",
            capacity_class=CAPACITY_FREE,
            refreshable=True,
        )
        for i in range(9_000)
    ]
    manual_ids = [r.route_id for r in rows[:100]]
    t_manual = ScriptedTransport({})
    manual = _scope_owner(caps_dir, t_manual)
    out = manual.refresh_for_consumer(
        _scope_snapshot(rows, scope=SCOPE_A),
        consumer="picker",
        needed_ids=manual_ids,
        config=bounded,
        explicit=True,
    )
    assert out.probed == 4
    assert out.requests_made == 8  # 4 routes x 2 full-probe requests
    assert out.requests_made <= bounded.max_requests
    assert len(t_manual.calls) == 8
    assert {rid for rid, _phase in t_manual.calls} == {
        rid for rid in manual_ids if rid in out.route_outcomes and out.route_outcomes[rid].probed
    }
    manual_probed = {rid for rid, o in out.route_outcomes.items() if o.probed}

    # -- census and confirm triggers join the SAME owner/budgets (A12) --
    census_ids = [r.route_id for r in rows[100:200]]
    t_census = ScriptedTransport({})
    census = _scope_owner(caps_dir, t_census, clock=lambda: NOW + timedelta(seconds=30))
    out_census = census.refresh_for_consumer(
        _scope_snapshot(rows, scope=SCOPE_A, generation="rev2"),
        consumer="census",
        needed_ids=census_ids,
        config=bounded,
    )
    assert out_census.probed == 4
    assert out_census.requests_made <= bounded.max_requests
    census_probed = {rid for rid, o in out_census.route_outcomes.items() if o.probed}
    assert census_probed.isdisjoint(manual_probed)

    confirm_ids = [r.route_id for r in rows[200:300]]
    t_confirm = ScriptedTransport({})
    confirm = _scope_owner(caps_dir, t_confirm, clock=lambda: NOW + timedelta(seconds=60))
    out_confirm = confirm.refresh_for_consumer(
        _scope_snapshot(rows, scope=SCOPE_A, generation="rev3"),
        consumer="admit_confirm",
        needed_ids=confirm_ids,
        config=bounded,
        authorized=True,
    )
    assert out_confirm.probed == 4
    assert out_confirm.requests_made <= bounded.max_requests
    confirm_probed = {rid for rid, o in out_confirm.route_outcomes.items() if o.probed}
    assert confirm_probed.isdisjoint(manual_probed | census_probed)

    # -- stable scope: debounce binds scope, not evidence revision (A13) --
    # The marker now holds the confirm job (scope A, confirm_ids, finished at
    # NOW+60). A census trigger 3 s later with a NEW evidence revision but the
    # SAME scope debounces: own evidence updates do not invalidate the scope.
    t_again = ScriptedTransport({})
    again = _scope_owner(caps_dir, t_again, clock=lambda: NOW + timedelta(seconds=63))
    out_again = again.refresh_for_consumer(
        _scope_snapshot(rows, scope=SCOPE_A, generation="rev9"),
        consumer="census",
        needed_ids=confirm_ids[:4],
        config=bounded,
    )
    assert out_again.outcome == OUTCOME_DEBOUNCED
    assert t_again.calls == []

    # A different gateway/profile within the same window, same ids, never
    # reuses outcomes: only the stable scope differs.
    t_other = ScriptedTransport({})
    other = _scope_owner(caps_dir, t_other, clock=lambda: NOW + timedelta(seconds=64))
    out_other = other.refresh_for_consumer(
        _scope_snapshot(rows, scope=SCOPE_B, generation="rev9"),
        consumer="census",
        needed_ids=confirm_ids,
        config=bounded,
    )
    assert out_other.outcome != OUTCOME_DEBOUNCED
    assert out_other.outcome == OUTCOME_CAPPED  # its own bounded job, route cap labelled
    assert out_other.requests_made == 8
    assert t_other.calls  # it ran its own job

    # External epoch change (scope moved back) also never reuses outcomes.
    t_back = ScriptedTransport({})
    back = _scope_owner(caps_dir, t_back, clock=lambda: NOW + timedelta(seconds=95))
    out_back = back.refresh_for_consumer(
        _scope_snapshot(rows, scope=SCOPE_A, generation="rev9"),
        consumer="census",
        needed_ids=confirm_ids,
        config=bounded,
    )
    assert out_back.outcome == OUTCOME_CAPPED  # again its own job, never a reuse
    assert t_back.calls

    # -- per-provider fairness: at most 2 routes per provider (A14) --
    fair_dir = tmp_path / "fairness"
    t_fair = ScriptedTransport({})
    fair = _scope_owner(fair_dir, t_fair)
    five = [_row(f"cc/r{i}") for i in range(5)]
    out_fair = fair.refresh_for_consumer(
        _scope_snapshot(five, scope=SCOPE_A),
        consumer="picker",
        needed_ids=[r.route_id for r in five],
        config=RefreshConfig(max_routes=10, max_per_provider=2),
        explicit=True,
    )
    assert out_fair.probed == 2
    assert len(t_fair.calls) == 4

    # -- two real local processes share one flock/marker (A12, A13) --
    procs_dir = tmp_path / "procs"
    procs_dir.mkdir(parents=True, exist_ok=True)
    lock_path = procs_dir / "refresh.lock"
    marker_path = procs_dir / "refresh.json"
    cache_path = procs_dir / "health-cache.json"
    ctx = multiprocessing.get_context("fork")

    started = ctx.Event()
    release = ctx.Event()
    result_q = ctx.Queue()
    child = ctx.Process(
        target=_fork_scope_owner,
        args=(lock_path, marker_path, cache_path, SCOPE_A, started, release, result_q),
    )
    child.start()
    assert started.wait(timeout=10.0)
    t_join = ScriptedTransport({})
    joiner = RefreshCoordinator(
        cache=HealthCache(cache_path),
        transport=t_join,
        lock_path=lock_path,
        marker_path=marker_path,
    )
    out_join = joiner.refresh_for_consumer(
        _scope_snapshot([_row("cc/a"), _row("cc/b")], scope=SCOPE_A),
        consumer="census",
        needed_ids=["cc/a", "cc/b"],
        config=RefreshConfig(wall_seconds=10.0),
    )
    release.set()
    child.join(timeout=30.0)
    assert result_q.get(timeout=5.0) == "done"
    assert child.exitcode == 0
    assert out_join.outcome == OUTCOME_JOINED
    assert t_join.calls == []  # the joiner never probed anything itself
    assert out_join.route_outcomes["cc/a"].probed is True

    # A different-scope caller under the SAME live owner never joins or
    # reuses its outcomes: bounded, fail-closed, zero calls.
    procs_dir2 = tmp_path / "procs2"
    procs_dir2.mkdir(parents=True, exist_ok=True)
    started2 = ctx.Event()
    release2 = ctx.Event()
    result_q2 = ctx.Queue()
    child2 = ctx.Process(
        target=_fork_scope_owner,
        args=(
            procs_dir2 / "refresh.lock",
            procs_dir2 / "refresh.json",
            procs_dir2 / "health-cache.json",
            SCOPE_A,
            started2,
            release2,
            result_q2,
        ),
    )
    child2.start()
    assert started2.wait(timeout=10.0)
    t_mismatch = ScriptedTransport({})
    mismatch = RefreshCoordinator(
        cache=HealthCache(procs_dir2 / "health-cache.json"),
        transport=t_mismatch,
        lock_path=procs_dir2 / "refresh.lock",
        marker_path=procs_dir2 / "refresh.json",
    )
    out_mismatch = mismatch.refresh_for_consumer(
        _scope_snapshot([_row("cc/a"), _row("cc/b")], scope=SCOPE_B),
        consumer="admit_confirm",
        needed_ids=["cc/a", "cc/b"],
        config=RefreshConfig(wall_seconds=10.0),
        authorized=True,
    )
    release2.set()
    child2.join(timeout=30.0)
    assert result_q2.get(timeout=5.0) == "done"
    assert out_mismatch.outcome == OUTCOME_LOCK_TIMEOUT
    assert t_mismatch.calls == []
    for route in out_mismatch.route_outcomes.values():
        assert route.refresh_reason == REASON_LOCK_TIMEOUT
        assert route.verified is False  # the other scope's proof is never reused


# ---------------------------------------------------------------------------
# BOD-297 / S05 phase 1 review fixes
# ---------------------------------------------------------------------------


def _hold_lock(path: Path) -> Any:
    import fcntl

    held = path.open("a+")
    fcntl.flock(held.fileno(), fcntl.LOCK_EX)
    return held


def _marker_payload(
    job_id: str, scope: str, *, running: bool, seq: int = 1, ids: Sequence[str] = ("cc/a",)
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "job_id": job_id,
        "owner": "picker",
        "scope": scope,
        "ids": list(ids),
        "running": running,
        "sequence": seq,
        "outcomes": {
            rid: {"provider": rid.split("/")[0], "probed": True, "verified": True, "alive": True}
            for rid in ids
        }
        if not running
        else {},
    }
    if not running:
        payload["finished_wall"] = 0.0
    return payload


def _write_marker_file(path: Path, payload: dict[str, Any]) -> None:
    import json

    path.write_text(json.dumps(payload), encoding="utf-8")


def test_empty_scope_is_gateway_derived_never_matches_across_gateways(tmp_path: Path) -> None:
    rows = [_row("cc/a"), _row("cc/b")]

    def snap(origin: str) -> RefreshSnapshot:
        return RefreshSnapshot(rows=tuple(rows), generation="g", gateway_origin=origin)

    t1 = ScriptedTransport({})
    first = _coordinator(tmp_path, t1)
    first.refresh_for_consumer(
        snap("http://gw-a:20128"),
        consumer="picker",
        needed_ids=["cc/a", "cc/b"],
        explicit=True,
        config=RefreshConfig(),
    )
    assert t1.calls

    # Different gateway, same window: never debounced onto gateway A's outcomes.
    t2 = ScriptedTransport({})
    second = _coordinator(tmp_path, t2, clock=lambda: NOW + timedelta(seconds=1))
    out = second.refresh_for_consumer(
        snap("http://gw-b:20128"),
        consumer="picker",
        needed_ids=["cc/a", "cc/b"],
        explicit=True,
        config=RefreshConfig(),
    )
    assert out.outcome != OUTCOME_DEBOUNCED
    assert t2.calls

    # Same gateway still debounces (the fix must not break own-scope reuse).
    t3 = ScriptedTransport({})
    third = _coordinator(tmp_path, t3, clock=lambda: NOW + timedelta(seconds=2))
    again = third.refresh_for_consumer(
        snap("http://gw-b:20128"),
        consumer="picker",
        needed_ids=["cc/a", "cc/b"],
        explicit=True,
        config=RefreshConfig(),
    )
    assert again.outcome == OUTCOME_DEBOUNCED
    assert t3.calls == []


def test_empty_scope_join_never_reuses_another_gateways_outcomes(tmp_path: Path) -> None:
    from verdict.orchestration.verified_refresh import OUTCOME_LOCK_TIMEOUT

    rows = [_row("cc/a"), _row("cc/b")]
    owner = _coordinator(tmp_path, ScriptedTransport({}))
    owner.refresh_for_consumer(
        RefreshSnapshot(rows=tuple(rows), gateway_origin="http://gw-a:20128"),
        consumer="picker",
        needed_ids=["cc/a", "cc/b"],
        explicit=True,
        config=RefreshConfig(),
    )
    held = _hold_lock(tmp_path / "refresh.lock")
    try:
        t = ScriptedTransport({})
        joiner = _coordinator(tmp_path, t, clock=lambda: NOW + timedelta(seconds=30))
        out = joiner.refresh_for_consumer(
            RefreshSnapshot(rows=tuple(rows), gateway_origin="http://gw-b:20128"),
            consumer="picker",
            needed_ids=["cc/a", "cc/b"],
            explicit=True,
            config=RefreshConfig(wall_seconds=1.0),
        )
    finally:
        held.close()
    assert out.outcome == OUTCOME_LOCK_TIMEOUT
    assert t.calls == []
    assert all(not r.verified for r in out.route_outcomes.values())


def test_join_waits_past_stale_other_scope_finished_marker(tmp_path: Path) -> None:
    marker_path = tmp_path / "refresh.json"
    _write_marker_file(marker_path, _marker_payload("old-b", SCOPE_B, running=False))
    polls = {"n": 0}

    def sleep(_s: float) -> None:
        polls["n"] += 1
        if polls["n"] == 2:
            _write_marker_file(marker_path, _marker_payload("job-a", SCOPE_A, running=True))
        elif polls["n"] == 4:
            _write_marker_file(marker_path, _marker_payload("job-a", SCOPE_A, running=False, seq=2))

    t = ScriptedTransport({})
    joiner = _coordinator(tmp_path, t)
    joiner.sleep = sleep
    held = _hold_lock(tmp_path / "refresh.lock")
    try:
        out = joiner.refresh_for_consumer(
            _scope_snapshot([_row("cc/a")], scope=SCOPE_A),
            consumer="picker",
            needed_ids=["cc/a"],
            explicit=True,
            config=RefreshConfig(wall_seconds=10.0),
        )
    finally:
        held.close()
    assert out.outcome == OUTCOME_JOINED
    assert out.job_id == "job-a"
    assert out.route_outcomes["cc/a"].probed is True
    assert t.calls == []


def test_joined_job_survives_a_later_foreign_scope_marker(tmp_path: Path) -> None:
    from verdict.orchestration.verified_refresh import OUTCOME_LOCK_TIMEOUT

    marker_path = tmp_path / "refresh.json"
    _write_marker_file(marker_path, _marker_payload("job-a", SCOPE_A, running=True))
    polls = {"n": 0}

    def sleep(_s: float) -> None:
        polls["n"] += 1
        if polls["n"] == 1:
            _write_marker_file(marker_path, _marker_payload("job-b", SCOPE_B, running=True))

    joiner = _coordinator(tmp_path, ScriptedTransport({}))
    joiner.sleep = sleep
    held = _hold_lock(tmp_path / "refresh.lock")
    try:
        out = joiner.refresh_for_consumer(
            _scope_snapshot([_row("cc/a")], scope=SCOPE_A),
            consumer="picker",
            needed_ids=["cc/a"],
            explicit=True,
            config=RefreshConfig(wall_seconds=10.0),
        )
    finally:
        held.close()
    # Reported as the joined job, still running/unfinished -- never as a mismatch
    # and never carrying the foreign owner's job id.
    assert out.outcome == OUTCOME_LOCK_TIMEOUT
    assert out.job_id == "job-a"
    assert out.complete is False


def test_scope_mismatch_does_not_leak_foreign_job_id(tmp_path: Path) -> None:
    from verdict.orchestration.verified_refresh import OUTCOME_LOCK_TIMEOUT

    marker_path = tmp_path / "refresh.json"
    _write_marker_file(marker_path, _marker_payload("foreign-job", SCOPE_B, running=True))
    joiner = _coordinator(tmp_path, ScriptedTransport({}))
    held = _hold_lock(tmp_path / "refresh.lock")
    try:
        out = joiner.refresh_for_consumer(
            _scope_snapshot([_row("cc/a")], scope=SCOPE_A),
            consumer="picker",
            needed_ids=["cc/a"],
            explicit=True,
            config=RefreshConfig(wall_seconds=10.0),
        )
    finally:
        held.close()
    assert out.outcome == OUTCOME_LOCK_TIMEOUT
    assert out.job_id != "foreign-job"


@pytest.mark.parametrize(
    "env",
    [
        {"VERDICT_REFRESH_MAX_ROUTES": "5"},
        {"VERDICT_REFRESH_MAX_REQUESTS": "17"},
        {"VERDICT_REFRESH_WALL_SECONDS": "601"},
        {"VERDICT_REFRESH_CONCURRENCY": "5"},
        {"VERDICT_REFRESH_OUTPUT_TOKENS": "4097"},
        {"VERDICT_REFRESH_MAX_PER_PROVIDER": "3"},
    ],
)
def test_maintenance_env_can_never_exceed_contract_caps(env: dict[str, str]) -> None:
    from verdict.orchestration.verified_refresh import maintenance_from_env

    with pytest.raises(RefreshConfigError):
        maintenance_from_env(env)


def test_maintenance_env_may_lower_caps() -> None:
    from verdict.orchestration.verified_refresh import maintenance_from_env

    lowered = maintenance_from_env(
        {
            "VERDICT_REFRESH_MAX_ROUTES": "2",
            "VERDICT_REFRESH_MAX_REQUESTS": "8",
            "VERDICT_REFRESH_OUTPUT_TOKENS": "1024",
            "VERDICT_REFRESH_MAX_PER_PROVIDER": "1",
        }
    )
    assert (lowered.max_routes, lowered.max_requests) == (2, 8)
    assert (lowered.output_tokens, lowered.max_per_provider) == (1024, 1)


def test_per_provider_cap_normalizes_provider_names() -> None:
    providers = ["OpenAI", "openai", "OpenAI ", "OPENAI"]
    rows = [_row(f"x/m{i}", provider=p) for i, p in enumerate(providers)]
    picked = select_candidates(
        _snapshot(rows),
        needed_ids=[r.route_id for r in rows],
        explicit=True,
        config=RefreshConfig(max_per_provider=2),
    )
    assert len(picked) == 2


def test_interactive_has_no_per_provider_cap_but_maintenance_does() -> None:
    from verdict.orchestration.verified_refresh import maintenance_from_env

    assert RefreshConfig().max_per_provider is None
    assert config_from_env({}).max_per_provider is None
    assert maintenance_from_env({}).max_per_provider == 2
    rows = [_row(f"cc/r{i}") for i in range(5)]
    picked = select_candidates(
        _snapshot(rows),
        needed_ids=[r.route_id for r in rows],
        explicit=True,
        config=RefreshConfig(max_routes=10),
    )
    assert len(picked) == 5


# ---------------------------------------------------------------------------
# BOD-297/S05-D: concurrency lanes
# ---------------------------------------------------------------------------


class InFlightTransport:
    """Records how many probes overlap, overall and per credential pool."""

    def __init__(self, hold: float = 0.0, script: dict[str, ProbeExchange] | None = None):
        self.hold = hold
        self.script = script or {}
        self.lock = threading.Lock()
        self.calls: list[tuple[str, str]] = []
        self.in_flight: dict[str, int] = {}
        self.max_total = 0
        self.max_per_pool: dict[str, int] = {}
        self.total_at_call: dict[str, int] = {}

    def __call__(self, route_id: str, phase: str, timeout: float) -> ProbeExchange:
        from verdict.orchestration.credential_pools import pool_of

        pool = pool_of(route_id)
        with self.lock:
            self.calls.append((route_id, phase))
            self.in_flight[pool] = self.in_flight.get(pool, 0) + 1
            total = sum(self.in_flight.values())
            self.total_at_call[route_id] = max(self.total_at_call.get(route_id, 0), total)
            self.max_total = max(self.max_total, total)
            self.max_per_pool[pool] = max(self.max_per_pool.get(pool, 0), self.in_flight[pool])
        try:
            if self.hold:
                import time

                time.sleep(self.hold)
            return self.script.get(route_id) or _ok(route_id, phase)
        finally:
            with self.lock:
                self.in_flight[pool] -= 1


class _BarrierChatTransport:
    """Every chat call waits on a shared ``threading.Barrier(parties=4,
    timeout=0.5)`` before returning, so the test can tell, deterministically,
    how many probes the coordinator actually let run together.

    If 4 chat calls truly arrive together, the barrier releases them all at
    once and every one of them observes the full set in flight: the shared
    counters below (max_total) become exact, not a timing race. If only 3
    (or fewer) chat calls ever arrive, the barrier times out after 0.5 s and
    those callers proceed via ``BrokenBarrierError``, having correctly never
    seen a 4th caller join. Either way max_total reports the true number of
    chat calls that overlapped, so the test can assert "no 4th probe admits
    next to 3 in flight" and also prove (by raising the cap so a 4th really
    is allowed) that this transport would have caught it if it happened.

    The barrier sits on the chat phase only: each full probe makes a chat
    call and then a tool call (REQUESTS_PER_FULL_PROBE == 2), and blocking
    the tool call too would not add any extra overlap guarantee while making
    the barrier arithmetic (which phase, which call) harder to reason about.
    """

    def __init__(self, parties: int = 4, timeout: float = 0.5):
        self.lock = threading.Lock()
        self.calls: list[tuple[str, str]] = []
        self.in_flight: dict[str, int] = {}
        self.max_total = 0
        self.max_per_pool: dict[str, int] = {}
        self.total_at_call: dict[str, int] = {}
        self._barrier = threading.Barrier(parties, timeout=timeout)

    def __call__(self, route_id: str, phase: str, timeout: float) -> ProbeExchange:
        from verdict.orchestration.credential_pools import pool_of

        pool = pool_of(route_id)
        with self.lock:
            self.calls.append((route_id, phase))
            self.in_flight[pool] = self.in_flight.get(pool, 0) + 1
            total = sum(self.in_flight.values())
            self.total_at_call[route_id] = max(self.total_at_call.get(route_id, 0), total)
            self.max_total = max(self.max_total, total)
            self.max_per_pool[pool] = max(self.max_per_pool.get(pool, 0), self.in_flight[pool])
        try:
            if phase == "chat":
                with contextlib.suppress(threading.BrokenBarrierError):
                    self._barrier.wait()
            return _ok(route_id, phase)
        finally:
            with self.lock:
                self.in_flight[pool] -= 1


def test_probes_run_four_at_once_one_per_pool(tmp_path: Path) -> None:
    import time

    transport = InFlightTransport(hold=0.25)
    rows = [_row(f"p{i}/m") for i in range(8)]
    coord = _coordinator(tmp_path, transport)
    started = time.monotonic()
    out = coord.refresh_for_consumer(
        _snapshot(rows),
        consumer="verified_view",
        needed_ids=[r.route_id for r in rows],
        config=RefreshConfig(concurrency=4),
    )
    elapsed = time.monotonic() - started
    assert out.outcome == OUTCOME_COMPLETED and out.verified == 8
    assert transport.max_total == 4
    # 8 routes x 2 calls x 0.25 s is 4 s one at a time; two waves of 4 take 1 s.
    assert elapsed < 2.5


def test_one_probe_per_pool_including_aliases(tmp_path: Path) -> None:
    transport = InFlightTransport(hold=0.05)
    rows = [_row(f"cc/m{i}") for i in range(3)] + [
        _row("claude/m9"),
        _row("agy/x"),
        _row("antigravity/y"),
    ]
    coord = _coordinator(tmp_path, transport)
    out = coord.refresh_for_consumer(
        _snapshot(rows),
        consumer="verified_view",
        needed_ids=[r.route_id for r in rows],
        config=RefreshConfig(concurrency=4),
    )
    assert out.verified == 6
    assert set(transport.max_per_pool.values()) == {1}
    assert transport.max_total == 2  # the claude and antigravity pools overlap each other


def test_unknown_pool_probe_runs_alone(tmp_path: Path) -> None:
    transport = InFlightTransport(hold=0.05)
    rows = [_row(f"p{i}/m", capacity=CAPACITY_SUBSCRIPTION) for i in range(3)]
    rows.insert(1, _row("u/x", capacity="unknown"))
    coord = _coordinator(tmp_path, transport)
    out = coord.refresh_for_consumer(
        _snapshot(rows),
        consumer="models_refresh",
        needed_ids=[r.route_id for r in rows],
        config=RefreshConfig(concurrency=4),
        authorized=True,
    )
    assert ("u/x", "chat") in transport.calls
    assert transport.total_at_call["u/x"] == 1
    assert transport.max_total >= 2  # the known pools still overlap each other
    assert out.requests_made == 3 * 2 + 1


def test_concurrent_requests_never_exceed_the_cap(tmp_path: Path) -> None:
    # Every chat call waits on a shared Barrier(parties=4, timeout=0.5) (see
    # _BarrierChatTransport): if the coordinator ever let a 4th probe start
    # its chat call next to 3 already in flight, all 4 would release the
    # barrier together and max_total would report 4. With cap 6 (3 probes
    # fit: 3 x 2 requests = 6), only 3 chat calls ever arrive, the barrier
    # times out at 0.5 s, and max_total deterministically reports 3.
    transport = _BarrierChatTransport(parties=4, timeout=0.5)
    rows = [_row(f"p{i}/m") for i in range(8)]
    events: list[Any] = []
    coord = _coordinator(tmp_path, transport)
    out = coord.refresh_for_consumer(
        _snapshot(rows),
        consumer="verified_view",
        needed_ids=[r.route_id for r in rows],
        config=RefreshConfig(concurrency=4, max_requests=6),
        on_progress=events.append,
    )
    # requests_made == len(transport.calls) == 6 is the timing-independent
    # check: a 4th admitted probe makes 2 more transport calls regardless of
    # how fast or slow it runs, so this assertion alone would already fail
    # if a 4th probe were ever admitted next to 3 in flight.
    assert out.requests_made == len(transport.calls) == 6
    assert out.cap_reason == REASON_REQUEST_CAP
    # Requests reserved by in-flight probes count against the cap: a 4th probe
    # is never admitted next to 3 in flight.
    assert transport.max_total == 3
    assert all(e.requests_reserved <= 6 for e in events)


def test_the_barrier_transport_would_catch_a_4th_probe_admitted(tmp_path: Path) -> None:
    # Mutation check: raising the cap to 8 lets a 4th probe start next to the
    # first 3 in flight. This proves _BarrierChatTransport actually detects
    # that case (max_total == 4), so its "3" result above is not a transport
    # that always reports 3 regardless of what the coordinator does.
    transport = _BarrierChatTransport(parties=4, timeout=0.5)
    rows = [_row(f"p{i}/m") for i in range(8)]
    coord = _coordinator(tmp_path, transport)
    out = coord.refresh_for_consumer(
        _snapshot(rows),
        consumer="verified_view",
        needed_ids=[r.route_id for r in rows],
        config=RefreshConfig(concurrency=4, max_requests=8),
    )
    assert transport.max_total == 4
    assert out.requests_made == len(transport.calls) == 8


def test_confirmed_outage_drops_every_in_flight_probe(tmp_path: Path) -> None:
    release = threading.Event()
    gate_calls: list[tuple[str, int | None, int]] = []

    def transport(route_id: str, phase: str, timeout: float) -> ProbeExchange:
        if route_id == "p0/m":
            return _fail(401)
        release.wait(timeout=5.0)
        return _fail(401) if route_id == "p3/m" else _ok(route_id, phase)

    def auth_gate(route_id: str, http_status: int | None, remaining: int) -> tuple[bool, int]:
        gate_calls.append((route_id, http_status, remaining))
        release.set()
        return True, 0

    recorder = ScriptedTransport({})

    def recording(route_id: str, phase: str, timeout: float) -> ProbeExchange:
        recorder(route_id, phase, timeout)
        return transport(route_id, phase, timeout)

    coord = _coordinator(tmp_path, recording)
    coord.auth_gate = auth_gate
    rows = [_row(f"p{i}/m", rank_hint=i) for i in range(5)]
    out = coord.refresh_for_consumer(
        _snapshot(rows),
        consumer="verified_view",
        needed_ids=[r.route_id for r in rows],
        config=RefreshConfig(concurrency=4, max_requests=8),
    )
    # p0 spent 1 request; p1..p3 hold 2 reserved each, so 8 - 1 - 6 = 1 remains.
    assert gate_calls == [("p0/m", 401, 1)]
    assert out.cap_reason == "auth_outage"
    assert "p4/m" not in recorder.routes_called()  # nothing admitted after it
    assert all(not o.probed for o in out.route_outcomes.values())
    assert out.requests_made <= 8
    assert HealthCache(tmp_path / "health-cache.json").routes() == {}


def test_a_raising_probe_is_recorded_and_the_job_continues(tmp_path: Path) -> None:
    import json

    from verdict.orchestration.verified_refresh import REASON_PROBE_ERROR

    inner = InFlightTransport(hold=0.05)

    def transport(route_id: str, phase: str, timeout: float) -> ProbeExchange:
        if route_id == "p2/m":
            raise RuntimeError("transport bug")
        return inner(route_id, phase, timeout)

    rows = [_row(f"p{i}/m") for i in range(4)]
    coord = _coordinator(tmp_path, transport)
    out = coord.refresh_for_consumer(
        _snapshot(rows),
        consumer="verified_view",
        needed_ids=[r.route_id for r in rows],
        config=RefreshConfig(concurrency=4, max_requests=8),
    )
    assert out.outcome == OUTCOME_COMPLETED
    assert out.verified == 3
    assert out.route_outcomes["p2/m"].refresh_reason == REASON_PROBE_ERROR
    # Conservative: the raising probe's 2 reserved requests count as sent.
    assert out.requests_made == 3 * 2 + 2 <= 8
    marker = json.loads((tmp_path / "refresh.json").read_text())
    assert marker["running"] is False and marker["result"] == OUTCOME_COMPLETED
    cache = HealthCache(tmp_path / "health-cache.json")
    assert cache.entry("p2/m") is None
    assert all(cache.entry(f"p{i}/m") is not None for i in (0, 1, 3))
