"""Relay alternatives can only narrow; they never broaden admission."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.test_proxy import FixedIntelligence, ResponsesTransport, _configure_test_app
from verdict import api
from verdict.admission import RuntimeEvidence, RuntimeObservation, admit
from verdict.models import RoutingDecision
from verdict.proxy import UpstreamProxy
from verdict.relay import build_attempts

NOW = datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc)
DEAD = "cc/dead"


def _decision(**kw: Any) -> RoutingDecision:
    base = RoutingDecision(
        model="kr/ok",
        provider="omniroute",
        tier=2,
        reason="t",
        request_id="r",
        managed_backend_status="healthy",
        quality_outcome="unknown",
    )
    return replace(base, **kw)


def _admitted() -> Any:
    rows = [{"id": DEAD, "owned_by": "cc"}, {"id": "kr/ok", "owned_by": "kr"}]
    conns = [
        {"provider": "cc", "isActive": True, "testStatus": "ok"},
        {"provider": "kr", "isActive": True, "testStatus": "ok"},
    ]
    runtime = RuntimeEvidence(
        (RuntimeObservation(f"route:{DEAD}", "exhausted", "quota_exhausted", "fixture", None),),
        ("fixture",),
    )
    return admit(rows, conns, runtime, now=NOW)


PROXY = UpstreamProxy("http://upstream.test/v1", api_key="k")


def test_alternatives_without_candidate_states_are_not_broadened() -> None:
    attempts = build_attempts(
        PROXY, _decision(alternatives=[DEAD], candidate_states=[]), protocol="openai.chat"
    )
    assert [a.model for a in attempts] == ["kr/ok"]


def test_alternative_admitted_by_decision_but_not_live_admission_is_dropped() -> None:
    decision = _decision(
        alternatives=[DEAD], candidate_states=[{"model_id": DEAD, "admitted": True}]
    )
    attempts = build_attempts(PROXY, decision, protocol="openai.chat", admitted=_admitted())
    assert [a.model for a in attempts] == ["kr/ok"]


def test_selected_model_outside_live_admission_yields_no_attempt() -> None:
    attempts = build_attempts(
        PROXY, _decision(model=DEAD), protocol="openai.chat", admitted=_admitted()
    )
    assert attempts == ()


def test_relay_fails_closed_when_admission_provider_errors(monkeypatch: Any) -> None:
    transport = ResponsesTransport()
    _configure_test_app(monkeypatch, transport)

    def broken() -> Any:
        raise RuntimeError("gateway down")

    monkeypatch.setattr(api, "relay_admission_provider", broken)
    with TestClient(api.app) as client:
        response = client.post("/v1/responses", json={"input": "preserve all fields"})
    assert response.status_code == 503
    assert transport.requests == []


def test_relay_rejects_dead_selected_route_before_upstream_call(monkeypatch: Any) -> None:
    transport = ResponsesTransport()
    _configure_test_app(monkeypatch, transport)

    class DeadIntelligence(FixedIntelligence):
        async def route(self, task, criticality="medium", context=None, *, request_id=None):  # type: ignore[no-untyped-def]
            decision = await super().route(task, criticality, context)
            return replace(decision, model=DEAD)

    monkeypatch.setattr(api, "_build_intelligence", lambda: DeadIntelligence())
    monkeypatch.setattr(api, "relay_admission_provider", _admitted)
    with TestClient(api.app) as client:
        response = client.post("/v1/responses", json={"input": "preserve all fields"})
    assert response.status_code == 503
    assert transport.requests == []


def test_wired_hook_never_fails_over_to_admitted_out_alternative(monkeypatch: Any) -> None:
    """Wired hook: a decision-admitted but live-dead alternative is never sent upstream."""
    transport = ResponsesTransport(statuses=[503, 200])
    _configure_test_app(monkeypatch, transport)

    class Alternatives(FixedIntelligence):
        async def route(self, task, criticality="medium", context=None, *, request_id=None):  # type: ignore[no-untyped-def]
            decision = await super().route(task, criticality, context)
            return replace(
                decision,
                model="kr/ok",
                alternatives=[DEAD],
                candidate_states=[{"model_id": DEAD, "admitted": True, "state": "ready"}],
            )

    monkeypatch.setattr(api, "_build_intelligence", lambda: Alternatives())
    monkeypatch.setattr(api, "relay_admission_provider", _admitted)
    with TestClient(api.app) as client:
        response = client.post(
            "/v1/responses",
            json={"input": "preserve all fields"},
            headers={"idempotency-key": "idem-dead"},
        )
    assert response.status_code == 503
    assert [item["body"]["model"] for item in transport.requests] == ["kr/ok"]


def test_cached_relay_admission_never_performs_request_time_refresh() -> None:
    from verdict.api import CachedRelayAdmission

    calls = 0

    def refresh() -> Any:
        nonlocal calls
        calls += 1
        return _admitted()

    cache = CachedRelayAdmission(refresh, ttl_seconds=10)
    stamp = NOW
    cache.refresh(now=stamp)
    assert cache.get(now=stamp + timedelta(seconds=9)) is cache.get(
        now=stamp + timedelta(seconds=9)
    )
    assert calls == 1
    with pytest.raises(RuntimeError, match="stale"):
        cache.get(now=stamp + timedelta(seconds=11))
    assert calls == 1


def test_cached_relay_admission_refresh_failure_does_not_reuse_expired_authority() -> None:
    from verdict.api import CachedRelayAdmission

    cache = CachedRelayAdmission(_admitted, ttl_seconds=1)
    cache.refresh(now=NOW)
    cache._refresh = lambda: (_ for _ in ()).throw(TimeoutError())  # type: ignore[method-assign]
    with pytest.raises(TimeoutError):
        cache.refresh(now=NOW + timedelta(seconds=2))
    assert cache.last_refresh_failure == "TimeoutError"
    with pytest.raises(RuntimeError, match="stale"):
        cache.get(now=NOW + timedelta(seconds=2))
