"""eligibility-now: the agentic phase runs on its own budget and finds live free routes."""

from __future__ import annotations

import fcntl
import io
import json
import threading
import urllib.error
from datetime import datetime, timedelta, timezone
from email.message import Message
from pathlib import Path
from typing import Any

import pytest

from verdict.orchestration.health_cache import (
    AGENTIC_PROBE_TTL_SECONDS,
    FRESH_SECONDS,
    HealthCache,
    ProbeResult,
    agentic_capability,
)
from verdict.prove_at_rest import (
    AGENTIC_PROBE_FILE,
    AGENTIC_TOOL_EDIT,
    AGENTIC_TOOL_READ,
    DEFAULT_AGENTIC_INTERVAL_HOURS,
    AdmittedRoute,
    CycleStats,
    ProbeExchange,
    Prober,
    _http_error_exchange,
    agentic_candidates,
    order_cycle,
    routes_from_evidence,
)

NOW = datetime(2026, 10, 13, 3, 0, tzinfo=timezone.utc)
pytestmark = pytest.mark.usefixtures("no_gateway_network")

_SCRIPT = [
    (AGENTIC_TOOL_READ, {"path": AGENTIC_PROBE_FILE}),
    (
        AGENTIC_TOOL_EDIT,
        {"path": AGENTIC_PROBE_FILE, "old_text": "line two", "new_text": "LINE TWO"},
    ),
    (AGENTIC_TOOL_READ, {"path": AGENTIC_PROBE_FILE}),
]


def _turn(route_id: str, payload: dict[str, Any]) -> ProbeExchange:
    """A model that does the read/edit/read task correctly."""
    turn = sum(1 for message in payload["messages"] if message.get("role") == "user") - 1
    name, args = _SCRIPT[turn]
    body = {
        "model": route_id.split("/", 1)[1],
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": f"c{turn}",
                            "type": "function",
                            "function": {"name": name, "arguments": json.dumps(args)},
                        }
                    ],
                }
            }
        ],
    }
    return ProbeExchange(
        http_status=200, ok=True, latency_ms=10.0, response_body=body, reported_model=body["model"]
    )


def _free(route_id: str, **kwargs: Any) -> AdmittedRoute:
    return AdmittedRoute(route_id, route_id.split("/", 1)[0], "free", **kwargs)


def _dead_chat(route_id: str, phase: str, timeout: float) -> ProbeExchange:
    return ProbeExchange(http_status=500, ok=False, error_category="upstream")


