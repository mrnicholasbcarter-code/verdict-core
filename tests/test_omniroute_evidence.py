"""Synthetic OmniRoute evidence at the identification boundary."""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from verdict.omniroute_evidence import parse_omniroute_evidence, stale_connections

NOW = datetime(2026, 10, 20, 13, tzinfo=timezone.utc)


@pytest.fixture
def sources():
    return json.loads(
        (Path(__file__).parent / "fixtures/omniroute_evidence/snapshot.json").read_text()
    )


def test_connection_pass_requires_resilience_and_capacity(sources):
    evidence = parse_omniroute_evidence(**sources, now=NOW)
    assert evidence.usable_providers == frozenset({"alpha"})
    assert {c.connection_id: c.usable for c in evidence.connections} == {
        "ok": True,
        "cool": False,
        "empty": False,
    }
    assert not evidence.source_errors


def test_traffic_maps_exact_catalog_route_and_excludes_error_only(sources):
    evidence = parse_omniroute_evidence(**sources, now=NOW)
    assert set(evidence.proven_by_traffic) == {"a/best"}
    traffic = evidence.proven_by_traffic["a/best"]
    assert traffic.provider == "alpha"
    assert traffic.successes == 3
    assert traffic.last_request_at == NOW - timedelta(hours=1)


def test_missing_matrix_is_unknown_not_proven(sources):
    sources["health_matrix"] = None
    evidence = parse_omniroute_evidence(**sources, now=NOW)
    assert "health_matrix" in evidence.source_errors
    assert not evidence.proven_by_traffic


@pytest.mark.parametrize("source", ["providers", "resilience", "limits", "cooldowns"])
def test_missing_availability_source_fails_closed(sources, source):
    sources[source] = None
    evidence = parse_omniroute_evidence(**sources, now=NOW)
    assert source in evidence.source_errors
    assert not evidence.usable_providers


def test_stale_connections_use_snapshot_time(sources):
    sources["providers"]["connections"][1]["lastTested"] = None
    evidence = parse_omniroute_evidence(**sources, now=NOW)
    assert stale_connections(evidence, max_age=timedelta(hours=2)) == ("cool",)
    assert stale_connections(evidence, max_age=timedelta(minutes=30)) == ("ok", "cool", "empty")


@pytest.mark.parametrize("state", ["OPEN", "HALF_OPEN"])
def test_open_breaker_excludes_otherwise_passing_connection(sources, state):
    sources["resilience"]["breakers"] = [{"name": "alpha", "state": state}]
    evidence = parse_omniroute_evidence(**sources, now=NOW)
    assert "alpha" not in evidence.usable_providers


def test_expired_quota_window_no_longer_blocks_connection(sources):
    sources["limits"]["caches"]["empty"]["quotas"]["weekly"]["resetAt"] = "2026-10-20T12:59:00Z"
    evidence = parse_omniroute_evidence(**sources, now=NOW)
    assert "gamma" in evidence.usable_providers


def test_absent_connection_limit_cache_is_not_unlimited(sources):
    del sources["limits"]["caches"]["ok"]
    evidence = parse_omniroute_evidence(**sources, now=NOW)
    assert "alpha" not in evidence.usable_providers


def test_catalog_aliases_and_cooldowns_retain_exact_ids(sources):
    sources["v1_models"]["data"].append({"id": "alpha/best", "root": "best", "owned_by": "alpha"})
    sources["health_matrix"]["providers"][0]["accounts"][0]["models"].append(
        {"model": "unknown", "status": "healthy", "requests": 1, "successes": 1, "lastStatus": 200}
    )
    sources["cooldowns"]["items"] = [{"provider": "alpha", "connectionId": "ok", "model": "best"}]
    evidence = parse_omniroute_evidence(**sources, now=NOW)
    assert set(evidence.proven_by_traffic) == {"a/best", "alpha/best"}
    assert evidence.cooled_models == frozenset({"best", "a/best", "alpha/best"})


def test_latest_error_is_not_proof_even_after_old_success(sources):
    model = sources["health_matrix"]["providers"][0]["accounts"][0]["models"][0]
    model.update(status="error", successes=2, lastStatus=403)
    evidence = parse_omniroute_evidence(**sources, now=NOW)
    assert not evidence.proven_by_traffic


def test_fetch_is_get_only_and_retains_source_failure(sources, monkeypatch):
    from io import BytesIO
    from urllib.error import HTTPError

    from verdict import omniroute_evidence as module

    paths = {
        "/api/providers": "providers",
        "/api/resilience/connections": "resilience",
        "/api/usage/provider-limits": "limits",
        "/api/resilience/model-cooldowns": "cooldowns",
        "/api/providers/health-matrix?range=7d": "health_matrix",
        "/v1/models": "v1_models",
    }
    visited = []

    def transport(request, *, timeout):
        path = request.full_url.removeprefix("http://gateway")
        visited.append(path)
        assert request.get_method() == "GET"
        assert request.get_header("Authorization") == "Bearer resolved-secret"
        assert timeout == 7
        if paths[path] == "health_matrix":
            raise HTTPError(request.full_url, 503, "resolved-secret", {}, None)
        return BytesIO(json.dumps(sources[paths[path]]).encode())

    monkeypatch.setattr(module, "resolve_api_key", lambda: "resolved-secret")
    monkeypatch.setattr(module, "open_no_redirect", transport)
    evidence = module.fetch_omniroute_evidence("http://gateway/", api_key=None, timeout=7)
    assert set(visited) == set(paths)
    assert evidence.source_errors == ("health_matrix",)
    assert not evidence.proven_by_traffic
    assert "resolved-secret" not in repr(evidence)


def test_batch_is_explicit_selected_and_does_not_trust_local_pass(sources, monkeypatch):
    from io import BytesIO

    from verdict import omniroute_evidence as module

    posts = []
    snapshot = parse_omniroute_evidence(**sources, now=NOW)

    def transport(request, *, timeout):
        assert request.get_method() == "POST"
        assert request.full_url == "http://gateway/api/providers/test-batch"
        posts.append(json.loads(request.data))
        return BytesIO(json.dumps({"results": [{"connectionId": "cool", "valid": True}]}).encode())

    monkeypatch.setattr(module, "open_no_redirect", transport)
    monkeypatch.setattr(module, "fetch_omniroute_evidence", lambda *args, **kwargs: snapshot)
    result = module.run_test_batch("http://gateway", api_key="secret", connection_ids=["cool"])
    assert posts == [{"mode": "selected", "connectionIds": ["cool"]}]
    assert not result["cool"].usable
    assert module.run_test_batch("http://gateway", api_key="secret", connection_ids=[]) == {}
