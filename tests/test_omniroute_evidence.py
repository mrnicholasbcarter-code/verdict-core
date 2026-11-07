"""Synthetic OmniRoute evidence at the identification boundary."""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from verdict.omniroute_evidence import LiveCandidate, QuotaWindow, parse_omniroute_evidence

NOW = datetime(2026, 10, 20, 13, tzinfo=timezone.utc)
PATHS = {
    "/api/providers": "providers",
    "/api/resilience/connections": "resilience",
    "/api/usage/provider-limits": "limits",
    "/api/resilience/model-cooldowns": "cooldowns",
    "/api/providers/health-matrix?range=7d": "health_matrix",
    "/v1/models": "v1_models",
    "/v1/auto-combo/auto/candidates": "candidates",
}


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
    assert "alpha" in evidence.usable_providers
    connection = next(c for c in evidence.connections if c.connection_id == "ok")
    assert connection.quota_state == "unknown"
    assert connection.quota_reason == "cache_missing"


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


def test_oversized_source_fails_closed_not_partial_healthy(sources, monkeypatch):
    from io import BytesIO

    from verdict import omniroute_evidence as module

    def transport(request, *, timeout):
        path = request.full_url.removeprefix("http://gateway")
        name = PATHS[path]
        return BytesIO(json.dumps(sources[name]).encode())

    monkeypatch.setattr(module, "open_no_redirect", transport)
    evidence = module.fetch_omniroute_evidence(
        "http://gateway/", api_key=None, timeout=7, decoded_byte_budget=10
    )
    assert set(evidence.source_errors) == {
        "providers",
        "resilience",
        "limits",
        "cooldowns",
        "health_matrix",
        "v1_models",
        "candidates",
    }
    assert not evidence.usable_providers
    assert not evidence.live_candidates
    assert not evidence.proven_by_traffic


def test_raw_row_limit_fails_source_closed(sources, monkeypatch):
    from io import BytesIO

    from verdict import omniroute_evidence as module

    oversized = dict(sources["v1_models"])
    oversized["data"] = list(sources["v1_models"]["data"]) + [
        {"id": f"pad/{i}", "owned_by": "pad", "root": str(i)} for i in range(5)
    ]

    def transport(request, *, timeout):
        path = request.full_url.removeprefix("http://gateway")
        if path == "/v1/models":
            return BytesIO(json.dumps(oversized).encode())
        name = PATHS[path]
        return BytesIO(json.dumps(sources[name]).encode())

    monkeypatch.setattr(module, "open_no_redirect", transport)
    evidence = module.fetch_omniroute_evidence(
        "http://gateway/", api_key="legacy-key", timeout=7, raw_row_limit=6
    )
    assert evidence.source_errors == ("v1_models",)
    assert "alpha" in evidence.usable_providers


def test_aggregate_deadline_exceeded_fails_remaining_sources_closed(sources, monkeypatch):
    from io import BytesIO

    from verdict import omniroute_evidence as module

    def transport(request, *, timeout):
        path = request.full_url.removeprefix("http://gateway")
        name = PATHS[path]
        return BytesIO(json.dumps(sources[name]).encode())

    monkeypatch.setattr(module, "open_no_redirect", transport)
    evidence = module.fetch_omniroute_evidence(
        "http://gateway/", api_key=None, timeout=7, aggregate_deadline_seconds=0.0
    )
    assert evidence.source_errors == (
        "providers",
        "resilience",
        "limits",
        "cooldowns",
        "health_matrix",
        "v1_models",
        "candidates",
    )


@pytest.mark.parametrize("inference", [None, "inference-sentinel"])
def test_split_credentials_are_confined_and_missing_inference_is_local(
    sources, monkeypatch, inference
):
    from io import BytesIO

    from verdict import omniroute_evidence as module

    seen = []

    def transport(request, *, timeout):
        path = request.full_url.removeprefix("http://gateway")
        seen.append((path, request.get_header("Authorization")))
        name = PATHS[path]
        return BytesIO(json.dumps(sources[name]).encode())

    monkeypatch.setattr(module, "resolve_api_key", lambda: None)
    monkeypatch.setattr(module, "open_no_redirect", transport)
    evidence = module.fetch_omniroute_evidence(
        "http://gateway/", api_key=inference, management_token=" management-sentinel ", timeout=7
    )
    assert len(seen) == (7 if inference else 5)
    assert all(
        auth == "Bearer management-sentinel" for path, auth in seen if path.startswith("/api/")
    )
    if inference:
        assert ("/v1/models", "Bearer inference-sentinel") in seen
        assert ("/v1/auto-combo/auto/candidates", "Bearer inference-sentinel") in seen
        assert not evidence.source_errors
        assert evidence.live_candidates["a/best"].reachable
    else:
        assert {"v1_models", "candidates"} <= set(evidence.source_errors)
        assert not any(path.startswith("/v1/") for path, _ in seen)
        assert not evidence.live_candidates