def test_prove_at_rest_cycle_holds_the_shared_lease_and_obeys_maintenance_caps(
    tmp_path: Path,
) -> None:
    """BOD-297/S05-B: the chat phase is a bounded RefreshCoordinator client.

    Two real processes: one prove-at-rest ``run_once`` holding the shared
    flock (default coordinator paths beside the health cache), one
    ``RefreshCoordinator.refresh_for_consumer`` trying to join -- the second
    sees zero direct transport calls (lock_timeout/joined). The owning
    cycle's own bounded config (parent decision 2026-10-19: NOT census's 4
    routes/16 requests -- that would starve the daemon's fresh set; instead
    ``min(daemon max_requests // 2, 50)`` routes, ``min(daemon max_requests,
    100)`` requests) completes a 20-route cycle in one pass. Red today:
    run_once neither takes the flock nor goes through the coordinator at
    all.
    """
    import multiprocessing
    from datetime import datetime, timezone

    from verdict.orchestration.health_cache import HealthCache
    from verdict.orchestration.verified_refresh import (
        RefreshConfig,
        RefreshCoordinator,
        RefreshSnapshot,
        RowInput,
    )

    cache_path = tmp_path / "health.json"
    lock_path = cache_path.parent / "verified-refresh.lock"
    marker_path = cache_path.parent / "verified-refresh.json"
    cache_path.parent.mkdir(parents=True, exist_ok=True)

    routes = [_free(f"p{i}/m") for i in range(20)]
    now_local = datetime(2026, 10, 19, 0, 0, tzinfo=timezone.utc)

    def _run_owner(cache_path_str: str, started: Any, release: Any, result_q: Any) -> None:
        def transport(route_id: str, phase: str, timeout: float) -> ProbeExchange:
            started.set()
            release.wait(timeout=5.0)
            return ProbeExchange(http_status=200, ok=True, chat_exact=True, tool_called=True)

        try:
            prober = Prober(
                HealthCache(Path(cache_path_str)),
                lambda: routes,
                transport,
                clock=lambda: now_local,
            )
            stats = prober.run_once()
            result_q.put(stats.stopped_reason)
        except BaseException as exc:  # reported to the parent through the queue
            result_q.put(f"error: {exc!r}")

    ctx = multiprocessing.get_context("fork")
    started = ctx.Event()
    release = ctx.Event()
    result_q: Any = ctx.Queue()
    owner = ctx.Process(target=_run_owner, args=(str(cache_path), started, release, result_q))
    owner.start()
    try:
        assert started.wait(timeout=5.0)
        joiner_calls: list[tuple[str, str]] = []

        def joiner_transport(route_id: str, phase: str, timeout: float) -> ProbeExchange:
            joiner_calls.append((route_id, phase))
            return ProbeExchange(http_status=200, ok=True, chat_exact=True, tool_called=True)

        joiner = RefreshCoordinator(
            cache=HealthCache(cache_path),
            transport=joiner_transport,
            lock_path=lock_path,
            marker_path=marker_path,
        )
        out = joiner.refresh_for_consumer(
            RefreshSnapshot(rows=(RowInput("q/new", "q", "UNVERIFIED", "free", refreshable=True),)),
            consumer="prove_at_rest",
            needed_ids=["q/new"],
            config=RefreshConfig(wall_seconds=2.0),
            explicit=True,
        )
        assert joiner_calls == []
        assert out.outcome in {"lock_timeout", "joined"}
    finally:
        release.set()
        owner.join(timeout=5.0)
    assert result_q.get(timeout=5.0) == "complete"

    # -- second, lock-free cycle: the maintenance contract bounds requests --
    cache2 = HealthCache(tmp_path / "health2.json")
    chat_calls: list[tuple[str, str]] = []

    def chat_transport(route_id: str, phase: str, timeout: float) -> ProbeExchange:
        chat_calls.append((route_id, phase))
        return ProbeExchange(http_status=200, ok=True, chat_exact=True, tool_called=True)

    many_routes = [_free(f"r{i}/m") for i in range(20)]
    prober2 = Prober(cache2, lambda: many_routes, chat_transport, clock=lambda: now_local)
    stats2 = prober2.run_once()
    # Daemon default max_requests=300 -> config max_routes=min(150, 50)=50,
    # max_requests=min(300, 100)=100: a 20-route cycle completes in one pass.
    assert stats2.stopped_reason == "complete"
    assert len({rid for rid, _ in chat_calls}) == 20
    assert len(chat_calls) == 40


def test_outage_breaker_still_runs_under_a_tight_coordinator_cap(tmp_path: Path) -> None:
    """Companion to test_auth_outage.py's request-cap test (parent decision
    2026-10-19): with a cap wide enough for exactly one SUBSCRIPTION probe
    (2 requests), the coordinator-backed chat phase still runs the BOD-338
    breaker to completion -- the gateway check fires once, the outage is
    confirmed, and the cache stays unpoisoned.
    """
    from datetime import datetime, timezone

    from verdict.orchestration.health_cache import HealthCache

    now_local = datetime(2026, 10, 19, 0, 0, tzinfo=timezone.utc)
    cache = HealthCache(tmp_path / "cache.json")
    checks: list[float] = []

    def check(timeout: float) -> ProbeExchange:
        checks.append(timeout)
        return ProbeExchange(http_status=401, ok=False)

    routes = [AdmittedRoute("af/a", "af", "subscription")]
    prober = Prober(
        cache,
        lambda: routes,
        lambda *args: ProbeExchange(http_status=401, ok=False),
        clock=lambda: now_local,
        max_requests=2,
        gateway_auth_check=check,
    )
    stats = prober.run_once()
    assert stats.requests == 2
    assert len(checks) == 1
    assert stats.auth_outage
    assert stats.stopped_reason == "auth_outage"
    assert cache.routes() == {}


