"""Offline regressions for BOD-338 gateway versus credential-pool auth failures."""

from datetime import datetime, timezone
from pathlib import Path

import pytest

from verdict.orchestration.credential_pools import pool_of
from verdict.orchestration.health_cache import HealthCache, ProbeResult
from verdict.prove_at_rest import AdmittedRoute, ProbeExchange, Prober, order_cycle

NOW = datetime(2026, 10, 10, tzinfo=timezone.utc)
pytestmark = pytest.mark.usefixtures("no_gateway_network")


def _routes() -> list[AdmittedRoute]:
    ids = ["af/a", "bm/a", "charm-hyper/a", "af/b", "bm/b"]
    ids += [f"healthy{i}/a" for i in range(4)]
    return [AdmittedRoute(rid, rid.split("/")[0], "subscription", pool_of(rid)) for rid in ids]


@pytest.mark.parametrize("gateway_status", [None, 200])
def test_dead_pools_do_not_stop_healthy_pools(tmp_path: Path, gateway_status: int | None) -> None:
    cache = HealthCache(tmp_path / "cache.json")
    calls: list[str] = []

    def transport(rid: str, phase: str, timeout: float) -> ProbeExchange:
        calls.append(rid)
        healthy = rid.startswith("healthy")
        return ProbeExchange(http_status=200 if healthy else 401, ok=healthy, chat_exact=healthy)

    options = (
        {}
        if gateway_status is None
        else {
            "gateway_auth_check": lambda timeout: ProbeExchange(http_status=gateway_status, ok=True)
        }
    )
    stats = Prober(cache, _routes, transport, clock=lambda: NOW, epsilon=0, **options).run_once()
    assert not stats.auth_outage
    assert stats.stopped_reason == "complete"
    assert stats.fresh == 4
    assert all(cache.entry(f"healthy{i}/a").healthy for i in range(4))
    if gateway_status == 200:
        # Review N1: once af/bm are cooled in this cycle, their second routes
        # are not probed in the same cycle.
        assert "af/b" not in calls and "bm/b" not in calls
    # A later cycle does not probe an alias of a cooled provider, even if it is new.
    calls.clear()
    sibling = AdmittedRoute("api-airforce/new", "api-airforce", "subscription")
    Prober(cache, lambda: [sibling], transport, clock=lambda: NOW, epsilon=0).run_once()
    assert calls == []
    assert cache.entry(sibling.route_id) is None
    assert HealthCache(cache.path).entry("af/a").category == "authentication"


@pytest.mark.parametrize("status", [401, 403])
def test_gateway_key_rejection_stops_without_cache_poison(tmp_path: Path, status: int) -> None:
    cache = HealthCache(tmp_path / "cache.json")
    checks: list[float] = []

    def check(timeout: float) -> ProbeExchange:
        checks.append(timeout)
        return ProbeExchange(http_status=status, ok=False)

    stats = Prober(
        cache,
        _routes,
        lambda *args: ProbeExchange(http_status=401, ok=False),
        clock=lambda: NOW,
        epsilon=0,
        gateway_auth_check=check,
    ).run_once()
    assert stats.auth_outage
    assert stats.stopped_reason == "auth_outage"
    assert len(checks) == 1
    assert stats.requests == 2
    assert stats.probed == stats.negative == 0
    assert HealthCache(cache.path).routes() == {}


@pytest.mark.parametrize("evidence", ["recent_success", "session"])
def test_evidenced_pool_orders_before_cold_pool(tmp_path: Path, evidence: str) -> None:
    cache = HealthCache(tmp_path / "cache.json")
    if evidence == "recent_success":
        cache.record("warm/old", ProbeResult(category="ok", chat_ok=True, tool_ok=True), NOW)
    else:
        cache.record_agentic_evidence("warm/old", passed=True, at=NOW, source="test", child_id="c")
    routes = [AdmittedRoute("cold/a", "cold", "free"), AdmittedRoute("warm/new", "warm", "free")]
    assert [r.route_id for r, _ in order_cycle(routes, cache, NOW, epsilon=0)] == [
        "warm/new",
        "cold/a",
    ]
    # Evidence is only an ordering hint, not new health or admission authority.
    assert cache.entry("warm/new") is None


