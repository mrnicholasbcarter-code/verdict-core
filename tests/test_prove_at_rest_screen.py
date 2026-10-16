"""BOD-410: the M1 evidence order (cheap test-all screen, then the tool probe on survivors)."""

from __future__ import annotations

import io
import json
import urllib.error
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from email.message import Message
from pathlib import Path
from typing import Any

import pytest

from tests.test_prove_at_rest_agentic_budget import _turn
from verdict.orchestration.health_cache import HealthCache, ProbeResult
from verdict.prove_at_rest import (
    AdmittedRoute,
    CycleStats,
    ProbeExchange,
    Prober,
    _cooled_pools,
    _http_error_exchange,
    live_screen_transport,
)

NOW = datetime(2026, 10, 16, 6, 0, tzinfo=timezone.utc)
pytestmark = pytest.mark.usefixtures("no_gateway_network")


def _free(route_id: str) -> AdmittedRoute:
    provider = route_id.split("/", 1)[0]
    return AdmittedRoute(route_id, provider, "free", pool=provider, live_catalog=True)


class _Screen:
    """Fake POST /api/models/test-all: one canned result per model id."""

    def __init__(self, results: Mapping[str, Mapping[str, Any]]) -> None:
        self.results = results
        self.calls: list[tuple[str, list[str]]] = []

    def __call__(
        self, provider_id: str, model_ids: Sequence[str], timeout: float
    ) -> Mapping[str, Mapping[str, Any]] | None:
        self.calls.append((provider_id, list(model_ids)))
        return {mid: self.results[mid] for mid in model_ids if mid in self.results}


def _prober(
    cache: HealthCache, routes: list[AdmittedRoute], screen: _Screen, agentic: Any
) -> Prober:
    return Prober(
        cache,
        lambda: routes,
        lambda rid, phase, timeout: ProbeExchange(http_status=500, ok=False),
        agentic_transport=agentic,
        screen_transport=screen,
        clock=lambda: NOW,
    )


def _cool(cache: HealthCache, route_id: str) -> None:
    pool = route_id.split("/", 1)[0]
    cache.record(route_id, ProbeResult("permission", False, False, http_status=403, pool=pool), NOW)


def test_screen_survivor_in_a_cooled_pool_gets_the_tool_probe(tmp_path: Path) -> None:
    cache = HealthCache(tmp_path / "cache.json", bucket_capacity=100)
    _cool(cache, "q/dead")
    assert "q" in _cooled_pools(cache, NOW)
    routes = [_free("q/a"), _free("q/b")]
    screen = _Screen({"q/a": {"status": "ok"}, "q/b": {"status": "ok"}})
    probed: list[str] = []

    def agentic(rid: str, payload: dict[str, Any], timeout: float) -> ProbeExchange:
        probed.append(rid)
        return _turn(rid, payload)

    stats = CycleStats()
    _prober(cache, routes, screen, agentic).run_agentic_probes(stats, routes=routes)
    assert screen.calls == [("q", ["q/a"])], "a cooled pool gets one half-open screen"
    assert set(probed) == {"q/a"}
    assert stats.agentic_passed == 1
    assert stats.screened == 1 and stats.screen_ok == 1


def test_screen_ok_alone_never_admits(tmp_path: Path) -> None:
    cache = HealthCache(tmp_path / "cache.json", bucket_capacity=100)
    routes = [_free("p/a"), _free("p/b")]
    screen = _Screen(
        {
            "p/a": {"status": "error", "statusCode": 404, "error": "[404]: gone"},
            "p/b": {"status": "ok"},
        }
    )
    probed: list[str] = []

    def agentic(rid: str, payload: dict[str, Any], timeout: float) -> ProbeExchange:
        probed.append(rid)
        return ProbeExchange(http_status=502, ok=False, error_category="upstream")

    stats = CycleStats()
    _prober(cache, routes, screen, agentic).run_agentic_probes(stats, routes=routes)
    assert probed == ["p/b"], "a failed screen skips the tool probe"
    failed = cache.entry("p/a")
    assert failed is not None and not failed.healthy and failed.category == "not_found"
    survivor = cache.entry("p/b")
    assert survivor is not None and not survivor.healthy
    assert stats.agentic_passed == 0