def test_auth_gate_denies_outage_excludes_route_from_same_cycle_agentic(tmp_path: Path) -> None:
    """BOD-297/S05-B (parent decision 2026-10-19, item 13): accepted as
    product behaviour -- the coordinator's auth_gate already confirms a
    401/403 is a real per-route failure before writing it, so retrying that
    SAME route with an agentic probe in the SAME cycle would only spend
    another request on a known failure. The negative is written immediately
    and the route never reaches run_agentic_probes' candidate list.
    """
    from datetime import datetime, timezone

    from verdict.orchestration.health_cache import HealthCache

    now_local = datetime(2026, 10, 19, 0, 0, tzinfo=timezone.utc)
    cache = HealthCache(tmp_path / "cache.json")
    routes = [_free("p/denied")]
    agentic_calls: list[str] = []

    def chat(route_id: str, phase: str, timeout: float) -> ProbeExchange:
        return ProbeExchange(http_status=401, ok=False, error_category="auth")

    def agentic(route_id: str, payload: object, timeout: float) -> ProbeExchange:
        agentic_calls.append(route_id)
        return ProbeExchange(http_status=200, ok=True)

    prober = Prober(
        cache=cache,
        routes_loader=lambda: routes,
        transport=chat,
        agentic_transport=agentic,
        clock=lambda: now_local,
        monotonic=lambda: 0.0,
        # No gateway_auth_check: the heuristic auth_gate denies outage
        # immediately for a single route (never reaches the threshold), so
        # the 401 is written as a real negative within this same cycle.
    )
    stats = prober.run_once()
    assert agentic_calls == []
    assert stats.negative == 1
    entry = cache.entry("p/denied")
    assert entry is not None and not entry.healthy


def test_unconfirmed_auth_gate_reports_budget_stop_not_outage(tmp_path: Path) -> None:
    """Review finding 18a (2026-10-19): when the gate cannot run its own
    confirmation (its request/wall budget is already spent), the job must
    report a budget stop (request_cap/wall_cap), never a CONFIRMED outage
    nobody actually checked. The failure is still never written, and the
    agentic phase still runs (only a confirmed outage skips it).
    """
    from datetime import datetime, timezone

    from verdict.orchestration.health_cache import HealthCache

    now_local = datetime(2026, 10, 19, 0, 0, tzinfo=timezone.utc)
    cache = HealthCache(tmp_path / "cache.json")
    tick = [0.0]

    def chat(route_id: str, phase: str, timeout: float) -> ProbeExchange:
        tick[0] += 100.0  # the chat call itself crosses the wall budget
        return ProbeExchange(http_status=401, ok=False, error_category="auth")

    checks: list[float] = []

    def check(timeout: float) -> ProbeExchange:
        checks.append(timeout)
        return ProbeExchange(http_status=200, ok=True)

    agentic_calls: list[str] = []

    def agentic(route_id: str, payload: object, timeout: float) -> ProbeExchange:
        agentic_calls.append(route_id)
        return ProbeExchange(http_status=200, ok=True)

    prober = Prober(
        cache,
        lambda: [_free("p/m")],
        chat,
        max_wall_seconds=50.0,
        agentic_transport=agentic,
        agentic_wall_seconds=1000.0,
        clock=lambda: now_local,
        monotonic=lambda: tick[0],
        gateway_auth_check=check,
        epsilon=0,
    )
    stats = prober.run_once()
    assert stats.auth_outage is False
    assert stats.stopped_reason == "wall_cap"
    assert checks == []
    # The chat phase's unconfirmed 401 is never written; the agentic phase
    # still runs (only a confirmed outage skips it) and is free to write its
    # own result.
    entry = cache.entry("p/m")
    assert entry is not None and entry.probe_class == "agentic"
    assert stats.agentic_probed == 1


