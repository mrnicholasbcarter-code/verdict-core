"""Admit snapshot reads survive transient OmniRoute failures (census S5)."""

from __future__ import annotations

import contextlib
import json
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

from verdict.catalog_cache import put_cached_catalog
from verdict.free_tier_admit import LiveAdmitError, load_omniroute_admit_snapshot

BASE = "http://127.0.0.1:20128"
ROUTE = "openrouter/nvidia/nemotron-3-nano-30b-a3b:free"

PAYLOADS: dict[str, object] = {
    "/v1/models": {"data": [{"id": ROUTE, "object": "model", "owned_by": "openrouter"}]},
    "/api/free-tier/summary": {
        "perModel": [{"modelId": "nvidia/nemotron-3-nano-30b-a3b:free", "provider": "openrouter"}]
    },
    "/api/v1/me/status": {"accountQuotas": []},
    "/api/providers": {
        "connections": [{"provider": "openrouter", "isActive": True, "testStatus": "active"}],
        "total": 1,
    },
    "/api/usage/model-latency-stats": {"entries": []},
    "/api/resilience/connections": {"connections": []},
}


def _transport(
    failing: Callable[[str, int], bool], seen: list[str] | None = None
) -> httpx.MockTransport:
    counts: dict[str, int] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        assert "auto" not in path and "combo" not in path
        counts[path] = counts.get(path, 0) + 1
        if seen is not None:
            seen.append(path)
        if failing(path, counts[path]):
            raise httpx.ReadTimeout("slow", request=request)
        if path not in PAYLOADS:
            return httpx.Response(404)
        return httpx.Response(200, json=PAYLOADS[path])

    return httpx.MockTransport(handler)


@pytest.fixture()
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("VERDICT_CATALOG_CACHE", str(tmp_path / "catalog-cache.json"))
    return tmp_path / "admit-last-good.json"


def _load(last_good: Path, transport: httpx.MockTransport) -> object:
    return load_omniroute_admit_snapshot(
        BASE,
        "test-token",
        transport=transport,
        last_good_path=last_good,
        sleep=lambda _seconds: None,
    )


def test_single_providers_timeout_is_retried_and_yields_a_snapshot(isolated: Path) -> None:
    seen: list[str] = []
    snapshot = _load(
        isolated, _transport(lambda path, n: path == "/api/providers" and n == 1, seen)
    )
    assert seen.count("/api/providers") == 2
    assert snapshot.stale_served == ()  # type: ignore[attr-defined]
    assert snapshot.active_providers == frozenset({"openrouter"})  # type: ignore[attr-defined]


def test_providers_failing_after_retry_is_served_from_last_good(isolated: Path) -> None:
    _load(isolated, _transport(lambda _p, _n: False))
    snapshot = _load(isolated, _transport(lambda path, _n: path == "/api/providers"))
    assert snapshot.stale_served == ("providers",)  # type: ignore[attr-defined]
    assert snapshot.active_providers == frozenset({"openrouter"})  # type: ignore[attr-defined]


def test_stale_catalog_cache_and_slow_catalog_read_does_not_raise(isolated: Path) -> None:
    put_cached_catalog(
        "http://127.0.0.1:20128",
        "test-token",
        PAYLOADS["/v1/models"],
        now=datetime.now(timezone.utc) - timedelta(minutes=10),
        ttl_seconds=300,
    )
    snapshot = _load(isolated, _transport(lambda path, _n: path == "/v1/models"))
    assert snapshot.stale_served == ("catalog",)  # type: ignore[attr-defined]
    assert [row.identity_id for row in snapshot.catalog] == [ROUTE]  # type: ignore[attr-defined]


def test_no_last_good_and_all_failing_raises_a_clear_error(isolated: Path) -> None:
    with pytest.raises(LiveAdmitError) as caught:
        _load(isolated, _transport(lambda _p, _n: True))
    assert caught.value.code == "timeout"
    assert "no last-good snapshot" in str(caught.value)


def test_last_good_older_than_limit_is_not_served(isolated: Path) -> None:
    _load(isolated, _transport(lambda _p, _n: False))
    data = json.loads(isolated.read_text())
    old = (datetime.now(timezone.utc) - timedelta(minutes=16)).isoformat()
    for row in data[BASE].values():
        row["fetched_at"] = old
    isolated.write_text(json.dumps(data))
    with pytest.raises(LiveAdmitError, match="no last-good snapshot"):
        _load(isolated, _transport(lambda path, _n: path == "/api/providers"))


def test_successful_load_refreshes_last_good_atomically(isolated: Path) -> None:
    _load(isolated, _transport(lambda _p, _n: False))
    data = json.loads(isolated.read_text())
    assert set(data[BASE]) == {"free_tier", "providers", "account_status"}
    assert oct(isolated.stat().st_mode & 0o777) == "0o600"
    assert not list(isolated.parent.glob(".admit-last-good.json.*.tmp"))


def test_unauthorized_is_never_masked_by_last_good(isolated: Path) -> None:
    _load(isolated, _transport(lambda _p, _n: False))

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/providers":
            return httpx.Response(401)
        return httpx.Response(200, json=PAYLOADS.get(request.url.path, {}))

    with pytest.raises(LiveAdmitError) as caught:
        _load(isolated, httpx.MockTransport(handler))
    assert caught.value.code == "unauthorized"