def test_account_level_screen_error_cools_the_pool(tmp_path: Path) -> None:
    cache = HealthCache(tmp_path / "cache.json", bucket_capacity=100)
    routes = [_free("p/a"), _free("p/b")]
    screen = _Screen(
        {
            "p/a": {
                "status": "error",
                "statusCode": 403,
                "error": "[403]: Key limit exceeded (total limit)",
            },
            "p/b": {
                "status": "error",
                "statusCode": 403,
                "error": "[403]: Key limit exceeded (total limit)",
            },
        }
    )
    probed: list[str] = []
    stats = CycleStats()
    _prober(
        cache, routes, screen, lambda rid, p, t: probed.append(rid) or _turn(rid, p)
    ).run_agentic_probes(stats, routes=routes)
    assert "p" in _cooled_pools(cache, NOW)
    assert probed == []


def test_model_scoped_screen_403_leaves_the_pool_open(tmp_path: Path) -> None:
    cache = HealthCache(tmp_path / "cache.json", bucket_capacity=100)
    routes = [_free("p/x:free"), _free("p/y")]
    screen = _Screen(
        {
            "p/x:free": {
                "status": "error",
                "statusCode": 403,
                "error": "[403]: x:free is only available on agentic harnesses",
            },
            "p/y": {"status": "ok"},
        }
    )
    stats = CycleStats()
    _prober(cache, routes, screen, lambda rid, p, t: _turn(rid, p)).run_agentic_probes(
        stats, routes=routes
    )
    entry = cache.entry("p/x:free")
    assert entry is not None and entry.failure_scope == "route"
    assert "p" not in _cooled_pools(cache, NOW)
    assert stats.agentic_passed == 1


def test_rate_limited_screen_writes_no_route_negative(tmp_path: Path) -> None:
    cache = HealthCache(tmp_path / "cache.json", bucket_capacity=100)
    routes = [_free("p/a")]
    screen = _Screen({"p/a": {"status": "error", "rateLimited": True, "error": "[429]: slow down"}})
    stats = CycleStats()
    _prober(cache, routes, screen, lambda rid, p, t: _turn(rid, p)).run_agentic_probes(
        stats, routes=routes
    )
    assert cache.entry("p/a") is None
    assert not cache.consume("p", NOW, pool="p")


def test_agentic_403_naming_the_model_is_route_scoped(tmp_path: Path) -> None:
    body = (
        b'{"error":{"message":"[p/x:free] [403]: x:free is only available on agentic harnesses"}}'
    )
    exc = urllib.error.HTTPError("http://gw", 403, "Forbidden", Message(), io.BytesIO(body))
    exchange = _http_error_exchange(exc, 5.0)
    assert "agentic harnesses" in exchange.detail
    cache = HealthCache(tmp_path / "cache.json", bucket_capacity=100)
    routes = [_free("p/x:free")]
    prober = Prober(
        cache,
        lambda: routes,
        lambda rid, phase, timeout: ProbeExchange(http_status=500, ok=False),
        agentic_transport=lambda rid, p, t: exchange,
        clock=lambda: NOW,
    )
    prober.run_agentic_probes(CycleStats(), routes=routes)
    entry = cache.entry("p/x:free")
    assert entry is not None and entry.failure_scope == "route"
    assert "p" not in _cooled_pools(cache, NOW)


def test_live_screen_transport_posts_the_m1_test_all_body(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[Any] = []

    class _Response(io.BytesIO):
        status = 200

        def __enter__(self) -> _Response:
            return self

        def __exit__(self, *args: object) -> None:
            return None

    def fake_open(request: Any, timeout: float) -> _Response:
        sent.append((request.full_url, json.loads(request.data)))
        return _Response(json.dumps({"results": {"p/a": {"status": "ok"}}}).encode())

    monkeypatch.setattr("verdict.prove_at_rest.open_no_redirect", fake_open)
    screen = live_screen_transport("http://gw/v1", api_key="k")
    assert screen("p", ["p/a"], 5.0) == {"p/a": {"status": "ok"}}
    url, body = sent[0]
    assert url == "http://gw/api/models/test-all"
    assert body == {
        "providerId": "p",
        "modelIds": ["p/a"],
        "respectRateLimit": True,
        "autoHideFailed": False,
    }