def test_gateway_check_counts_against_the_coordinator_request_budget(tmp_path: Path) -> None:
    """Review finding 18b (2026-10-19): the gateway check's own live
    request was not counted in the coordinator's requests_made, so a
    second route could be admitted one request over max_requests. With
    max_requests=4: chat ok, tool 401 (2 requests), one gateway check
    (1 request) = 3 real requests before a second route could even be
    considered -- the second route must never be admitted past that.
    """
    from datetime import datetime, timezone

    from verdict.orchestration.health_cache import HealthCache

    now_local = datetime(2026, 10, 19, 0, 0, tzinfo=timezone.utc)
    cache = HealthCache(tmp_path / "cache.json")
    calls: list[tuple[str, str]] = []

    def chat(route_id: str, phase: str, timeout: float) -> ProbeExchange:
        calls.append((route_id, phase))
        if route_id == "p/a" and phase == "tool":
            return ProbeExchange(http_status=401, ok=False)
        return ProbeExchange(
            http_status=200, ok=True, chat_exact=(phase == "chat"), tool_called=(phase == "tool")
        )

    checks: list[float] = []

    def check(timeout: float) -> ProbeExchange:
        checks.append(timeout)
        return ProbeExchange(http_status=200, ok=True)

    routes = [AdmittedRoute("p/a", "p", "free"), AdmittedRoute("p2/b", "p2", "free")]
    stats = Prober(
        cache,
        lambda: routes,
        chat,
        clock=lambda: now_local,
        monotonic=lambda: 0.0,
        epsilon=0,
        max_requests=4,
        gateway_auth_check=check,
    ).run_once()
    total_real_requests = len(calls) + len(checks)
    assert total_real_requests <= 4
    assert stats.requests == total_real_requests


def test_gate_denies_its_own_check_when_the_triggering_probe_used_the_whole_cap(
    tmp_path: Path,
) -> None:
    """Review finding 19 (2026-10-19): the gate must check the coordinator's
    OWN remaining budget (passed in as ``remaining_requests``), not
    ``Prober.stats.requests`` -- that counter stays 0 for the whole chat
    phase until AFTER the coordinator call returns, so the old check always
    saw a free budget. With max_requests=2: chat ok + tool 401 already
    spends the entire cap (2 requests); the gate must not spend a 3rd
    request on a live check it cannot afford -- it reports unconfirmed
    (no write) and the job stops with request_cap, not an outage.
    """
    from datetime import datetime, timezone

    from verdict.orchestration.health_cache import HealthCache

    now_local = datetime(2026, 10, 19, 0, 0, tzinfo=timezone.utc)
    cache = HealthCache(tmp_path / "cache.json")
    sent: list[tuple[str, str]] = []

    def chat(route_id: str, phase: str, timeout: float) -> ProbeExchange:
        sent.append((route_id, phase))
        return ProbeExchange(
            http_status=200 if phase == "chat" else 401,
            ok=phase == "chat",
            chat_exact=phase == "chat",
        )

    checks: list[float] = []

    def check(timeout: float) -> ProbeExchange:
        checks.append(timeout)
        return ProbeExchange(http_status=200, ok=True)

    stats = Prober(
        cache,
        lambda: [AdmittedRoute("a/m", "a", "free")],
        chat,
        clock=lambda: now_local,
        monotonic=lambda: 0.0,
        sleep=lambda _s: None,
        epsilon=0,
        max_requests=2,
        gateway_auth_check=check,
    ).run_once()
    assert len(sent) == 2
    assert checks == []
    assert stats.requests == 2
    assert stats.stopped_reason == "request_cap"
    assert stats.auth_outage is False
    assert cache.entry("a/m") is None