def test_catalog_read_gets_its_own_longer_timeout(isolated: Path) -> None:
    timeouts: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        timeouts[request.url.path] = request.extensions["timeout"]["read"]
        return httpx.Response(200, json=PAYLOADS.get(request.url.path, {}))

    _load(isolated, httpx.MockTransport(handler))
    assert timeouts["/v1/models"] == 60.0
    assert timeouts["/api/providers"] == 20.0


def _gated_transport(
    seen: list[tuple[str, str]], fail_provider: str | None = None
) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        seen.append((request.method, path))
        if path == "/api/providers/test-batch":
            raise AssertionError("census/admit must never POST /api/providers/test-batch")
        assert request.method == "GET"
        assert path != "/v1/models", "gated census must not pull the full catalog"
        if path.startswith("/api/v1/providers/"):
            provider = path.split("/")[4]
            if provider == fail_provider:
                raise httpx.ReadTimeout("slow", request=request)
            return httpx.Response(
                200,
                json={"object": "list", "data": [{"id": "nvidia/nemotron-3-nano-30b-a3b:free"}]},
            )
        if path not in PAYLOADS:
            return httpx.Response(404)
        return httpx.Response(200, json=PAYLOADS[path])

    return httpx.MockTransport(handler)


def _load_gated(last_good: Path, transport: httpx.MockTransport) -> object:
    return load_omniroute_admit_snapshot(
        BASE,
        "test-token",
        transport=transport,
        last_good_path=last_good,
        sleep=lambda _seconds: None,
        catalog_scope="gated",
        catalog_cache_path=last_good.parent / "census-catalog-cache.json",
    )


def test_gated_catalog_reads_only_gated_provider_models(isolated: Path) -> None:
    seen: list[tuple[str, str]] = []
    snapshot = _load_gated(isolated, _gated_transport(seen))
    assert ("GET", "/api/v1/providers/openrouter/models") in seen
    assert [p for _m, p in seen if p.startswith("/api/v1/providers/")] == [
        "/api/v1/providers/openrouter/models"
    ]
    assert [row.identity_id for row in snapshot.catalog] == [ROUTE]  # type: ignore[attr-defined]
    assert snapshot.stale_served == ()  # type: ignore[attr-defined]


def test_gated_catalog_failure_falls_back_to_last_good(isolated: Path) -> None:
    _load_gated(isolated, _gated_transport([]))
    data = json.loads((isolated.parent / "census-catalog-cache.json").read_text())
    for entry in data["entries"].values():
        entry["cached_at"] = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    (isolated.parent / "census-catalog-cache.json").write_text(json.dumps(data))
    seen: list[tuple[str, str]] = []
    snapshot = _load_gated(isolated, _gated_transport(seen, fail_provider="openrouter"))
    assert seen.count(("GET", "/api/v1/providers/openrouter/models")) == 2
    assert snapshot.stale_served == ("catalog",)  # type: ignore[attr-defined]
    assert [row.identity_id for row in snapshot.catalog] == [ROUTE]  # type: ignore[attr-defined]


def test_gated_catalog_failure_without_last_good_fails_closed(isolated: Path) -> None:
    with pytest.raises(LiveAdmitError, match="no last-good snapshot"):
        _load_gated(isolated, _gated_transport([], fail_provider="openrouter"))


def test_census_and_admit_paths_never_post_test_batch(isolated: Path) -> None:
    from verdict.census_passports import load_census_cooldowns

    seen: list[tuple[str, str]] = []
    transport = _gated_transport(seen)
    _load_gated(isolated, transport)
    _load(isolated, _transport(lambda _p, _n: False, None))
    with contextlib.suppress(Exception):  # only the request log matters here
        load_census_cooldowns(BASE, transport=transport)
    assert all(method == "GET" for method, _ in seen)
    assert not any("test-batch" in path for _, path in seen)


def test_census_code_path_has_no_test_batch_call() -> None:
    import verdict.census_passports
    import verdict.census_passports_service
    import verdict.free_tier_admit
    import verdict.omniroute_health

    for module in (
        verdict.census_passports,
        verdict.census_passports_service,
        verdict.free_tier_admit,
        verdict.omniroute_health,
    ):
        assert "test-batch" not in Path(str(module.__file__)).read_text(encoding="utf-8")


def test_provider_model_ids_are_upstream_ids_and_always_get_the_provider_prefix() -> None:
    # Verified 2026-10-17 on OmniRoute 3.8.51: GET /api/v1/providers/nvidia/models
    # returns {object, data[119]} with upstream ids; the unified /v1/models id is
    # always "<provider>/<upstream id>" (47 of them are nvidia/nvidia/...).
    from verdict.free_tier_admit import _merge_provider_catalogs

    merged = _merge_provider_catalogs(
        {
            "nvidia": {
                "object": "list",
                "data": [
                    {"id": "nvidia/nemotron-mini", "object": "model", "owned_by": "nvidia"},
                    {"id": "meta/llama-3.1-8b", "object": "model", "owned_by": "nvidia"},
                    {"id": "plain-model", "object": "model", "owned_by": "nvidia"},
                ],
            }
        }
    )
    assert [row["id"] for row in merged["data"]] == [  # type: ignore[index]
        "nvidia/nvidia/nemotron-mini",
        "nvidia/meta/llama-3.1-8b",
        "nvidia/plain-model",
    ]
