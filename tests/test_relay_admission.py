"""Relay alternatives can only narrow; they never broaden admission."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from typing import Any

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
