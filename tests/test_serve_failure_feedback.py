"""Serve relay real-request failure feedback into the shared health cache.

BOD-345 AC6: general upstream 5xx responses and transport timeouts that
reach the production relay path must write a correctly scoped negative into
the shared health cache (route scope, never a provider-wide block), while
caller, input, auth and cancellation failures stay ledger-only and the
existing 401/403/model-404 dispatch revocation behaviour is unchanged.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

import verdict.api as api
from tests import test_live_candidate_shortlist as live
from tests.test_proxy import FixedIntelligence
from verdict.models import RoutingDecision
from verdict.orchestration.health_cache import (
    STATE_NEGATIVE,
    TRANSIENT_BASE_SECONDS,
    HealthCache,
    HealthCacheError,
    HealthCacheLockTimeoutError,
    ProbeResult,
    default_cache_path,
)
from verdict.proxy import UpstreamProxy
from verdict.relay import relay_failure_feedback


def _prepare(
    monkeypatch: pytest.MonkeyPatch, transport: Any, *, alternatives: bool = True
) -> list[RoutingDecision]:
    """Wire the real relay path to a stub upstream transport, permit-test style."""

    current = datetime.now(timezone.utc)
    monkeypatch.setattr(live, "NOW", current)
    service = live._service([], top_k=2)
    service.admit_now = current
    service.passports = {
        rid: live._passport(rid, latency_ms=1.0) for rid in (live.STRONG, live.NEW)
    }
    prepared: list[RoutingDecision] = []

    class PreparedIntelligence(FixedIntelligence):
        log_path = ""
        profile = "production"

        async def route(
            self,
            task: str,
            criticality: str = "medium",
            context: dict[str, Any] | None = None,
            *,
            request_id: str | None = None,
        ) -> RoutingDecision:
            assert context is not None
            ctx = {**context, **live._context(live._request(live.STRONG, live.NEW))}
            ctx["_dispatch_gateway_origin"] = "http://upstream.test/v1"
            decision = await service.route(
                "fix code", criticality="low", context=ctx, request_id=request_id
            )
            if not alternatives:
                decision = replace(decision, alternatives=[])
            prepared.append(decision)
            return decision

    monkeypatch.setattr(api, "_build_intelligence", PreparedIntelligence)
    monkeypatch.setattr(
        api,
        "_build_proxy",
        lambda: UpstreamProxy("http://upstream.test/v1", transport=httpx.MockTransport(transport)),
    )
    monkeypatch.setenv("LLMGATE_ALLOW_ANONYMOUS", "true")
    return prepared


def _request_body() -> dict[str, Any]:
    return {
        "messages": [{"role": "user", "content": "fix code"}],
        "tools_required": True,
        "task_family": "coding",
        "spend_policy": "free_only",
    }


def _attempted_route(prepared: list[RoutingDecision]) -> str:
    """Route id of the primary attempt actually sent upstream."""

    decision = prepared[0]
    proof = next(p for p in decision.admission_proofs.values() if p.model == decision.model)
    return proof.route_id


def _other_route(prepared: list[RoutingDecision]) -> str:
    """Route id of the admitted alternative that was never attempted."""

    decision = prepared[0]
    return next(p.route_id for p in decision.admission_proofs.values() if p.model != decision.model)


def test_upstream_5xx_records_route_scoped_negative(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    async def transport(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": {"message": "server lost reply"}})

    prepared = _prepare(monkeypatch, transport, alternatives=False)
    started = datetime.now(timezone.utc)
    with TestClient(api.app) as client:
        response = client.post(
            "/v1/chat/completions",
            json=_request_body(),
            headers={"idempotency-key": "feedback-5xx"},
        )
    assert response.status_code == 503
    route = _attempted_route(prepared)
    other = _other_route(prepared)
    cache = HealthCache(default_cache_path())
    entry = cache.entry(route)
    assert entry is not None, "a relay 5xx must reach the shared health cache"
    assert not entry.healthy and not entry.chat_ok and not entry.tool_ok
    assert entry.category == "upstream"
    assert entry.http_status == 503
    assert entry.failure_scope == "route"
    assert entry.feedback_source == "serve_request"
    assert entry.feedback_category == "transport_upstream"
    assert entry.consecutive_failures == 1
    # Existing transient backoff category: first failure is the 60 s base.
    assert (entry.until - entry.checked_at).total_seconds() == TRANSIENT_BASE_SECONDS
    # The negative carries the actual event time of the failed request.
    assert started <= entry.checked_at <= datetime.now(timezone.utc)
    assert cache.lookup(route, entry.checked_at + timedelta(seconds=1)).state == STATE_NEGATIVE
    # One model/request error never disables siblings, pools or the provider.
    assert cache.lookup(other, started).entry is None
    assert cache.cooldowns() == {}


def test_upstream_timeout_records_route_scoped_negative(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    async def transport(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("reply lost", request=request)

    prepared = _prepare(monkeypatch, transport, alternatives=False)
    with TestClient(api.app) as client:
        response = client.post(
            "/v1/chat/completions",
            json=_request_body(),
            headers={"idempotency-key": "feedback-timeout"},
        )
    assert response.status_code == 502
    route = _attempted_route(prepared)
    cache = HealthCache(default_cache_path())
    entry = cache.entry(route)
    assert entry is not None, "a relay transport timeout must reach the shared cache"
    assert entry.category == "timeout"
    assert entry.http_status is None
    assert entry.failure_scope == "route"
    assert entry.feedback_category == "transport_timeout"
    assert entry.feedback_source == "serve_request"
    assert (entry.until - entry.checked_at).total_seconds() == TRANSIENT_BASE_SECONDS
    assert cache.cooldowns() == {}
    assert cache.lookup(_other_route(prepared), datetime.now(timezone.utc)).entry is None


def test_newer_failure_supersedes_older_pass(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    passed_at = datetime.now(timezone.utc) - timedelta(seconds=30)
    seeded = HealthCache(default_cache_path())
    for route in (live.STRONG, live.NEW):
        seeded.record(route, ProbeResult("ok", True, True), passed_at)
    seeded.save()

    async def transport(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": {"message": "boom"}})

    prepared = _prepare(monkeypatch, transport, alternatives=False)
    with TestClient(api.app) as client:
        response = client.post(
            "/v1/chat/completions",
            json=_request_body(),
            headers={"idempotency-key": "feedback-supersede"},
        )
    assert response.status_code == 500
    route = _attempted_route(prepared)
    other = _other_route(prepared)
    cache = HealthCache(default_cache_path())
    entry = cache.entry(route)
    assert entry is not None
    assert not entry.healthy
    assert entry.checked_at > passed_at
    # The newer failure wins immediately; the older pass is only history.
    assert entry.last_success_at == passed_at
    # The untouched sibling candidate keeps its healthy pass.
    sibling = cache.entry(other)
    assert sibling is not None and sibling.healthy
    assert sibling.checked_at == passed_at


def test_auth_revocation_unchanged_and_no_route_negative(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    async def transport(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"error": {"message": "forbidden"}})

    prepared = _prepare(monkeypatch, transport, alternatives=False)
    with TestClient(api.app) as client:
        response = client.post(
            "/v1/chat/completions",
            json=_request_body(),
            headers={"idempotency-key": "feedback-auth"},
        )
    assert response.status_code == 403
    route = _attempted_route(prepared)
    proof = next(p for p in prepared[0].admission_proofs.values() if p.route_id == route)
    cache = HealthCache(default_cache_path())
    # Dispatch revocation still fires (existing behaviour, unchanged).
    assert cache.revocation_epoch(proof.scope) == proof.epoch + 1
    # Auth stays revocation-scoped; it must not add a route health negative.
    assert cache.entry(route) is None


def test_caller_input_errors_write_no_negative(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    async def transport(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": {"message": "bad request shape"}})

    _prepare(monkeypatch, transport, alternatives=False)
    with TestClient(api.app) as client:
        malformed = client.post(
            "/v1/chat/completions",
            content=b"{not json",
            headers={"content-type": "application/json"},
        )
        rejected = client.post(
            "/v1/chat/completions",
            json=_request_body(),
            headers={"idempotency-key": "feedback-400"},
        )
    assert malformed.status_code == 400
    assert rejected.status_code == 400
    cache = HealthCache(default_cache_path())
    assert cache.routes() == {}
    assert cache.cooldowns() == {}


def test_cancelled_client_stream_writes_no_negative(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    async def transport(request: httpx.Request) -> httpx.Response:
        raise asyncio.CancelledError()

    _prepare(monkeypatch, transport, alternatives=False)
    body = json.dumps(_request_body()).encode()
    scope: dict[str, Any] = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "method": "POST",
        "path": "/v1/chat/completions",
        "raw_path": b"/v1/chat/completions",
        "query_string": b"",
        "root_path": "",
        "headers": [
            (b"content-type", b"application/json"),
            (b"idempotency-key", b"feedback-cancel"),
        ],
        "client": ("127.0.0.1", 50000),
        "server": ("testserver", 80),
        "scheme": "http",
    }
    sent = False

    async def receive() -> dict[str, Any]:
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.disconnect"}

    with TestClient(api.app):
        request = Request(scope, receive)
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(api._relay_completion(request, surface="chat"))
    cache = HealthCache(default_cache_path())
    assert cache.routes() == {}


def test_cache_write_failure_is_reported_not_silent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    async def transport(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": {"message": "server lost reply"}})

    prepared = _prepare(monkeypatch, transport, alternatives=False)
    real = HealthCache.merge_and_save
    calls = {"count": 0}

    def flaky(self: HealthCache, mutate: Any, **kwargs: Any) -> None:
        calls["count"] += 1
        # First serialized write is the dispatch permit commit; the second is
        # the failure-feedback write, which must fail loudly here.
        if calls["count"] > 1:
            raise HealthCacheLockTimeoutError("lock busy")
        real(self, mutate, **kwargs)

    monkeypatch.setattr(HealthCache, "merge_and_save", flaky)
    with TestClient(api.app) as client:
        response = client.post(
            "/v1/chat/completions",
            json=_request_body(),
            headers={"idempotency-key": "feedback-persistence"},
        )
        route = _attempted_route(prepared)
        assert response.status_code == 503
        assert calls["count"] >= 2
        stored = api.evidence_store_instance.find(
            evidence_id=response.headers["x-verdict-evidence-id"], scope="anonymous"
        )
    assert stored is not None
    details = (stored.events[-1]).details or {}
    feedback = details.get("health_cache_feedback")
    assert feedback == [
        {
            "route_id": route,
            "recorded": False,
            "reason": "persistence_error:HealthCacheLockTimeoutError",
        }
    ]
    assert HealthCache(default_cache_path()).entry(route) is None


# ---------------------------------------------------------------------------
# Unit coverage for the reused classification/writer helper (relay.py).
# ---------------------------------------------------------------------------

NOW = datetime(2026, 10, 21, tzinfo=timezone.utc)


def test_helper_writes_route_negative_and_leaves_sibling_untouched(tmp_path: Path) -> None:
    cache = HealthCache(tmp_path / "health.json")
    sibling = "cc/model-b"
    passed_at = NOW - timedelta(seconds=10)
    cache.record(sibling, ProbeResult("ok", True, True), passed_at)
    cache.save()

    result = relay_failure_feedback(
        cache, route_id="cc/model-a", status_code=502, now=NOW, provenance="req-1:att-1"
    )
    assert result.recorded and result.reason == "recorded"
    entry = result.entry
    assert entry is not None
    assert entry.route_id == "cc/model-a"
    assert entry.category == "upstream"
    assert entry.http_status == 502
    assert entry.failure_scope == "route"
    assert entry.feedback_source == "serve_request"
    assert entry.feedback_category == "transport_upstream"
    assert entry.feedback_provenance == ("sha256:" + hashlib.sha256(b"req-1:att-1").hexdigest())
    assert entry.state_at(NOW) == STATE_NEGATIVE
    loaded = HealthCache(cache.path)
    sibling_entry = loaded.entry(sibling)
    assert sibling_entry is not None and sibling_entry.healthy
    assert loaded.cooldowns() == {}


@pytest.mark.parametrize("status", [400, 404, 422, 429])
def test_helper_request_level_statuses_do_not_write(tmp_path: Path, status: int) -> None:
    cache = HealthCache(tmp_path / "health.json")
    result = relay_failure_feedback(cache, route_id="cc/model-a", status_code=status, now=NOW)
    assert not result.recorded and result.reason == "non_availability_failure"
    assert not cache.path.exists()


def test_helper_context_length_overflow_is_request_scoped(tmp_path: Path) -> None:
    cache = HealthCache(tmp_path / "health.json")
    result = relay_failure_feedback(
        cache,
        route_id="cc/model-a",
        status_code=400,
        body="input is too long: maximum context length exceeded",
        now=NOW,
    )
    assert not result.recorded and result.reason == "non_availability_failure"
    assert not cache.path.exists()


@pytest.mark.parametrize(
    "error",
    [
        asyncio.CancelledError(),
        ValueError("local serialization bug"),
        RuntimeError("dispatch permit revoked after commit; possibly sent"),
    ],
)
def test_helper_non_transport_errors_do_not_write(tmp_path: Path, error: BaseException) -> None:
    cache = HealthCache(tmp_path / "health.json")
    result = relay_failure_feedback(cache, route_id="cc/model-a", error=error, now=NOW)
    assert not result.recorded and result.reason == "non_availability_failure"
    assert not cache.path.exists()


def test_helper_maps_transport_exception_classes(tmp_path: Path) -> None:
    cache = HealthCache(tmp_path / "health.json")
    timed_out = relay_failure_feedback(
        cache, route_id="cc/model-a", error=httpx.ReadTimeout("reply lost"), now=NOW
    )
    assert timed_out.recorded and timed_out.entry is not None
    assert timed_out.entry.category == "timeout"
    assert timed_out.entry.http_status is None
    assert timed_out.entry.feedback_category == "transport_timeout"
    refused = relay_failure_feedback(
        cache, route_id="cc/model-c", error=httpx.ConnectError("connection refused"), now=NOW
    )
    assert refused.recorded and refused.entry is not None
    assert refused.entry.category == "upstream"
    assert refused.entry.feedback_category == "transport_connection"


def test_helper_consecutive_failures_grow_existing_backoff(tmp_path: Path) -> None:
    cache = HealthCache(tmp_path / "health.json")
    first = relay_failure_feedback(cache, route_id="cc/model-a", status_code=503, now=NOW)
    second = relay_failure_feedback(
        cache, route_id="cc/model-a", status_code=503, now=NOW + timedelta(seconds=5)
    )
    assert first.recorded and second.recorded
    assert first.entry is not None and second.entry is not None
    assert (first.entry.until - first.entry.checked_at).total_seconds() == 60.0
    assert (second.entry.until - second.entry.checked_at).total_seconds() == 120.0
    assert second.entry.consecutive_failures == 2


def test_helper_older_observation_does_not_overwrite_newer_negative(tmp_path: Path) -> None:
    cache = HealthCache(tmp_path / "health.json")
    first = relay_failure_feedback(cache, route_id="cc/model-a", status_code=503, now=NOW)
    replay = relay_failure_feedback(
        cache, route_id="cc/model-a", status_code=503, now=NOW - timedelta(seconds=60)
    )
    assert first.recorded
    assert not replay.recorded and replay.reason == "superseded_or_duplicate"
    entry = HealthCache(cache.path).entry("cc/model-a")
    assert entry is not None and entry.checked_at == NOW


def test_helper_rejects_naive_event_time_and_empty_route(tmp_path: Path) -> None:
    cache = HealthCache(tmp_path / "health.json")
    with pytest.raises(HealthCacheError):
        relay_failure_feedback(
            cache, route_id="cc/model-a", status_code=503, now=datetime(2026, 10, 21)
        )
    with pytest.raises(HealthCacheError):
        relay_failure_feedback(cache, route_id="  ", status_code=503, now=NOW)
    assert not cache.path.exists()


def test_helper_lock_and_persistence_errors_propagate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cache = HealthCache(tmp_path / "health.json")

    def boom(self: HealthCache, mutate: Any, **kwargs: Any) -> None:
        raise HealthCacheLockTimeoutError("lock busy")

    monkeypatch.setattr(HealthCache, "merge_and_save", boom)
    with pytest.raises(HealthCacheLockTimeoutError):
        relay_failure_feedback(cache, route_id="cc/model-a", status_code=503, now=NOW)