def test_management_401_does_not_fallback_or_retry(sources, monkeypatch):
    from urllib.error import HTTPError

    from verdict import omniroute_evidence as module

    visited = []

    def transport(request, *, timeout):
        visited.append((request.full_url, request.get_header("Authorization")))
        raise HTTPError(request.full_url, 401, "management-sentinel", {}, None)

    monkeypatch.setattr(module, "resolve_api_key", lambda: "inference-sentinel")
    monkeypatch.setattr(module, "open_no_redirect", transport)
    evidence = module.fetch_omniroute_evidence(
        "http://gateway/", api_key=None, management_token="management-sentinel"
    )
    assert len(visited) == len(set(url for url, _ in visited)) == 7
    assert len(evidence.source_errors) == 7
    assert all(
        auth
        == (
            "Bearer inference-sentinel"
            if url.startswith("http://gateway/v1/")
            else "Bearer management-sentinel"
        )
        for url, auth in visited
    )
    assert "management-sentinel" not in repr(evidence)


def test_empty_inference_catalog_does_not_invent_unmapped_historical_routes(sources):
    sources["v1_models"] = {"data": []}
    evidence = parse_omniroute_evidence(**sources, now=NOW)
    assert "v1_models" not in evidence.source_errors
    assert not evidence.inventory_route_ids
    assert not evidence.proven_by_traffic
    # Pre-existing parser limitation: matrix models require catalog route mapping.
    assert not evidence.traffic_by_route
    assert sources["health_matrix"]["providers"]


def test_both_credentials_missing_never_accesses_network(monkeypatch):
    from verdict import omniroute_evidence as module

    monkeypatch.setattr(module, "resolve_api_key", lambda: None)

    def forbidden(*args, **kwargs):
        raise AssertionError("unexpected network")

    monkeypatch.setattr(module, "open_no_redirect", forbidden)
    evidence = module.fetch_omniroute_evidence("http://gateway", api_key=None)
    assert len(evidence.source_errors) == 7
    assert not evidence.live_candidates
    assert not evidence.inventory_route_ids


@pytest.mark.parametrize("management_token", [None, "   "])
def test_absent_management_token_uses_legacy_api_key_for_management_reads(
    sources, monkeypatch, management_token
):
    from io import BytesIO

    from verdict import omniroute_evidence as module

    seen = []

    def transport(request, *, timeout):
        path = request.full_url.removeprefix("http://gateway")
        seen.append((path, request.get_method(), request.get_header("Authorization")))
        name = PATHS[path]
        return BytesIO(json.dumps(sources[name]).encode())

    monkeypatch.setattr(module, "resolve_api_key", lambda: None)
    monkeypatch.setattr(module, "open_no_redirect", transport)
    evidence = module.fetch_omniroute_evidence(
        "http://gateway/", api_key="legacy-sentinel", management_token=management_token, timeout=7
    )
    # Legacy fallback only when the management token is absent: all seven bounded
    # GETs carry the inference key, nothing is POSTed, and no source fails.
    assert len(seen) == len({path for path, _, _ in seen}) == 7
    assert all(method == "GET" for _, method, _ in seen)
    assert all(auth == "Bearer legacy-sentinel" for _, _, auth in seen)
    assert not evidence.source_errors
    assert "legacy-sentinel" not in repr(evidence)


def test_forbidden_inference_catalog_is_unknown_and_never_cross_retries(sources, monkeypatch):
    from io import BytesIO
    from urllib.error import HTTPError

    from verdict import omniroute_evidence as module

    seen = []

    def transport(request, *, timeout):
        path = request.full_url.removeprefix("http://gateway")
        seen.append((path, request.get_header("Authorization")))
        if path == "/v1/models":
            raise HTTPError(request.full_url, 403, "inference-sentinel", {}, None)
        name = PATHS[path]
        return BytesIO(json.dumps(sources[name]).encode())

    monkeypatch.setattr(module, "resolve_api_key", lambda: None)
    monkeypatch.setattr(module, "open_no_redirect", transport)
    evidence = module.fetch_omniroute_evidence(
        "http://gateway/",
        api_key="inference-sentinel",
        management_token="management-sentinel",
        timeout=7,
    )
    # A deny-all catalog is an honest unknown: one attempt with the inference
    # key only, no retry with the management token, no invented routes.
    assert [auth for path, auth in seen if path == "/v1/models"] == ["Bearer inference-sentinel"]
    assert len(seen) == 7
    assert "v1_models" in evidence.source_errors
    assert not evidence.inventory_route_ids
    assert not evidence.proven_by_traffic
    assert "inference-sentinel" not in repr(evidence)
    assert "management-sentinel" not in repr(evidence)


