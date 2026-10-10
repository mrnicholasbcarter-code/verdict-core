"""Offline regressions for BOD-338 gateway versus credential-pool auth failures."""

from datetime import datetime, timezone
from pathlib import Path

import pytest

from verdict.orchestration.credential_pools import pool_of
from verdict.orchestration.health_cache import HealthCache, ProbeResult
from verdict.prove_at_rest import AdmittedRoute, ProbeExchange, Prober, order_cycle

NOW = datetime(2026, 10, 10, tzinfo=timezone.utc)


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

    options = {} if gateway_status is None else {
        "gateway_auth_check": lambda timeout: ProbeExchange(http_status=gateway_status, ok=True)
    }
    stats = Prober(cache, _routes, transport, clock=lambda: NOW, epsilon=0, **options).run_once()
    assert not stats.auth_outage
    assert stats.stopped_reason == "complete"
    assert stats.fresh == 4
    assert all(cache.entry(f"healthy{i}/a").healthy for i in range(4))
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
        cache, _routes, lambda *args: ProbeExchange(http_status=401, ok=False),
        clock=lambda: NOW, epsilon=0, gateway_auth_check=check,
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
    routes = [
        AdmittedRoute("cold/a", "cold", "free"),
        AdmittedRoute("warm/new", "warm", "free"),
    ]
    assert [r.route_id for r, _ in order_cycle(routes, cache, NOW, epsilon=0)] == [
        "warm/new", "cold/a"
    ]
    # Evidence is only an ordering hint, not new health or admission authority.
    assert cache.entry("warm/new") is None