def test_agentic_phase_runs_after_the_chat_phase_hits_its_caps(tmp_path: Path) -> None:
    """A spent chat budget (request and wall cap) no longer skips agentic probing."""
    tick = [0.0]

    def chat(route_id: str, phase: str, timeout: float) -> ProbeExchange:
        tick[0] += 10.0  # every chat probe advances the clock, within the wall budget
        return ProbeExchange(http_status=500, ok=False, error_category="upstream")

    routes = [_free(f"p{i}/m") for i in range(3)]
    cache = HealthCache(tmp_path / "cache.json", bucket_capacity=100)
    # BOD-297/S05-B (parent decision 2026-10-19, item 11): the coordinator
    # reserves a whole probe's request cost (2) upfront, never a partial
    # spend, so max_requests=2 lets p0 get one full probe that fails WITHIN
    # the wall budget (negative written, decision 5's deadline-drop does not
    # fire); p1 then hits the request cap.
    stats = Prober(
        cache,
        lambda: routes,
        chat,
        max_requests=2,
        max_wall_seconds=50.0,
        agentic_transport=lambda rid, payload, timeout: _turn(rid, payload),
        agentic_wall_seconds=1000.0,
        clock=lambda: NOW,
        monotonic=lambda: tick[0],
        epsilon=0,
    ).run_once()
    # BOD-297/S05-B (decision 14, same pattern as items 1/4): with 2-request
    # full probes, max_requests=2 admits exactly 1 route into the plan
    # (max_routes = max_requests // 2), so the coordinator labels the cap
    # "route_cap" where the old per-route loop labeled it "request_cap";
    # the numeric effect (probe exactly p0, leave p1/p2 for agentic) is
    # unchanged.
    assert stats.stopped_reason == "route_cap"
    assert stats.agentic_stopped_reason == "complete"
    assert stats.agentic_passed == 2  # p0 failed chat (negative); p1, p2 qualify
    assert agentic_capability(HealthCache(cache.path).entry("p1/m"), NOW)["qualified"]


def test_save_after_the_wall_cap_is_not_a_lock_timeout(tmp_path: Path) -> None:
    """The cycle deadline no longer bounds the cache lock wait (root cause of lock_timeout)."""
    tick = [0.0]

    def chat(route_id: str, phase: str, timeout: float) -> ProbeExchange:
        tick[0] = 1000.0  # the probe ends after the cycle deadline
        return ProbeExchange(http_status=502, ok=False, error_category="upstream")

    cache = HealthCache(tmp_path / "cache.json")
    stats = Prober(
        cache,
        lambda: [_free("p/m")],
        chat,
        max_wall_seconds=10.0,
        clock=lambda: NOW,
        monotonic=lambda: tick[0],
        sleep=lambda _s: None,
        epsilon=0,
    ).run_once()
    assert stats.stopped_reason != "lock_timeout"
    # Parent decision 2026-10-19: the coordinator records a probe that
    # crosses its own deadline as no_write (conservative: leave prior
    # proof), so this 502 is never persisted as a negative.
    assert stats.stopped_reason == "wall_cap"
    assert HealthCache(cache.path).entry("p/m") is None


def test_held_lock_still_times_out_and_skips_dispatch(tmp_path: Path) -> None:
    calls: list[str] = []
    cache = HealthCache(tmp_path / "cache.json")
    with (tmp_path / "cache.json.lock").open("a+") as held:
        fcntl.flock(held.fileno(), fcntl.LOCK_EX)
        stats = Prober(
            cache,
            lambda: [_free("p/m")],
            lambda rid, phase, timeout: calls.append(rid) or _dead_chat(rid, phase, timeout),
            agentic_transport=lambda rid, payload, timeout: (
                calls.append(rid) or _turn(rid, payload)
            ),
            clock=lambda: NOW,
            monotonic=lambda: 0.0,
            sleep=lambda _s: None,
        ).run_once()
    assert stats.stopped_reason == "lock_timeout"
    assert stats.agentic_stopped_reason == "lock_timeout"
    assert calls == []


