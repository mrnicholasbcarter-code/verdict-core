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


def test_agentic_phase_runs_after_the_chat_phase_hits_its_caps(tmp_path: Path) -> None:
    """A spent chat budget (request and wall cap) no longer skips agentic probing."""
    tick = [0.0]

    def chat(route_id: str, phase: str, timeout: float) -> ProbeExchange:
        tick[0] += 100.0  # every chat probe uses the whole chat wall budget
        return ProbeExchange(http_status=500, ok=False, error_category="upstream")

    routes = [_free(f"p{i}/m") for i in range(3)]
    cache = HealthCache(tmp_path / "cache.json", bucket_capacity=100)
    stats = Prober(
        cache,
        lambda: routes,
        chat,
        max_requests=1,
        max_wall_seconds=50.0,
        agentic_transport=lambda rid, payload, timeout: _turn(rid, payload),
        agentic_wall_seconds=1000.0,
        clock=lambda: NOW,
        monotonic=lambda: tick[0],
        epsilon=0,
    ).run_once()
    assert stats.stopped_reason == "request_cap"
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
    assert HealthCache(cache.path).entry("p/m") is not None


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