def test_fetch_is_get_only_and_retains_source_failure(sources, monkeypatch):
    from io import BytesIO
    from urllib.error import HTTPError

    from verdict import omniroute_evidence as module

    paths = PATHS
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


@pytest.mark.parametrize(
    "case",
    [
        "null",
        "unreported",
        "stale",
        "past_reset",
        "missing_time",
        "bad_time",
        "future_time",
        "missing_interval",
        "scoped",
        "unresolved",
    ],
)
def test_quota_unknown_never_fabricates_health_or_connection_exhaustion(sources, case):
    cache = sources["limits"]["caches"]["empty"]
    window = cache["quotas"]["weekly"]
    if case == "null":
        cache["quotas"] = None
        cache["message"] = "No quota observation"
    elif case == "unreported":
        window["fractionReported"] = False
    elif case == "stale":
        cache["fetchedAt"] = "2026-10-20T09:00:00Z"
    elif case == "past_reset":
        window["resetAt"] = "2026-10-20T12:59:00Z"
    elif case == "missing_time":
        del cache["fetchedAt"]
    elif case == "bad_time":
        cache["fetchedAt"] = "not-a-date"
    elif case == "future_time":
        cache["fetchedAt"] = "2026-10-21T12:00:00Z"
    elif case == "missing_interval":
        del sources["limits"]["intervalMinutes"]
    elif case == "scoped":
        window["claudeQuota"].update(kind="weekly_scoped", modelId="unrelated-model")
    else:
        del window["claudeQuota"]
    evidence = parse_omniroute_evidence(**sources, now=NOW)
    connection = next(c for c in evidence.connections if c.connection_id == "empty")
    assert connection.usable
    assert connection.quota_state == "unknown"
    assert connection.reason is None
    assert connection.quota_reason


def test_fresh_explicit_global_exhaustion_is_distinct_from_unknown(sources):
    evidence = parse_omniroute_evidence(**sources, now=NOW)
    c = next(c for c in evidence.connections if c.connection_id == "empty")
    assert not c.usable
    assert c.quota_state == "exhausted"
    assert c.reason == "quota_exhausted"
    assert c.quota_observed_at == NOW - timedelta(hours=1)
    assert c.quota_windows == (
        QuotaWindow("weekly", "weekly_all", "exhausted", 0.0, "2026-10-23T12:00:00+00:00"),
    )


def test_retained_window_keeps_reset_and_names_unresolved_scope(sources):
    cache = sources["limits"]["caches"]["empty"]
    cache["quotas"]["weekly"]["remainingPercentage"] = 35
    cache["quotas"]["scoped"] = {
        "remainingPercentage": 80,
        "resetAt": "2026-10-24T12:00:00Z",
        "claudeQuota": {"kind": "weekly_scoped", "modelId": "some-model"},
    }
    sources["cooldowns"]["items"] = [
        {"provider": "alpha", "model": "best", "remainingMs": 60_000},
        {"model": "unscoped"},
    ]
    evidence = parse_omniroute_evidence(**sources, now=NOW)
    connection = next(c for c in evidence.connections if c.connection_id == "empty")
    assert connection.quota_state == "unknown"
    assert connection.quota_windows == (
        QuotaWindow("scoped", "weekly_scoped", "unknown", 80.0, "2026-10-24T12:00:00+00:00"),
        QuotaWindow("weekly", "weekly_all", "available", 35.0, "2026-10-23T12:00:00+00:00"),
    )
    assert evidence.model_cooldowns == (
        {"provider": "alpha", "model": "best", "remaining_ms": 60_000, "routes": ("a/best",)},
        {"provider": None, "model": "unscoped", "remaining_ms": None, "routes": ()},
    )


def test_nonfinite_cooldown_remaining_is_unknown_not_a_parse_failure(sources):
    sources["cooldowns"]["items"] = [
        {"provider": "alpha", "model": "best", "remainingMs": float("nan")}
    ]
    evidence = parse_omniroute_evidence(**sources, now=NOW)
    assert evidence.model_cooldowns == (
        {"provider": "alpha", "model": "best", "remaining_ms": None, "routes": ("a/best",)},
    )