def test_agentic_budget_is_configurable_and_respects_pool_limits(tmp_path: Path) -> None:
    lock = threading.Lock()
    active: dict[str, int] = {}
    peak: dict[str, int] = {}
    overall = [0, 0]
    barrier = threading.Event()

    def agentic(route_id: str, payload: dict[str, Any], timeout: float) -> ProbeExchange:
        pool = route_id.split("/", 1)[0]
        with lock:
            active[pool] = active.get(pool, 0) + 1
            peak[pool] = max(peak.get(pool, 0), active[pool])
            overall[0] += 1
            overall[1] = max(overall[1], overall[0])
            if overall[0] >= 3:
                barrier.set()
        barrier.wait(timeout=0.5)
        try:
            return _turn(route_id, payload)
        finally:
            with lock:
                active[pool] -= 1
                overall[0] -= 1

    routes = [_free(f"{pool}/m{i}") for pool in ("a", "b", "c") for i in range(4)]
    cache = HealthCache(tmp_path / "cache.json", bucket_capacity=100)
    prober = Prober(
        cache,
        lambda: routes,
        _dead_chat,
        agentic_transport=agentic,
        agentic_max_routes=9,
        agentic_concurrency=3,
        agentic_pool_concurrency=1,
        clock=lambda: NOW,
    )
    stats = CycleStats()
    prober.run_agentic_probes(stats, routes=routes)
    assert stats.agentic_probed == stats.agentic_passed == 9
    assert stats.requests == 27
    assert max(peak.values()) == 1, "one conversation per pool at a time"
    assert overall[1] == 3, "pools run concurrently"


def test_candidates_prefer_proven_then_live_catalog_routes(tmp_path: Path) -> None:
    cache = HealthCache(tmp_path / "cache.json")
    cache.record("p/tool", ProbeResult("ok", True, True), NOW)
    cache.record_liveness("q/chat", latency_ms=1, pool=None, capacity_evidence=None, now=NOW)
    cache.record("r/dead", ProbeResult("upstream", False, False, http_status=502), NOW)
    routes = [
        _free("s/ghost", live_catalog=False),
        _free("t/unknown"),
        _free("u/live", live_catalog=True),
        _free("q/chat"),
        _free("r/dead"),
        _free("p/tool"),
        AdmittedRoute("v/sub", "v", "subscription"),
    ]
    assert [r.route_id for r in agentic_candidates(routes, cache, NOW)] == [
        "p/tool",
        "q/chat",
        "u/live",
        "t/unknown",
        "s/ghost",
    ]
    assert [r.route_id for r, _ in order_cycle(routes, cache, NOW, epsilon=0)][:4] == [
        "u/live",
        "t/unknown",
        "s/ghost",
        "v/sub",
    ]


def test_routes_from_evidence_marks_live_catalog(tmp_path: Path) -> None:
    rows = [
        {"id": "af/served", "owned_by": "api-airforce", "capabilities": {"tool_calling": True}},
        {"id": "af/ghost", "owned_by": "api-airforce", "capabilities": {"tool_calling": True}},
    ]
    conns = [{"provider": "api-airforce", "isActive": True, "testStatus": "active"}]
    plain = routes_from_evidence(rows, conns, now=NOW, state_dir=tmp_path, openrouter_free_set=None)
    marked = routes_from_evidence(
        rows,
        conns,
        now=NOW,
        state_dir=tmp_path,
        live_catalog={"api-airforce": {"served"}},
        openrouter_free_set=None,
    )
    assert {r.route_id for r in plain} == {r.route_id for r in marked}, "a hint never admits"
    assert {r.route_id: r.live_catalog for r in plain} == dict.fromkeys(
        (r.route_id for r in plain), None
    )
    assert {r.route_id: r.live_catalog for r in marked} == {
        r.route_id: r.route_id == "af/served" for r in marked
    }


def test_gateway_catalog_ghost_is_catalog_stale_with_long_ttl() -> None:
    body = b'{"error":{"message":"Model \'x\' is not available in the active live catalog"}}'
    exc = urllib.error.HTTPError("http://gw", 400, "Bad Request", Message(), io.BytesIO(body))
    exchange = _http_error_exchange(exc, 5.0)
    assert exchange.error_category == "catalog_stale"
    other = urllib.error.HTTPError("http://gw", 400, "Bad", Message(), io.BytesIO(b"{}"))
    assert _http_error_exchange(other, 5.0).error_category == "http_error"