@pytest.mark.parametrize("status", [None, "protocol", 500, 200])
def test_gateway_check_once_and_non_auth_errors_continue(
    tmp_path: Path, status: int | str | None
) -> None:
    import http.client

    calls: list[float] = []

    def check(timeout: float) -> ProbeExchange:
        calls.append(timeout)
        if status is None:
            raise OSError("offline network error")
        if status == "protocol":
            raise http.client.BadStatusLine("garbage")
        assert isinstance(status, int)
        return ProbeExchange(http_status=status, ok=status == 200)

    cache = HealthCache(tmp_path / "cache.json")
    stats = Prober(
        cache,
        _routes,
        lambda *args: ProbeExchange(http_status=401, ok=False),
        clock=lambda: NOW,
        gateway_auth_check=check,
    ).run_once()
    assert not stats.auth_outage
    assert len(calls) == 1
    assert stats.requests <= 10
    assert cache.entry("af/a").category == "authentication"


def test_gateway_confirmation_respects_request_cap(tmp_path: Path) -> None:
    calls: list[float] = []
    cache = HealthCache(tmp_path / "cache.json")

    def check(timeout: float) -> ProbeExchange:
        calls.append(timeout)
        return ProbeExchange(http_status=401, ok=False)

    prober = Prober(
        cache,
        _routes,
        lambda *args: ProbeExchange(http_status=401, ok=False),
        clock=lambda: NOW,
        max_requests=1,
        gateway_auth_check=check,
    )
    stats = prober.run_once()
    assert stats.requests == 1
    assert stats.stopped_reason == "request_cap"
    assert calls == []
    assert cache.routes() == {}
    assert cache.cursor["probed_ids"] == []


def test_gateway_check_resets_each_cycle(tmp_path: Path) -> None:
    calls: list[float] = []
    cache = HealthCache(tmp_path / "cache.json")

    def check(timeout: float) -> ProbeExchange:
        calls.append(timeout)
        return ProbeExchange(http_status=401, ok=False)

    prober = Prober(
        cache,
        _routes,
        lambda *args: ProbeExchange(http_status=401, ok=False),
        clock=lambda: NOW,
        gateway_auth_check=check,
    )
    assert prober.run_once().auth_outage
    assert prober.run_once().auth_outage
    assert len(calls) == 2
    assert cache.routes() == {}


def test_pool_cooldown_expires_and_route_failure_stays_local(tmp_path: Path) -> None:
    from datetime import timedelta

    cache = HealthCache(tmp_path / "cache.json")
    entry = cache.record(
        "af/a",
        ProbeResult(category="authentication", http_status=401, chat_ok=False, tool_ok=False),
        NOW,
    )
    sibling = AdmittedRoute("api-airforce/new", "api-airforce", "free")
    assert order_cycle([sibling], cache, NOW) == []
    assert order_cycle([sibling], cache, entry.until + timedelta(seconds=1))
    cache.record(
        "af/a",
        ProbeResult(category="not_found", http_status=404, chat_ok=False, tool_ok=False),
        NOW,
    )
    assert order_cycle([sibling], cache, NOW)


@pytest.mark.parametrize("base_url", ["https://gateway.invalid", "https://gateway.invalid/v1"])
def test_live_gateway_check_uses_same_key_and_models_endpoint(
    base_url: str, monkeypatch: pytest.MonkeyPatch, no_gateway_network: None
) -> None:
    from verdict.prove_at_rest import live_gateway_auth_check

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

    seen: list[tuple[str, str, str, float]] = []

    def open_fake(request, *, timeout):
        seen.append(
            (request.full_url, request.get_method(), request.get_header("Authorization"), timeout)
        )
        return Response()

    monkeypatch.setattr("verdict.prove_at_rest.open_no_redirect", open_fake)
    result = live_gateway_auth_check(base_url, api_key="fixture-key")(4.0)
    assert result.http_status == 200
    assert seen == [("https://gateway.invalid/v1/models", "GET", "Bearer fixture-key", 4.0)]


def test_gateway_confirmation_timeout_is_bounded_by_wall_budget(tmp_path: Path) -> None:
    calls: list[float] = []
    ticks = [0.0]

    def transport(*args: object) -> ProbeExchange:
        ticks[0] = 9.0
        return ProbeExchange(http_status=401, ok=False)

    def check(timeout: float) -> ProbeExchange:
        calls.append(timeout)
        return ProbeExchange(http_status=401, ok=False)

    stats = Prober(
        HealthCache(tmp_path / "cache.json"),
        _routes,
        transport,
        clock=lambda: NOW,
        monotonic=lambda: ticks[0],
        max_wall_seconds=10.0,
        probe_timeout_seconds=15.0,
        gateway_auth_check=check,
    ).run_once()
    assert stats.auth_outage
    assert len(calls) == 1
    assert calls == [1.0]
