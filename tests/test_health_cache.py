"""Health-cache TTL math, buckets, and the prove-at-rest prober.

No network. Probe outcomes are replayed from an injected transport. The large
cycle uses a trimmed eligibility snapshot: route id, provider, and capacity
only, with host paths removed.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

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
    FRESH_SECONDS,
    LONG_BASE_SECONDS,
    LONG_CAP_SECONDS,
    STATE_FRESH,
    STATE_NEGATIVE,
    STATE_STALE,
    STATE_UNPROBED,
    TRANSIENT_BASE_SECONDS,
    TRANSIENT_CAP_SECONDS,
    USABLE_SECONDS,
    HealthCache,
    ProbeResult,
    classify_state,
    negative_seconds,
    next_backoff_seconds,
)
from verdict.prove_at_rest import AdmittedRoute, ProbeExchange, Prober, order_cycle, status_report

NOW = datetime(2026, 9, 29, 5, 0, tzinfo=timezone.utc)


def _at(seconds: float) -> datetime:
    return NOW + timedelta(seconds=seconds)


# ---------------------------------------------------------------------------
# TTL math
# ---------------------------------------------------------------------------


def test_healthy_windows() -> None:
    assert FRESH_SECONDS == 600
    assert USABLE_SECONDS == 1800
    assert (
        classify_state(healthy=True, checked_at=NOW, until=_at(1800), now=_at(599)) == STATE_FRESH
    )
    assert (
        classify_state(healthy=True, checked_at=NOW, until=_at(1800), now=_at(600)) == STATE_STALE
    )
    assert (
        classify_state(healthy=True, checked_at=NOW, until=_at(1800), now=_at(1799)) == STATE_STALE
    )
    assert (
        classify_state(healthy=True, checked_at=NOW, until=_at(1800), now=_at(1800))
        == STATE_UNPROBED
    )


def test_rate_limit_uses_retry_after_or_sixty_seconds() -> None:
    assert negative_seconds(CATEGORY_RATE_LIMITED, http_status=429) == 60
    assert negative_seconds(CATEGORY_RATE_LIMITED, http_status=429, retry_after_seconds=15) == 15
    assert negative_seconds("other", http_status=429, retry_after_seconds=0) == 60


def test_transient_backoff_doubles_and_caps_at_fifteen_minutes() -> None:
    assert negative_seconds(CATEGORY_TIMEOUT, consecutive_failures=1) == TRANSIENT_BASE_SECONDS
    assert negative_seconds(CATEGORY_UPSTREAM, http_status=503, consecutive_failures=2) == 120
    assert negative_seconds(CATEGORY_TIMEOUT, consecutive_failures=3) == 240
    assert (
        negative_seconds(CATEGORY_UPSTREAM, http_status=500, consecutive_failures=20)
        == TRANSIENT_CAP_SECONDS
    )
    assert TRANSIENT_CAP_SECONDS == 900


@pytest.mark.parametrize(
    ("category", "status"),
    [
        (CATEGORY_AUTH, 401),
        (CATEGORY_PAYMENT, 402),
        (CATEGORY_PERMISSION, 403),
        (CATEGORY_NOT_FOUND, 404),
        (CATEGORY_GONE, 410),
        (CATEGORY_CATALOG_STALE, None),
    ],
)
def test_long_negatives_span_six_to_twenty_four_hours(category: str, status: int | None) -> None:
    assert (
        negative_seconds(category, http_status=status, consecutive_failures=1) == LONG_BASE_SECONDS
    )
    assert LONG_BASE_SECONDS == 6 * 3600
    assert (
        negative_seconds(category, http_status=status, consecutive_failures=3) == LONG_CAP_SECONDS
    )


def test_half_open_success_halves_backoff() -> None:
    assert next_backoff_seconds(240) == 120
    assert next_backoff_seconds(80) == 60  # floor at the transient base
    assert next_backoff_seconds(0) == 60


def test_negative_is_unprobed_once_until_passes() -> None:
    assert (
        classify_state(healthy=False, checked_at=NOW, until=_at(60), now=_at(59)) == STATE_NEGATIVE
    )
    assert (
        classify_state(healthy=False, checked_at=NOW, until=_at(60), now=_at(60)) == STATE_UNPROBED
    )


def test_record_resets_failures_on_success_and_doubles_on_failure(tmp_path: Path) -> None:
    cache = HealthCache(tmp_path / "cache.json")
    cache.record("a/m", ProbeResult(category=CATEGORY_TIMEOUT, chat_ok=False, tool_ok=False), NOW)
    first = cache.entry("a/m")
    assert first is not None
    assert first.consecutive_failures == 1
    assert first.until == _at(60)
    cache.record(
        "a/m",
        ProbeResult(category=CATEGORY_TIMEOUT, chat_ok=False, tool_ok=False, http_status=503),
        _at(120),
    )
    second = cache.entry("a/m")
    assert second is not None and second.consecutive_failures == 2
    assert second.until == _at(120 + 120)
    cache.record(
        "a/m",
        ProbeResult(category=CATEGORY_OK, chat_ok=True, tool_ok=True, http_status=200),
        _at(300),
    )
    done = cache.entry("a/m")
    assert done is not None and done.healthy and done.consecutive_failures == 0
    assert done.tool_ok is True


def test_unprobed_is_not_healthy(tmp_path: Path) -> None:
    cache = HealthCache(tmp_path / "cache.json")
    assert cache.lookup("missing", NOW).state == STATE_UNPROBED
    assert cache.lookup("missing", NOW).healthy is False
    assert cache.healthy_routes(NOW) == ()


def test_half_open_recovery_reports_halved_backoff(tmp_path: Path) -> None:
    cache = HealthCache(tmp_path / "cache.json")
    cache.record(
        "a/m",
        ProbeResult(category=CATEGORY_UPSTREAM, chat_ok=False, tool_ok=False, http_status=502),
        NOW,
    )
    cache.record(
        "a/m",
        ProbeResult(category=CATEGORY_UPSTREAM, chat_ok=False, tool_ok=False, http_status=502),
        _at(120),
    )
    previous = cache.entry("a/m")
    assert previous is not None
    span = (previous.until - previous.checked_at).total_seconds()
    assert cache.record_half_open_success("a/m", _at(10_000)) == next_backoff_seconds(span)


# ---------------------------------------------------------------------------
# Buckets
# ---------------------------------------------------------------------------


def test_bucket_sliding_window_and_override(tmp_path: Path) -> None:
    cache = HealthCache(tmp_path / "cache.json", bucket_capacity=3, bucket_overrides={"nvidia": 1})
    assert cache.consume("openrouter", NOW) is True
    assert cache.consume("openrouter", _at(1)) is True
    assert cache.consume("openrouter", _at(2)) is True
    assert cache.consume("openrouter", _at(3)) is False
    assert cache.consume("openrouter", _at(61)) is True  # first token aged out
    assert cache.consume("nvidia", NOW) is True
    assert cache.consume("nvidia", _at(1)) is False


def test_rate_limit_zeroes_bucket_until_retry_after(tmp_path: Path) -> None:
    cache = HealthCache(tmp_path / "cache.json", bucket_capacity=5)
    assert cache.consume("bai", NOW) is True
    cache.zero_bucket("bai", _at(30))
    assert cache.bucket_remaining("bai", _at(10)) == 0
    assert cache.consume("bai", _at(10)) is False
    assert cache.consume("bai", _at(30)) is True


def test_pool_bucket_is_separate_from_provider(tmp_path: Path) -> None:
    cache = HealthCache(tmp_path / "cache.json", bucket_capacity=1)
    assert cache.consume("agy", NOW, pool="antigravity") is True
    assert cache.consume("agy", NOW) is True
    assert cache.consume("agy", NOW, pool="antigravity") is False


def test_cache_round_trip_uses_a_lock_file(tmp_path: Path) -> None:
    path = tmp_path / "cache.json"
    cache = HealthCache(path)
    cache.record("p/m", ProbeResult(category=CATEGORY_OK, chat_ok=True, tool_ok=True), NOW)
    cache.consume("p", NOW)
    cache.save()
    assert path.exists()
    assert path.with_suffix(".json.lock").exists()
    again = HealthCache(path)
    assert again.lookup("p/m", _at(10)).state == STATE_FRESH
    assert again.bucket_remaining("p", _at(10)) == 9


# ---------------------------------------------------------------------------
# Ordering and caps
# ---------------------------------------------------------------------------


def _route(route_id: str, capacity: str, pool: str | None = None) -> AdmittedRoute:
    provider = route_id.split("/", 1)[0]
    return AdmittedRoute(
        route_id=route_id,
        provider=provider,
        capacity=capacity,
        pool=pool,
        capacity_evidence=capacity,
    )


def test_order_is_half_open_then_stale_then_free_then_others(tmp_path: Path) -> None:
    cache = HealthCache(tmp_path / "cache.json")
    cache.record(
        "sub/old",
        ProbeResult(category=CATEGORY_TIMEOUT, chat_ok=False, tool_ok=False),
        NOW - timedelta(seconds=120),
    )
    cache.record(
        "free/stale",
        ProbeResult(category=CATEGORY_OK, chat_ok=True, tool_ok=True),
        NOW - timedelta(seconds=700),
    )
    cache.record(
        "free/fresh",
        ProbeResult(category=CATEGORY_OK, chat_ok=True, tool_ok=True),
        NOW - timedelta(seconds=30),
    )
    routes = [
        _route("free/fresh", "free"),
        _route("free/new-b", "free"),
        _route("free/new-a", "free"),
        _route("free/stale", "free"),
        _route("sub/old", "subscription"),
        _route("sub/new", "subscription"),
        _route("meter/new", "metered"),
    ]
    ordered = order_cycle(routes, cache, NOW, epsilon=0)
    ids = [route.route_id for route, _kind in ordered]
    assert ids[0] == "sub/old"  # half-open negative
    assert ids[1] == "free/stale"
    assert "free/fresh" not in ids  # still fresh: not probed
    free_new = [route_id for route_id in ids if route_id.startswith("free/new")]
    assert free_new == ["free/new-b", "free/new-a"]  # round-robin, input order of providers
    assert ids[-2:] == ["sub/new", "meter/new"]
    kinds = {route.route_id: kind for route, kind in ordered}
    assert kinds["free/new-a"] == "full"
    assert kinds["sub/new"] == "liveness"


def test_free_round_robin_alternates_providers_for_real(tmp_path: Path) -> None:
    cache = HealthCache(tmp_path / "cache.json")
    routes = [
        _route("a/1", "free"),
        _route("a/2", "free"),
        _route("b/1", "free", pool="p"),
        _route("c/1", "free"),
    ]
    ordered = [route.route_id for route, _kind in order_cycle(routes, cache, NOW, epsilon=0)]
    assert ordered == ["a/1", "b/1", "c/1", "a/2"]


def test_epsilon_adds_one_route_per_cold_provider(tmp_path: Path) -> None:
    cache = HealthCache(tmp_path / "cache.json")
    cache.record("warm/m", ProbeResult(category=CATEGORY_OK, chat_ok=True, tool_ok=True), NOW)
    routes = [
        _route("warm/m", "free"),
        _route("warm/other", "free"),
        _route("cold/a", "subscription"),
        _route("colder/a", "metered"),
    ]
    # warm/other is never-probed FREE, so it is already ordered. cold and
    # colder are in the liveness pass. Epsilon must not duplicate them.
    ordered = order_cycle(routes, cache, NOW, epsilon=2)
    ids = [route.route_id for route, _kind in ordered]
    assert ids.count("cold/a") == 1
    assert "warm/m" not in ids


class _Script:
    """Transport that replays scripted exchanges and then refuses to continue."""

    def __init__(self, script: dict[tuple[str, str], ProbeExchange]) -> None:
        self.script = script
        self.calls: list[tuple[str, str]] = []

    def __call__(self, route_id: str, phase: str, timeout: float) -> ProbeExchange:
        self.calls.append((route_id, phase))
        found = self.script.get((route_id, phase))
        if found is None:
            raise AssertionError(f"unscripted probe {route_id} {phase}")
        return found


def _ok(tool: bool = False) -> ProbeExchange:
    return ProbeExchange(
        http_status=200, ok=True, chat_exact=not tool, tool_called=tool, latency_ms=10
    )


def _large_routes(n: int = 3100) -> list[AdmittedRoute]:
    """Generate *n* synthetic routes over 10 providers (no disk I/O)."""
    providers = [
        "openrouter",
        "kilocode",
        "agy",
        "cx",
        "gc",
        "kr",
        "cc",
        "cu",
        "opencode",
        "deepseek",
    ]
    capacities = ["free", "metered", "subscription"]
    return [
        AdmittedRoute(
            route_id=f"{providers[i % len(providers)]}/model-{i:04d}",
            provider=providers[i % len(providers)],
            capacity=capacities[i % len(capacities)],
            capacity_evidence=capacities[i % len(capacities)],
        )
        for i in range(n)
    ]


def test_cycle_never_exceeds_request_cap_on_large_inventory(tmp_path: Path) -> None:
    routes = _large_routes(3100)
    assert len(routes) > 3000

    def transport(route_id: str, phase: str, timeout: float) -> ProbeExchange:
        return _ok(tool=(phase == "tool"))

    cache = HealthCache(tmp_path / "cache.json", bucket_capacity=100000)
    clock = {"now": NOW}

    def tick() -> datetime:
        clock["now"] = clock["now"] + timedelta(seconds=1)
        return clock["now"]

    prober = Prober(
        cache=cache,
        routes_loader=lambda: routes,
        transport=transport,
        max_requests=300,
        max_wall_seconds=10_000,
        concurrency=4,
        clock=tick,
        monotonic=lambda: 0.0,
        sleep=lambda _s: None,
    )
    stats = prober.run_once()
    assert stats.requests <= 300
    assert stats.stopped_reason == "request_cap"
    # Persisted entries cannot exceed the calls that were allowed.
    assert len(cache.routes()) <= 300


def test_crash_mid_cycle_keeps_partial_results(tmp_path: Path) -> None:
    path = tmp_path / "cache.json"
    routes = [_route(f"free/{index}", "free") for index in range(6)]

    class Boom:
        def __init__(self) -> None:
            self.calls = 0

        def __call__(self, route_id: str, phase: str, timeout: float) -> ProbeExchange:
            self.calls += 1
            if self.calls > 3:
                raise RuntimeError("crash")
            return _ok(tool=(phase == "tool"))

    prober = Prober(
        cache=HealthCache(path, bucket_capacity=100),
        routes_loader=lambda: routes,
        transport=Boom(),
        max_requests=100,
        concurrency=1,
        clock=lambda: NOW,
        monotonic=lambda: 0.0,
    )
    with pytest.raises(RuntimeError, match="crash"):
        prober.run_once()
    kept = HealthCache(path)
    assert len(kept.routes()) >= 1
    for entry in kept.routes().values():
        assert entry.chat_ok is True


def test_two_step_probe_replays_recorded_outcomes(tmp_path: Path) -> None:
    """Replay the recorded free-probe outcomes through a fake transport.

    The fixture rows are the outcome fields only: route, status, category.
    A 200 chat with a later tool success is a coding worker. A 402 stays
    negative. Nothing in this test opens a socket.
    """
    recorded = [
        ("agy/codestral", 200, True),
        ("cerebras/gpt-oss-120b", 402, False),
        ("nvidia/openai/gpt-oss-120b", 410, False),
    ]
    script: dict[tuple[str, str], ProbeExchange] = {}
    routes: list[AdmittedRoute] = []
    for route_id, status, ok in recorded:
        routes.append(_route(route_id, "free"))
        if ok:
            script[(route_id, "chat")] = ProbeExchange(
                http_status=200, ok=True, chat_exact=True, latency_ms=12
            )
            script[(route_id, "tool")] = ProbeExchange(
                http_status=200, ok=True, tool_called=True, latency_ms=20
            )
        else:
            category = "payment_required" if status == 402 else "http_410"
            script[(route_id, "chat")] = ProbeExchange(
                http_status=status, ok=False, error_category=category, latency_ms=5
            )
    transport = _Script(script)
    cache = HealthCache(tmp_path / "cache.json")
    prober = Prober(
        cache=cache,
        routes_loader=lambda: routes,
        transport=transport,
        max_requests=20,
        clock=lambda: NOW,
        monotonic=lambda: 0.0,
        epsilon=0,
    )
    prober.run_once()
    worker = cache.lookup("agy/codestral", _at(10))
    assert worker.state == STATE_FRESH and worker.entry is not None and worker.entry.tool_ok
    paid = cache.lookup("cerebras/gpt-oss-120b", _at(10))
    assert paid.state == STATE_NEGATIVE
    assert paid.entry is not None and paid.entry.category == CATEGORY_PAYMENT
    gone = cache.lookup("nvidia/openai/gpt-oss-120b", _at(10))
    assert gone.state == STATE_NEGATIVE
    assert gone.entry is not None and (gone.entry.until - NOW).total_seconds() == LONG_BASE_SECONDS
    # The tool step ran only after a chat OK.
    assert ("agy/codestral", "tool") in transport.calls
    assert ("cerebras/gpt-oss-120b", "tool") not in transport.calls


def test_liveness_probe_does_not_call_the_tool(tmp_path: Path) -> None:
    transport = _Script({("sub/m", "chat"): _ok()})
    cache = HealthCache(tmp_path / "cache.json")
    prober = Prober(
        cache=cache,
        routes_loader=lambda: [_route("sub/m", "subscription")],
        transport=transport,
        clock=lambda: NOW,
        monotonic=lambda: 0.0,
        epsilon=0,
    )
    prober.run_once()
    assert transport.calls == [("sub/m", "chat")]
    entry = cache.entry("sub/m")
    assert entry is not None and entry.healthy and entry.tool_ok is False
    workers = cache.healthy_routes(_at(1), predicate=lambda item: item.tool_ok)
    assert workers == ()


def test_bucket_stops_a_provider_mid_cycle(tmp_path: Path) -> None:
    # bucket_capacity=1 with 5 full-kind routes (chat+tool each):
    # Route 0: chat consumes the 1 token (requests=1); tool: no bucket → skipped_bucket+=1.
    # Routes 1-4: initial consume fails → skipped_bucket+=1 each.
    # Total: requests=1, skipped_bucket=5 (1 tool-skip + 4 initial-skips).
    routes = [_route(f"bai/{index}", "free") for index in range(5)]
    transport = _Script(
        {(route.route_id, "chat"): _ok() for route in routes}
        | {(route.route_id, "tool"): _ok(tool=True) for route in routes}
    )
    cache = HealthCache(tmp_path / "cache.json", bucket_capacity=1)
    prober = Prober(
        cache=cache,
        routes_loader=lambda: routes,
        transport=transport,
        clock=lambda: NOW,
        monotonic=lambda: 0.0,
        epsilon=0,
    )
    stats = prober.run_once()
    assert stats.requests == 1
    assert stats.skipped_bucket == 5  # 1 tool-skip (defect-1 fix) + 4 initial-skips


def test_status_report_lists_workers_and_cold_providers(tmp_path: Path) -> None:
    cache = HealthCache(tmp_path / "cache.json")
    cache.record(
        "agy/codestral",
        ProbeResult(
            category=CATEGORY_OK,
            chat_ok=True,
            tool_ok=True,
            latency_ms=40,
            capacity_evidence="free",
        ),
        NOW,
    )
    cache.record(
        "bai/paid",
        ProbeResult(category=CATEGORY_PAYMENT, chat_ok=False, tool_ok=False, http_status=402),
        NOW,
    )
    report = status_report(cache, now=_at(5))
    assert report["counts_by_state"]["fresh"] == 1
    assert report["counts_by_state"]["negative"] == 1
    assert report["top_healthy_coding_workers"][0]["route_id"] == "agy/codestral"
    assert "bai" in report["cold_providers"]
    assert "agy" not in report["cold_providers"]
    assert report["legacy_state"] == "ignored"


def test_capacity_class_matches_the_ladder() -> None:
    """capacity_class_of agrees with EligibilityLadder._capacity_class on shared shapes.

    The standalone function omits the ``:free`` suffix and connection-signal
    extensions (documented in its docstring) but must agree on every case it
    does handle.  We compare against the ladder's 3-tuple output (class,
    plan_label, rule) by checking only the class, which is what callers use.
    """
    from verdict.orchestration.eligibility import EligibilityLadder

    # A minimal ladder that needs no filesystem state for _capacity_class.
    ladder = EligibilityLadder.__new__(EligibilityLadder)

    # Shapes that capacity_class_of handles (no :free suffix / no
    # importFreeModelsOnly / no providerSpecificData that the ladder adds).
    shared_shapes: list[tuple[dict[str, object] | None, dict[str, object], CapacityClass]] = [
        (None, {}, CapacityClass.UNKNOWN),
        ({"authType": "oauth", "plan_label": "pro"}, {}, CapacityClass.SUBSCRIPTION),
        ({"authType": "oauth", "plan_label": "free"}, {}, CapacityClass.FREE),
        (
            {"authType": "apikey", "import_free_only": True, "plan_label": ""},
            {},
            CapacityClass.FREE,
        ),
        (
            {"authType": "apikey", "plan_label": ""},
            {"pricing": {"prompt": 1.0}},
            CapacityClass.METERED,
        ),
        ({"authType": "apikey", "plan_label": ""}, {"pricing": {"prompt": 0}}, CapacityClass.FREE),
        ({"authType": "apikey", "plan_label": ""}, {}, CapacityClass.UNKNOWN),
        # Bool pricing values must NOT be treated as prices (True == 1 in Python).
        ({"authType": "apikey", "plan_label": ""}, {"pricing": {"p": True}}, CapacityClass.UNKNOWN),
    ]
    for conn, row, expected_class in shared_shapes:
        via_standalone = capacity_class_of(conn, row)
        # _capacity_class returns (CapacityClass, plan_label, rule); extract class.
        via_ladder_triple = ladder._capacity_class(conn, row)
        assert via_standalone[0] == via_ladder_triple[0], (
            f"standalone/ladder mismatch for conn={conn!r} row={row!r}: "
            f"{via_standalone[0]} vs {via_ladder_triple[0]}"
        )
        assert via_standalone[0] == expected_class, (
            f"expected {expected_class} for conn={conn!r} row={row!r}, got {via_standalone[0]}"
        )

    # Extra cases the ladder handles via :free suffix / connection signals that
    # capacity_class_of intentionally skips (document their divergence).
    suffix_conn = {"authType": "oauth", "plan_label": "pro"}
    suffix_row: dict[str, object] = {}
    # :free suffix → FREE in ladder, but capacity_class_of returns SUBSCRIPTION.
    ladder_class, _, rule = ladder._capacity_class(
        suffix_conn, suffix_row, route_id="cc/model:free"
    )
    assert ladder_class == CapacityClass.FREE, f"ladder should classify :free as FREE, got {rule}"
    standalone_class, _ = capacity_class_of(suffix_conn, suffix_row)
    assert standalone_class == CapacityClass.SUBSCRIPTION, (
        "capacity_class_of must NOT classify :free suffix (it is a documented omission)"
    )


def test_legacy_cycle_file_is_not_written(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    legacy = tmp_path / "state.json"
    legacy.write_text('{"schema_version": "1", "keep": true}\n', encoding="utf-8")
    cache_path = tmp_path / "health.json"
    monkeypatch.setenv("VERDICT_PROVE_AT_REST_STATE", str(legacy))
    transport = _Script({("free/m", "chat"): _ok(), ("free/m", "tool"): _ok(tool=True)})
    prober = Prober(
        cache=HealthCache(cache_path),
        routes_loader=lambda: [_route("free/m", "free")],
        transport=transport,
        clock=lambda: NOW,
        monotonic=lambda: 0.0,
        epsilon=0,
    )
    prober.run_once()
    assert json.loads(legacy.read_text(encoding="utf-8"))["keep"] is True
    assert cache_path.exists()


# ---------------------------------------------------------------------------
# Regression tests for reviewer defects (fix2)
# ---------------------------------------------------------------------------


def test_tool_skipped_no_bucket_does_not_record_negative(tmp_path: Path) -> None:
    """Defect 1: chat succeeds, bucket exhausted for tool → route NOT marked negative.

    Only the chat token is consumed; the route should remain unprobed (no
    negative entry) because the tool was never tested.
    """
    cache_path = tmp_path / "cache.json"
    cache = HealthCache(cache_path, bucket_capacity=1)
    route = _route("cx/model", "free")
    # Only script the chat call; the tool must never be called.
    transport = _Script({("cx/model", "chat"): _ok()})
    prober = Prober(
        cache=cache,
        routes_loader=lambda: [route],
        transport=transport,
        clock=lambda: NOW,
        monotonic=lambda: 0.0,
        epsilon=0,
    )
    stats = prober.run_once()
    # One chat request consumed the bucket.
    assert stats.requests == 1
    # The route must NOT be marked unhealthy or cooled down.
    entry = cache.entry("cx/model")
    assert entry is None or entry.category != CATEGORY_OK or not entry.healthy, (
        "route should be unprobed or negative-free, not healthy"
    )
    # Confirm it wasn't recorded as negative.
    if entry is not None:
        from verdict.orchestration.health_cache import STATE_NEGATIVE

        assert entry.state_at(NOW) != STATE_NEGATIVE, (
            "skipped-tool route must not be marked negative"
        )


def test_cursor_resumes_at_first_unprobed_route_after_cap(tmp_path: Path) -> None:
    """Defect 2: a request-cap hit mid-batch leaves the cursor at the first unprobed route.

    With concurrency=1 and cap=2 on 3 full-probe routes, the prober should:
    - Probe route 0 completely (chat + tool use the 2 allowed requests).
    - Stop: route 0 is in probed_ids, route 1 is not.
    - Second cycle: resume at route 1 (not route 2 or back to 0).
    """
    routes = [_route(f"cx/r{i}", "free") for i in range(3)]
    cache_path = tmp_path / "cache.json"
    cache = HealthCache(cache_path, bucket_capacity=100)
    transport = _Script(
        {(r.route_id, "chat"): _ok() for r in routes}
        | {(r.route_id, "tool"): _ok(tool=True) for r in routes}
    )
    prober = Prober(
        cache=cache,
        routes_loader=lambda: routes,
        transport=transport,
        clock=lambda: NOW,
        monotonic=lambda: 0.0,
        max_requests=2,
        epsilon=0,
        concurrency=1,
    )
    stats1 = prober.run_once()
    assert stats1.stopped_reason == "request_cap"
    # Cursor must record the probed route ids so the next cycle resumes correctly.
    # (Defect 2 fix changed from next_index to probed_ids.)
    cursor = cache.cursor
    assert cursor.get("cycle_open") is True
    probed_ids = cursor.get("probed_ids") or []
    assert "cx/r0" in probed_ids, f"expected cx/r0 in probed_ids after cap, got {probed_ids!r}"
    assert "cx/r1" not in probed_ids, (
        f"cx/r1 must not be in probed_ids (was never probed), got {probed_ids!r}"
    )

    # Second cycle, after the fresh window: resume at r1 without repeating r0.
    transport.calls.clear()
    later = NOW + timedelta(seconds=FRESH_SECONDS + 1)
    prober2 = Prober(
        cache=cache,
        routes_loader=lambda: routes,
        transport=transport,
        clock=lambda: later,
        monotonic=lambda: 0.0,
        max_requests=2,
        epsilon=0,
        concurrency=1,
    )
    stats2 = prober2.run_once()
    assert stats2.stopped_reason == "request_cap"
    assert transport.calls == [("cx/r1", "chat"), ("cx/r1", "tool")], transport.calls
    assert set(cache.cursor.get("probed_ids") or []) == {"cx/r0", "cx/r1"}


def test_model_mismatch_is_not_recorded_healthy(tmp_path: Path) -> None:
    """Defect 3: a response reporting a different model must be recorded as model_mismatch."""
    cache_path = tmp_path / "cache.json"
    cache = HealthCache(cache_path, bucket_capacity=100)
    route = _route("cx/expected-model", "free")
    # Chat response echoes the wrong model.
    mismatched_chat = ProbeExchange(
        http_status=200,
        ok=True,
        chat_exact=True,
        tool_called=False,
        latency_ms=10,
        reported_model="cx/different-model",
    )
    transport = _Script({("cx/expected-model", "chat"): mismatched_chat})
    prober = Prober(
        cache=cache,
        routes_loader=lambda: [route],
        transport=transport,
        clock=lambda: NOW,
        monotonic=lambda: 0.0,
        epsilon=0,
        concurrency=1,
    )
    prober.run_once()
    entry = cache.entry("cx/expected-model")
    assert entry is not None
    assert entry.category == CATEGORY_MODEL_MISMATCH, (
        f"expected model_mismatch category, got {entry.category!r}"
    )
    assert not entry.healthy


def test_model_identity_match_is_healthy(tmp_path: Path) -> None:
    """Defect 3 (correct case): matching reported model → healthy."""
    cache_path = tmp_path / "cache.json"
    cache = HealthCache(cache_path, bucket_capacity=100)
    route = _route("cx/good-model", "free")
    # Chat reports the same model (without provider prefix — tolerated).
    matching_chat = ProbeExchange(
        http_status=200,
        ok=True,
        chat_exact=True,
        tool_called=False,
        latency_ms=10,
        reported_model="good-model",
    )
    matching_tool = ProbeExchange(
        http_status=200,
        ok=True,
        chat_exact=False,
        tool_called=True,
        latency_ms=10,
        reported_model="good-model",
    )
    transport = _Script(
        {("cx/good-model", "chat"): matching_chat, ("cx/good-model", "tool"): matching_tool}
    )
    prober = Prober(
        cache=cache,
        routes_loader=lambda: [route],
        transport=transport,
        clock=lambda: NOW,
        monotonic=lambda: 0.0,
        epsilon=0,
        concurrency=1,
    )
    prober.run_once()
    entry = cache.entry("cx/good-model")
    assert entry is not None
    assert entry.healthy, f"matching-model probe should be healthy; category={entry.category!r}"


def test_wall_cap_before_tool_call_does_not_record_negative(tmp_path: Path) -> None:
    """Defect 5: wall deadline hit between chat and tool → route stays unchanged."""
    cache_path = tmp_path / "cache.json"
    cache = HealthCache(cache_path, bucket_capacity=100)
    route = _route("cx/walltgt", "free")
    clock_val = [0.0]

    def monotonic() -> float:
        return clock_val[0]

    script = _Script({("cx/walltgt", "chat"): _ok()})

    def transport(route_id: str, phase: str, timeout: float) -> ProbeExchange:
        exchange = script(route_id, phase, timeout)
        if phase == "chat":
            clock_val[0] = 601.0  # deadline advances on dispatch, not on lock checks
        return exchange

    prober = Prober(
        cache=cache,
        routes_loader=lambda: [route],
        transport=transport,
        clock=lambda: NOW,
        monotonic=monotonic,
        max_wall_seconds=600.0,
        epsilon=0,
        concurrency=1,
    )
    stats = prober.run_once()
    assert stats.stopped_reason == "wall_cap"
    # Route must not be recorded as negative.
    entry = cache.entry("cx/walltgt")
    if entry is not None:
        from verdict.orchestration.health_cache import STATE_NEGATIVE

        assert entry.state_at(NOW) != STATE_NEGATIVE, (
            "wall-capped route between chat and tool must not be negative"
        )


def test_action_prove_once_forwards_max_wall_seconds() -> None:
    """Defect 4: max_wall_seconds from kwargs must reach build_live_daemon."""
    calls: list[dict[str, object]] = []

    from verdict.prove_at_rest import CycleStats

    def fake_build(**kw: object) -> object:
        calls.append(kw)

        class FakeDaemon:
            consented = True

            def run_once(self) -> CycleStats:
                return CycleStats()

        return FakeDaemon()

    # build_live_daemon is imported locally inside the action function under the
    # name 'verdict.prove_at_rest.build_live_daemon', so patch at the source.
    from unittest.mock import patch

    with patch("verdict.prove_at_rest.build_live_daemon", side_effect=fake_build):
        from verdict.actions.extra import _action_prove_at_rest_once

        _action_prove_at_rest_once(
            allow_live_probe=True, base_url="http://localhost:9999", max_wall_seconds=120.0
        )

    assert calls, "build_live_daemon was never called"
    assert calls[0].get("max_wall_seconds") == 120.0, f"max_wall_seconds not forwarded: {calls[0]}"


# ---------------------------------------------------------------------------
# Review round 3 — six regression tests
# ---------------------------------------------------------------------------


def test_request_cap_after_chat_does_not_record_negative(tmp_path: Path) -> None:
    """Issue 1 (request-cap path): chat succeeds, request cap reached → no negative.

    The max_requests cap fires BETWEEN the chat and the tool call.
    The route must be left unchanged (not recorded as a negative).
    """
    cache_path = tmp_path / "cache.json"
    cache = HealthCache(cache_path, bucket_capacity=100)
    route = _route("cx/rc-target", "free")
    # Only script the chat call; the tool must never be called.
    transport = _Script({("cx/rc-target", "chat"): _ok()})
    prober = Prober(
        cache=cache,
        routes_loader=lambda: [route],
        transport=transport,
        clock=lambda: NOW,
        monotonic=lambda: 0.0,
        # cap at exactly 1: chat call consumes it; tool would need cap > 1
        max_requests=1,
        epsilon=0,
        concurrency=1,
    )
    # Seed an existing healthy entry so "left unchanged" is observable.
    seeded = cache.record(
        "cx/rc-target",
        ProbeResult(category=CATEGORY_OK, chat_ok=True, tool_ok=True, latency_ms=7.0),
        NOW - timedelta(seconds=FRESH_SECONDS + 1),
    )
    cache.save()  # establish the persisted revision before testing no-write
    persisted = cache.entry(seeded.route_id)
    assert persisted is not None
    before = persisted.to_dict()
    stats = prober.run_once()
    assert stats.stopped_reason == "request_cap", (
        f"unexpected stopped_reason: {stats.stopped_reason!r}"
    )
    entry = cache.entry("cx/rc-target")
    assert entry is not None
    assert entry.to_dict() == before, "the seeded entry must be left exactly as it was"
    # Tool must never have been called.
    assert ("cx/rc-target", "tool") not in transport.calls, (
        "tool call must not be made when request cap reached after chat"
    )
    # The route was not completed, so the open cycle must still list it as pending.
    assert cache.cursor.get("cycle_open") is True
    assert "cx/rc-target" not in (cache.cursor.get("probed_ids") or []), (
        "an incompletely probed route must not be marked as probed in the cursor"
    )


def test_cursor_resume_survives_route_reorder_and_removal(tmp_path: Path) -> None:
    """Issue 2 (cursor by route_id): routes disappear/reorder -> no unprobed route skipped.

    Cycle 1: 4 free routes, cap=4 -> exactly r0 and r1 get full probes (2 requests
    each); the cycle stays open with probed_ids == {r0, r1}.
    Between cycles the clock passes the fresh window (so r0/r1 are due again),
    the list reorders, and r0 disappears.
    Cycle 2 must resume the open cycle: probe r3 and r2, and not re-probe r1.
    """
    r0, r1, r2, r3 = [_route(f"cx/rv{i}", "free") for i in range(4)]
    cache = HealthCache(tmp_path / "cache.json", bucket_capacity=100)
    transcript: list[tuple[str, str]] = []
    now = {"t": NOW}

    def transport(route_id: str, phase: str, timeout: float) -> ProbeExchange:
        transcript.append((route_id, phase))
        return _ok(tool=(phase == "tool"))

    prober1 = Prober(
        cache=cache,
        routes_loader=lambda: [r0, r1, r2, r3],
        transport=transport,
        clock=lambda: now["t"],
        monotonic=lambda: 0.0,
        max_requests=4,
        epsilon=0,
        concurrency=1,
    )
    stats1 = prober1.run_once()
    assert stats1.stopped_reason == "request_cap"
    cycle1 = {rid for rid, _ in transcript}
    assert cycle1 == {r0.route_id, r1.route_id}, f"cycle 1 probed {sorted(cycle1)}"
    assert cache.cursor.get("cycle_open") is True
    assert set(cache.cursor.get("probed_ids") or []) == {r0.route_id, r1.route_id}

    transcript.clear()
    now["t"] = NOW + timedelta(seconds=FRESH_SECONDS + 1)
    prober2 = Prober(
        cache=cache,
        routes_loader=lambda: [r1, r3, r2],
        transport=transport,
        clock=lambda: now["t"],
        monotonic=lambda: 0.0,
        max_requests=1000,
        epsilon=0,
        concurrency=1,
    )
    prober2.run_once()
    cycle2 = [rid for rid, phase in transcript if phase == "chat"]
    assert r1.route_id not in cycle2, f"r1 was re-probed inside the open cycle: {cycle2}"
    assert r3.route_id in cycle2 and r2.route_id in cycle2, f"cycle 2 skipped work: {cycle2}"
    assert cycle2.index(r3.route_id) < cycle2.index(r2.route_id), "new order must be followed"
    for ri in (r2, r3):
        entry = cache.entry(ri.route_id)
        assert entry is not None and entry.healthy


def test_half_open_and_stale_paid_routes_get_liveness_kind(tmp_path: Path) -> None:
    """Issue 3: half-open and stale subscription/metered routes must use kind='liveness'.

    Only FREE routes get kind='full'.  Other capacity classes are documented
    as chat-only; forcing a tool call on them was a defect.
    """
    cache = HealthCache(tmp_path / "cache.json")
    long_ago = NOW - timedelta(seconds=3600 * 8)  # deep in the negative window
    # Record a negative for a subscription route (will become half-open/stale once it
    # elapses, but for ordering we just need it in the half-open bucket).
    cache.record(
        "sub/model", ProbeResult(category=CATEGORY_TIMEOUT, chat_ok=False, tool_ok=False), long_ago
    )
    # Record a stale healthy entry for a metered route.
    cache.record(
        "meter/model",
        ProbeResult(category=CATEGORY_OK, chat_ok=True, tool_ok=True),
        NOW - timedelta(seconds=700),
    )
    routes = [
        _route("sub/model", "subscription"),
        _route("meter/model", "metered"),
        _route("free/model", "free"),
    ]
    ordered = order_cycle(routes, cache, NOW, epsilon=0)
    kinds = {route.route_id: kind for route, kind in ordered}

    # Half-open subscription → liveness.
    assert "sub/model" in kinds, "sub/model should be in ordered (half-open)"
    assert kinds["sub/model"] == "liveness", (
        f"subscription half-open must use kind='liveness', got {kinds['sub/model']!r}"
    )
    # Stale metered → liveness.
    assert "meter/model" in kinds, "meter/model should be in ordered (stale)"
    assert kinds["meter/model"] == "liveness", (
        f"metered stale must use kind='liveness', got {kinds['meter/model']!r}"
    )
    # Free → full (unchanged).
    if "free/model" in kinds:
        assert kinds["free/model"] == "full", "free route must keep kind='full'"


def test_model_identity_same_prefix_matches_different_prefix_does_not(tmp_path: Path) -> None:
    """Issue 4: provider prefix in reported model must be compared, not stripped.

    Cases:
    - Same provider prefix + same suffix → match (True).
    - No prefix in reported + same suffix → match (True).
    - Different provider prefix + same suffix → MISMATCH (False).
    """
    from verdict.prove_at_rest import Prober

    match_fn = Prober._model_identity_matches

    # Same provider: cc/model vs cc/model → match.
    assert match_fn("cc/claude-opus-4", "cc/claude-opus-4") is True, (
        "identical route_id+reported must match"
    )
    # No prefix in reported, same suffix → match (gateway stripped prefix).
    assert match_fn("cc/claude-opus-4", "claude-opus-4") is True, (
        "reported without prefix and matching suffix must match"
    )
    # Different provider prefix + same suffix → MISMATCH.
    assert match_fn("cc/claude-opus-4", "otherprov/claude-opus-4") is False, (
        "different provider prefix must be a mismatch even if suffix matches"
    )
    # Empty reported → match (not a mismatch).
    assert match_fn("cc/model", "") is True, "empty reported must not be a mismatch"
    # Nested model ids: an echo without only the gateway provider segment is
    # accepted (OmniRoute was observed echoing nvidia/moonshotai/kimi-k3 as
    # moonshotai/kimi-k3).
    assert match_fn("nvidia/moonshotai/kimi-k3", "moonshotai/kimi-k3") is True, (
        "a nested model id echoed without the gateway prefix must match"
    )
    assert match_fn("nvidia/moonshotai/kimi-k3", "nvidia/moonshotai/kimi-k3") is True
    assert match_fn("nvidia/moonshotai/kimi-k3", "other/moonshotai/kimi-k3") is False
    assert match_fn("nvidia/moonshotai/kimi-k3", "kimi-k3") is False, (
        "stripping more than the gateway segment is not the same identity"
    )


def test_empty_reported_model_stored_as_not_reported_identity(tmp_path: Path) -> None:
    """Issue 5: a probe where the gateway echoes no model must record identity='not_reported'.

    The route stays healthy (liveness is fine), but the identity field in the
    cache entry and the status report must say 'not_reported'.
    """
    cache_path = tmp_path / "cache.json"
    cache = HealthCache(cache_path, bucket_capacity=100)
    route = _route("cx/silent-model", "free")
    # Both chat and tool succeed but neither echoes a model id.
    chat_no_id = ProbeExchange(
        http_status=200,
        ok=True,
        chat_exact=True,
        tool_called=False,
        latency_ms=10,
        reported_model="",
    )
    tool_no_id = ProbeExchange(
        http_status=200,
        ok=True,
        chat_exact=False,
        tool_called=True,
        latency_ms=10,
        reported_model="",
    )
    transport = _Script(
        {("cx/silent-model", "chat"): chat_no_id, ("cx/silent-model", "tool"): tool_no_id}
    )
    prober = Prober(
        cache=cache,
        routes_loader=lambda: [route],
        transport=transport,
        clock=lambda: NOW,
        monotonic=lambda: 0.0,
        epsilon=0,
        concurrency=1,
    )
    prober.run_once()
    entry = cache.entry("cx/silent-model")
    assert entry is not None, "route should have been probed"
    # Route is healthy for liveness (still coding-worker-eligible if tool_ok).
    # Identity must be recorded as not_reported.
    assert entry.identity == "not_reported", (
        f"expected identity='not_reported', got {entry.identity!r}"
    )
    # Status report must also surface it.
    report = status_report(cache, now=NOW)
    workers = report.get("top_healthy_coding_workers", [])
    worker = next((w for w in workers if w["route_id"] == "cx/silent-model"), None)
    assert worker is not None, f"probed route missing from status report: {workers!r}"
    assert worker.get("identity") == "not_reported", (
        f"status report worker must carry identity='not_reported', got {worker!r}"
    )


def test_identity_verified_only_after_a_successful_match(tmp_path: Path) -> None:
    """A matching echo is 'verified'; a non-matching echo is a mismatch, never verified."""
    cache = HealthCache(tmp_path / "cache.json", bucket_capacity=100)
    good = _route("nvidia/moonshotai/kimi-k3", "free")
    bad = _route("cx/expected", "free")

    def ex(tool: bool, reported: str) -> ProbeExchange:
        return ProbeExchange(
            http_status=200,
            ok=True,
            chat_exact=not tool,
            tool_called=tool,
            latency_ms=10,
            reported_model=reported,
        )

    transport = _Script(
        {
            (good.route_id, "chat"): ex(False, "moonshotai/kimi-k3"),
            (good.route_id, "tool"): ex(True, "moonshotai/kimi-k3"),
            (bad.route_id, "chat"): ex(False, "other/expected"),
        }
    )
    prober = Prober(
        cache=cache,
        routes_loader=lambda: [good, bad],
        transport=transport,
        clock=lambda: NOW,
        monotonic=lambda: 0.0,
        epsilon=0,
        concurrency=1,
    )
    prober.run_once()
    good_entry = cache.entry(good.route_id)
    bad_entry = cache.entry(bad.route_id)
    assert good_entry is not None and good_entry.identity == "verified"
    assert good_entry.healthy
    assert bad_entry is not None
    assert bad_entry.category == CATEGORY_MODEL_MISMATCH
    assert bad_entry.identity != "verified", f"mismatch recorded as verified: {bad_entry!r}"
    assert not bad_entry.healthy
    report = status_report(cache, now=NOW)
    shown = {w["route_id"]: w for w in report.get("top_healthy_coding_workers", [])}
    assert shown[good.route_id]["identity"] == "verified"
    assert bad.route_id not in shown, "a mismatched route must not be listed as a healthy worker"


def test_cli_once_forwards_max_wall_seconds(tmp_path: Path) -> None:
    """Issue 6: cmd_prove_at_rest('once', max_wall_seconds=N) must include it in params.

    Calls the CLI function directly (bypassing Click argument parsing) and
    patches run_action at the source imported by cli.py to capture params.
    """
    collected: list[dict[str, object]] = []

    from unittest.mock import MagicMock, patch

    fake_result = MagicMock()
    fake_result.ok = True
    fake_result.data = {"state_path": str(tmp_path / "cache.json"), "stopped_reason": "complete"}
    fake_result.exit_code = 0

    def fake_run_action(name: str, params: dict[str, object]) -> object:
        collected.append({"name": name, "params": dict(params)})
        return fake_result

    # The CLI imports run_action locally inside cmd_prove_at_rest, so we patch
    # it at the source module (verdict.actions.registry).
    with patch("verdict.actions.registry.run_action", side_effect=fake_run_action):
        from verdict.cli import cmd_prove_at_rest

        cmd_prove_at_rest("once", allow_live_probe=True, max_wall_seconds=123.0, max_requests=5)

    assert collected, "run_action was never called"
    params = collected[0]["params"]
    assert isinstance(params, dict)
    assert params.get("max_wall_seconds") == 123.0, (
        f"max_wall_seconds not forwarded to run_action params: {params!r}"
    )


# ---------------------------------------------------------------------------
# BOD-292 additive schema-1 fields: cooldowns, last_success_at, merge_and_save
# ---------------------------------------------------------------------------


def test_last_success_at_preserved_across_a_later_failure(tmp_path: Path) -> None:
    cache = HealthCache(tmp_path / "health-cache.json")
    cache.record("free/m", ProbeResult(category=CATEGORY_OK, chat_ok=True, tool_ok=True), NOW)
    first = cache.entry("free/m")
    assert first is not None and first.last_success_at == NOW
    # A later failure overwrites checked_at but keeps the earlier success time.
    later = _at(60)
    cache.record(
        "free/m",
        ProbeResult(category=CATEGORY_UPSTREAM, chat_ok=False, tool_ok=False, http_status=500),
        later,
    )
    failed = cache.entry("free/m")
    assert failed is not None
    assert failed.healthy is False
    assert failed.checked_at == later
    assert failed.last_success_at == NOW  # earlier success history retained


def test_schema1_additive_fields_round_trip_and_old_readers_ignore(tmp_path: Path) -> None:
    from verdict.orchestration.health_cache import ScopedCooldown

    path = tmp_path / "health-cache.json"
    cache = HealthCache(path)
    cache.record("free/m", ProbeResult(category=CATEGORY_OK, chat_ok=True, tool_ok=True), NOW)
    cache.record_cooldown(
        ScopedCooldown(
            key="provider:cc",
            category=CATEGORY_AUTH,
            checked_at=NOW,
            until=_at(3600),
            provider_id="cc",
        )
    )
    cache.save()
    raw = json.loads(path.read_text(encoding="utf-8"))
    # New top-level key present; route carries last_success_at.
    assert "cooldowns" in raw
    assert raw["routes"]["free/m"]["last_success_at"].endswith("Z")
    # A reader that ignores unknown keys still loads routes/buckets fine.
    reloaded = HealthCache(path)
    assert reloaded.lookup("free/m", _at(10)).state == STATE_FRESH
    assert reloaded.cooldown_for("provider:cc", _at(10)) is not None
    assert reloaded.cooldown_for("provider:cc", _at(4000)) is None  # expired


def test_cooldowns_absent_by_default_keeps_snapshot_lean(tmp_path: Path) -> None:
    path = tmp_path / "health-cache.json"
    cache = HealthCache(path)
    cache.record("free/m", ProbeResult(category=CATEGORY_OK, chat_ok=True, tool_ok=True), NOW)
    cache.save()
    raw = json.loads(path.read_text(encoding="utf-8"))
    # No cooldowns were recorded, so the key is omitted (old goldens stable).
    assert "cooldowns" not in raw


def test_record_cooldown_keeps_the_later_deadline(tmp_path: Path) -> None:
    from verdict.orchestration.health_cache import ScopedCooldown

    cache = HealthCache(tmp_path / "health-cache.json")
    cache.record_cooldown(
        ScopedCooldown(
            key="provider:cc", category=CATEGORY_RATE_LIMITED, checked_at=NOW, until=_at(60)
        )
    )
    cache.record_cooldown(
        ScopedCooldown(key="provider:cc", category=CATEGORY_AUTH, checked_at=NOW, until=_at(3600))
    )
    cd = cache.cooldown_for("provider:cc", _at(10))
    assert cd is not None and cd.until == _at(3600)
    # An earlier deadline does not shorten the active one.
    cache.record_cooldown(
        ScopedCooldown(
            key="provider:cc", category=CATEGORY_RATE_LIMITED, checked_at=NOW, until=_at(120)
        )
    )
    current = cache.cooldown_for("provider:cc", _at(10))
    assert current is not None and current.until == _at(3600)


def test_merge_and_save_serializes_a_read_modify_write(tmp_path: Path) -> None:
    path = tmp_path / "health-cache.json"
    # Writer A records one route and saves.
    HealthCache(path).record(
        "free/a", ProbeResult(category=CATEGORY_OK, chat_ok=True, tool_ok=True), NOW
    )
    writer_a = HealthCache(path)
    writer_a.record("free/a", ProbeResult(category=CATEGORY_OK, chat_ok=True, tool_ok=True), NOW)
    writer_a.save()
    # Writer B merges a different route; merge_and_save reloads A's state first.
    writer_b = HealthCache(path)

    def mutate(cache: HealthCache) -> None:
        cache.record("free/b", ProbeResult(category=CATEGORY_OK, chat_ok=True, tool_ok=True), NOW)

    writer_b.merge_and_save(mutate)
    final = HealthCache(path)
    assert final.entry("free/a") is not None  # A's write was not clobbered
    assert final.entry("free/b") is not None  # B's merge landed