def test_positive_scoped_window_is_not_global_headroom(sources):
    w = sources["limits"]["caches"]["empty"]["quotas"]["weekly"]
    w.update(remainingPercentage=50)
    w["claudeQuota"].update(kind="weekly_scoped", modelId="some-model")
    c = parse_omniroute_evidence(**sources, now=NOW).connections[2]
    assert c.usable
    assert c.quota_state == "unknown"


def test_fresh_reported_global_headroom(sources):
    w = sources["limits"]["caches"]["empty"]["quotas"]["weekly"]
    w.update(remainingPercentage=50)
    c = parse_omniroute_evidence(**sources, now=NOW).connections[2]
    assert c.usable
    assert c.quota_state == "available"


@pytest.mark.parametrize("interval", [None, 0, -1, "70", True, float("inf")])
def test_invalid_sync_interval_cannot_grant_quota_knowledge(sources, interval):
    sources["limits"]["intervalMinutes"] = interval
    c = parse_omniroute_evidence(**sources, now=NOW).connections[2]
    assert c.usable
    assert c.quota_state == "unknown"


@pytest.mark.parametrize("other", ["exhausted", "unreported", "past_reset", "bad_reset", "scoped"])
def test_multiple_global_constraints_exhaustion_wins_and_unknown_is_not_available(sources, other):
    from copy import deepcopy

    cache = sources["limits"]["caches"]["empty"]
    first = cache["quotas"]["weekly"]
    first["remainingPercentage"] = 50
    second = deepcopy(first)
    if other == "exhausted":
        second["remainingPercentage"] = 0
    elif other == "unreported":
        second["fractionReported"] = False
    elif other == "past_reset":
        second["resetAt"] = "2026-10-20T12:59:00Z"
    elif other == "bad_reset":
        second["resetAt"] = "invalid"
    else:
        second["claudeQuota"]["kind"] = "weekly_scoped"
    cache["quotas"]["other"] = second
    c = parse_omniroute_evidence(**sources, now=NOW).connections[2]
    assert c.quota_state == ("exhausted" if other == "exhausted" else "unknown")
    assert c.usable is (other != "exhausted")


def test_explicit_unlimited_is_known_nonblocking_with_fresh_global_scope(sources):
    window = sources["limits"]["caches"]["empty"]["quotas"]["weekly"]
    window.update(unlimited=True, fractionReported=False, resetAt=None)
    c = parse_omniroute_evidence(**sources, now=NOW).connections[2]
    assert c.quota_state == "available"
    assert c.usable


@pytest.mark.parametrize("active,severity", [(False, "critical"), (True, "warning"), (None, None)])
def test_claude_zero_without_blocking_metadata_is_not_exhaustion(sources, active, severity):
    window = sources["limits"]["caches"]["empty"]["quotas"]["weekly"]
    window["claudeQuota"].update(active=active, severity=severity)
    c = parse_omniroute_evidence(**sources, now=NOW).connections[2]
    assert c.usable
    assert c.quota_state == "unknown"


def test_exhausted_global_wins_over_unresolved_scoped_window(sources):
    sources["limits"]["caches"]["empty"]["quotas"]["opaque"] = {"remainingPercentage": 80}
    c = parse_omniroute_evidence(**sources, now=NOW).connections[2]
    assert not c.usable
    assert c.quota_state == "exhausted"


def test_freshness_boundary_is_two_configured_sync_intervals(sources):
    cache = sources["limits"]["caches"]["empty"]
    cache["fetchedAt"] = (NOW - timedelta(minutes=140)).isoformat()
    c = parse_omniroute_evidence(**sources, now=NOW).connections[2]
    assert c.quota_state == "exhausted"
    cache["fetchedAt"] = (NOW - timedelta(minutes=140, seconds=1)).isoformat()
    c = parse_omniroute_evidence(**sources, now=NOW).connections[2]
    assert c.quota_state == "unknown"
    assert c.usable


@pytest.mark.parametrize("last_status", [429, 401])
def test_inventory_and_failed_traffic_are_retained_without_granting_proof(sources, last_status):
    row = sources["health_matrix"]["providers"][0]["accounts"][0]["models"][0]
    row.update(status="error", successes=2, lastStatus=last_status)
    sources["v1_models"]["data"].append({"id": "alpha/no-traffic"})
    evidence = parse_omniroute_evidence(**sources, now=NOW)
    assert evidence.inventory_route_ids == frozenset(
        {"a/best", "a/not-entitled", "alpha/no-traffic"}
    )
    assert not evidence.proven_by_traffic
    assert evidence.traffic_by_route["a/best"].last_status == last_status
    assert evidence.traffic_by_route["a/best"].status == "error"


