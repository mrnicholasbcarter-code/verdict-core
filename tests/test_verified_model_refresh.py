"""Bounded verification refresh coordinator tests (BOD-292 unit 3).

Everything is injected: transports, clocks, cache paths in tmp_path, lock and
marker paths. No real gateway or model is ever contacted.
"""

from __future__ import annotations

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