def test_agentic_http_failure_keeps_its_category(tmp_path: Path) -> None:
    cache = HealthCache(tmp_path / "cache.json")
    prober = Prober(
        cache,
        lambda: [_free("p/ghost")],
        _dead_chat,
        agentic_transport=lambda rid, payload, timeout: ProbeExchange(
            http_status=400, ok=False, error_category="catalog_stale"
        ),
        clock=lambda: NOW,
    )
    prober.run_agentic_probes(CycleStats())
    entry = cache.entry("p/ghost")
    assert entry is not None and entry.category == "catalog_stale"
    assert entry.until - NOW >= timedelta(hours=6)


def test_agentic_rate_limit_is_not_tested(tmp_path: Path) -> None:
    cache = HealthCache(tmp_path / "cache.json")
    prober = Prober(
        cache,
        lambda: [_free("p/busy")],
        _dead_chat,
        agentic_transport=lambda rid, payload, timeout: ProbeExchange(
            http_status=429, ok=False, retry_after_seconds=30
        ),
        clock=lambda: NOW,
    )
    stats = CycleStats()
    prober.run_agentic_probes(stats)
    assert cache.entry("p/busy") is None
    assert stats.agentic_probed == 0
    assert cache.bucket_remaining("p", NOW) == 0


def test_agentic_ttl_equals_the_reprobe_interval(tmp_path: Path) -> None:
    """Operator decision 2026-10-13: one constant for agentic proof TTL and re-probe interval."""
    assert DEFAULT_AGENTIC_INTERVAL_HOURS * 3600 == AGENTIC_PROBE_TTL_SECONDS
    cache = HealthCache(tmp_path / "cache.json")
    passed = ProbeResult("ok", True, True, probe_class="agentic", agentic_ok=True)
    cache.record("p/m", passed, NOW)
    later = NOW + timedelta(seconds=AGENTIC_PROBE_TTL_SECONDS - 1)
    assert agentic_capability(cache.entry("p/m"), later)["qualified"]
    assert not agentic_capability(
        cache.entry("p/m"), NOW + timedelta(seconds=AGENTIC_PROBE_TTL_SECONDS + 1)
    )["qualified"]
    prober = Prober(cache, lambda: [], _dead_chat)
    assert not prober._needs_agentic(_free("p/m"), later)
    # Liveness stays short and separate: the HEALTHY stage still needs a fresh probe.
    assert cache.lookup("p/m", NOW + timedelta(seconds=FRESH_SECONDS + 1)).state != "fresh"


def test_agentic_phase_waits_for_bucket_refill_within_its_budget(tmp_path: Path) -> None:
    clock = [NOW]
    mono = [0.0]

    def sleep(seconds: float) -> None:
        clock[0] += timedelta(seconds=seconds)
        mono[0] += seconds

    routes = [_free(f"p/m{i}") for i in range(3)]
    cache = HealthCache(tmp_path / "cache.json", bucket_capacity=3, bucket_window_seconds=60)
    prober = Prober(
        cache,
        lambda: routes,
        _dead_chat,
        agentic_transport=lambda rid, payload, timeout: _turn(rid, payload),
        agentic_max_routes=3,
        agentic_wall_seconds=100.0,
        clock=lambda: clock[0],
        monotonic=lambda: mono[0],
        sleep=sleep,
    )
    stats = CycleStats()
    prober.run_agentic_probes(stats, started=0.0, routes=routes)
    # One conversation per 60 s window: starts at 0 s and 60 s fit in 100 s; 120 s does not.
    assert stats.agentic_passed == 2
    assert stats.agentic_stopped_reason == "bucket_empty"
    assert stats.skipped_bucket == 1


def test_chat_phase_uses_the_callers_probe_timeout(tmp_path: Path) -> None:
    """BOD-297/S05-D: the coordinator call gets ``--timeout``, not the 30 s default.

    ``_run_chat_phase`` built its config from ``maintenance_from_env()`` without
    ``probe_timeout_seconds``, so a daemon started with ``--timeout 5`` still
    waited up to the coordinator's own 15 s cap per call.
    """
    seen: list[float] = []

    def transport(route_id: str, phase: str, timeout: float) -> ProbeExchange:
        seen.append(timeout)
        return ProbeExchange(http_status=500, ok=False, error_category="upstream")

    Prober(
        HealthCache(tmp_path / "health.json"),
        lambda: [_free("p0/m")],
        transport,
        clock=lambda: NOW,
        probe_timeout_seconds=5.0,
        agentic_max_routes=0,
    ).run_once()
    assert seen == [5.0]


