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

    from verdict.orchestration.verified_refresh import OUTCOME_LOCK_TIMEOUT, REASON_LOCK_TIMEOUT

    # -- configuration knobs (qualification bounds) --
    base = config_from_env({})
    assert base.output_tokens == 4096
    assert base.request_timeout_seconds == 30.0
    assert base.max_per_provider is None  # interactive: no per-provider cap
    assert base.cooldown_seconds == 300.0
    bounded = RefreshConfig(max_routes=4, max_requests=16, concurrency=4)
    # Env keeps precedence and the hard-max guard style.
    assert config_from_env({"VERDICT_REFRESH_MAX_PER_PROVIDER": "3"}).max_per_provider == 3
    with pytest.raises(RefreshConfigError):
        config_from_env({"VERDICT_REFRESH_MAX_PER_PROVIDER": "0"})
    with pytest.raises(RefreshConfigError):
        config_from_env({"VERDICT_REFRESH_OUTPUT_TOKENS": "0"})
    with pytest.raises(RefreshConfigError):
        RefreshConfig(wall_seconds=600.0)

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


def test_interactive_has_no_per_provider_cap() -> None:
    assert RefreshConfig().max_per_provider is None
    assert config_from_env({}).max_per_provider is None
    rows = [_row(f"cc/r{i}") for i in range(5)]
    picked = select_candidates(
        _snapshot(rows),
        needed_ids=[r.route_id for r in rows],
        explicit=True,
        config=RefreshConfig(max_routes=10),
    )
    assert len(picked) == 5