def test_live_candidates_group_connections_and_name_blocked_rows(sources):
    evidence = parse_omniroute_evidence(**sources, now=NOW)
    assert "candidates" not in evidence.source_errors
    assert evidence.live_candidates == {
        "a/best": LiveCandidate("a/best", "alpha", ("ok",), True),
        "cand/two": LiveCandidate(
            "cand/two", "alpha", ("ok", "ok-spare"), True, ("connection_cooldown",)
        ),
        "cand/open": LiveCandidate("cand/open", "beta", ("cool",), False, ("breaker_not_closed",)),
        "cand/missing": LiveCandidate("cand/missing", "gamma", ("empty",), False, ("unreachable",)),
    }


@pytest.mark.parametrize(
    "field,value,reason",
    [
        ("excluded", True, "excluded"),
        ("excluded", None, "excluded"),
        ("reachable", False, "unreachable"),
        ("reachable", 1, "unreachable"),
        ("breakerState", "HALF_OPEN", "breaker_not_closed"),
        ("breakerState", None, "breaker_not_closed"),
        ("connectionCooldown", True, "connection_cooldown"),
        ("connectionCooldown", "false", "connection_cooldown"),
        ("modelLocked", True, "model_locked"),
        ("modelLocked", 0, "model_locked"),
        ("freeAccessExclusion", "key_excluded", "free_access_excluded"),
        ("freeAccessExclusion", False, "free_access_excluded"),
    ],
)
def test_odd_or_blocking_candidate_field_is_never_healthy(sources, field, value, reason):
    sources["candidates"]["candidates"][0][field] = value
    candidate = parse_omniroute_evidence(**sources, now=NOW).live_candidates["a/best"]
    assert not candidate.reachable
    assert candidate.blocked_reasons == (reason,)


@pytest.mark.parametrize(
    "field",
    ["excluded", "breakerState", "connectionCooldown", "modelLocked", "freeAccessExclusion"],
)
def test_missing_candidate_field_blocks_row(sources, field):
    del sources["candidates"]["candidates"][0][field]
    candidate = parse_omniroute_evidence(**sources, now=NOW).live_candidates["a/best"]
    assert not candidate.reachable
    assert len(candidate.blocked_reasons) == 1


def test_candidate_route_falls_back_to_model_and_skips_rows_without_identity(sources):
    rows = sources["candidates"]["candidates"]
    rows[0]["modelStr"] = None
    rows.append({"provider": "alpha", "connectionId": "ok", "reachable": True})
    rows.append({"modelStr": "x/no-provider", "connectionId": "ok"})
    rows.append({"modelStr": "x/no-connection", "provider": "alpha"})
    candidates = parse_omniroute_evidence(**sources, now=NOW).live_candidates
    assert candidates["a/best"].reachable
    assert set(candidates) == {"a/best", "cand/two", "cand/open", "cand/missing"}


@pytest.mark.parametrize("payload", [None, [], {"candidates": None}, {"channel": "auto"}])
def test_malformed_candidates_payload_is_a_source_error(sources, payload):
    sources["candidates"] = payload
    evidence = parse_omniroute_evidence(**sources, now=NOW)
    assert evidence.source_errors == ("candidates",)
    assert not evidence.live_candidates
    assert evidence.usable_providers == frozenset({"alpha"})


def test_candidates_default_is_unavailable_not_empty_healthy(sources):
    del sources["candidates"]
    evidence = parse_omniroute_evidence(**sources, now=NOW)
    assert evidence.source_errors == ("candidates",)
    assert not evidence.live_candidates


def test_candidate_row_cap_fails_candidates_source_closed(sources, monkeypatch):
    from io import BytesIO

    from verdict import omniroute_evidence as module

    sources["v1_models"]["data"] = sources["v1_models"]["data"][:1]
    sources["candidates"]["candidates"] *= 2

    def transport(request, *, timeout):
        return BytesIO(
            json.dumps(sources[PATHS[request.full_url.removeprefix("http://gateway")]]).encode()
        )

    monkeypatch.setattr(module, "open_no_redirect", transport)
    evidence = module.fetch_omniroute_evidence(
        "http://gateway/", api_key="inference-key", timeout=7, raw_row_limit=9
    )
    assert evidence.source_errors == ("candidates",)
    assert not evidence.live_candidates
    assert evidence.inventory_route_ids == frozenset({"a/best"})