@pytest.mark.parametrize("kind", ["joined", "debounced", "lock_timeout", "reused_fresh"])
def test_chat_phase_reports_a_joined_job_by_name_and_keeps_the_cursor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    """BOD-297/S12 item 2: a run that did not own the coordinator job says so.

    The owner's counts are not this process's work: probed stays 0 and the
    durable cursor is neither advanced nor cleared.
    """
    from verdict.orchestration import verified_refresh
    from verdict.orchestration.verified_refresh import RefreshOutcome, RouteOutcome

    def fake_refresh(self: Any, snapshot: Any, **kwargs: Any) -> RefreshOutcome:
        ids = [row.route_id for row in snapshot.rows]
        return RefreshOutcome(
            outcome=kind,
            job_id="owner-job",
            consumer="prove_at_rest",
            probed=1,
            verified=1,
            failed=0,
            unavailable=0,
            requests_made=0,
            elapsed_seconds=0.0,
            route_outcomes={
                rid: RouteOutcome(rid, rid.split("/", 1)[0], i == 0, i == 0)
                for i, rid in enumerate(ids)
            },
            complete=True,
        )

    monkeypatch.setattr(verified_refresh.RefreshCoordinator, "refresh_for_consumer", fake_refresh)
    cache = HealthCache(tmp_path / "cache.json")
    cursor = {"cycle_open": True, "probed_ids": ["old/m"]}
    cache.set_cursor(cursor)
    cache.save()
    calls: list[str] = []
    stats = Prober(
        cache,
        lambda: [_free("a/m"), _free("b/m")],
        lambda rid, phase, timeout: calls.append(rid) or _dead_chat(rid, phase, timeout),
        agentic_max_routes=0,
        clock=lambda: NOW,
        sleep=lambda _s: None,
    ).run_once()
    assert stats.stopped_reason == kind
    assert stats.probed == 0
    assert stats.fresh == 0
    assert stats.requests == 0
    assert calls == []
    assert HealthCache(cache.path).cursor == cursor


def test_run_forever_reports_each_successful_cycle(tmp_path: Path) -> None:
    """BOD-297/S12 item 3: the daemon hands every finished cycle to on_cycle."""
    seen: list[CycleStats] = []
    prober = Prober(
        HealthCache(tmp_path / "cache.json"),
        lambda: [],
        _dead_chat,
        agentic_max_routes=0,
        interval_seconds=0.25,
        clock=lambda: NOW,
        sleep=lambda _s: prober.stop() if len(seen) >= 2 else None,
        on_cycle=seen.append,
    )
    last = prober.run_forever()
    assert len(seen) == 2
    assert seen[-1] is last


def test_daemon_cli_prints_one_stats_line_per_cycle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """BOD-297/S12 item 3: journald gets `prove-at-rest cycle: {json}` per cycle."""
    from verdict import cli, prove_at_rest

    prober = Prober(
        HealthCache(tmp_path / "cache.json"),
        lambda: [],
        _dead_chat,
        agentic_max_routes=0,
        interval_seconds=0.25,
        clock=lambda: NOW,
        sleep=lambda _s: prober.stop(),
    )

    def fake_build(**_kwargs: Any) -> prove_at_rest.ProverDaemon:
        return prove_at_rest.ProverDaemon(prober=prober, consented=True)

    monkeypatch.setattr(prove_at_rest, "build_live_daemon", fake_build)
    cli.cmd_prove_at_rest(
        "daemon", state_path=str(tmp_path / "cache.json"), allow_live_probe=True, interval=0.25
    )
    lines = [
        line
        for line in capsys.readouterr().out.splitlines()
        if line.startswith("prove-at-rest cycle: ")
    ]
    assert len(lines) == 1
    assert (
        json.loads(lines[0].removeprefix("prove-at-rest cycle: "))
        == CycleStats(stopped_reason="complete").to_dict()
    )
