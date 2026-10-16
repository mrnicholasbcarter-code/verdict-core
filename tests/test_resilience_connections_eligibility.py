"""BOD-387: resilience connections as health evidence; passport-gated token headroom."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx

from verdict.availability import AvailabilityState, OmniRouteAvailabilityAdapter
from verdict.omniroute import OmniRouteHTTPTransport
from verdict.subagent_models import SUBAGENT_ROLE_REQUIREMENTS

NOW = datetime(2026, 10, 15, 21, 0, tzinfo=timezone.utc)
MODELS = [
    {"id": "p/ok", "owned_by": "p", "capabilities": {"tool_calling": True, "reasoning": True}},
    {"id": "p/locked", "owned_by": "p", "capabilities": {"tool_calling": True, "reasoning": True}},
    {"id": "c/cool", "owned_by": "c", "capabilities": {"tool_calling": True, "reasoning": True}},
    {"id": "o/open", "owned_by": "o", "capabilities": {"tool_calling": True, "reasoning": True}},
]


def _connection(provider: str, **overrides):
    row = {
        "provider": provider,
        "isActive": True,
        "connectionStatus": "healthy",
        "cooldownRemainingMs": 0,
        "isCoolingDown": False,
        "breaker": {"state": "CLOSED"},
        "lockouts": [],
    }
    row.update(overrides)
    return row


CONNECTIONS = [
    _connection(
        "p", lockouts=[{"model": "locked", "reason": "rate_limited", "remainingMs": 60_000}]
    ),
    _connection(
        "c",
        connectionStatus="cooling_down",
        isCoolingDown=True,
        cooldownRemainingMs=120_000,
        breaker=None,
    ),
    _connection("o", breaker={"state": "OPEN"}),
]


def _adapter(passports=None) -> OmniRouteAvailabilityAdapter:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": MODELS})
        assert request.headers["authorization"] == "Bearer management-secret"
        if request.url.path == "/api/monitoring/health":
            # Only 7 providers on the real host; none of ours.
            return httpx.Response(
                200, json={"status": "healthy", "timestamp": NOW.isoformat(), "providerHealth": {}}
            )
        if request.url.path == "/api/resilience/connections":
            return httpx.Response(200, json={"connections": CONNECTIONS, "breakers": []})
        raise AssertionError(f"unexpected path: {request.url.path}")

    transport = OmniRouteHTTPTransport(
        "https://router.example.test",
        api_key="catalog-secret",
        management_token="management-secret",
        runtime_sources={"health", "connections"},
        allow_private_hosts={"router.example.test"},
        transport=httpx.MockTransport(handler),
        clock=lambda: NOW,
    )
    return OmniRouteAvailabilityAdapter(
        transport, passports=(lambda: passports) if passports is not None else None
    )


def _passport(expires_at: datetime):
    return SimpleNamespace(expires_at=expires_at)


def _states(report):
    return {c.model.id: c for c in report.candidates}


def test_cooling_connection_is_not_eligible() -> None:
    report = _adapter({"c/cool": _passport(NOW + timedelta(minutes=5))}).evaluate(
        SUBAGENT_ROLE_REQUIREMENTS["worker"], now=NOW
    )
    cool = _states(report)["c/cool"]
    assert cool.state is AvailabilityState.LOCKED_OUT
    assert "c/cool" not in {c.model.id for c in report.eligible}


def test_locked_model_is_locked_out_and_open_breaker_is_circuit_open() -> None:
    report = _adapter().evaluate(SUBAGENT_ROLE_REQUIREMENTS["worker"], now=NOW)
    states = _states(report)
    assert states["p/locked"].state is AvailabilityState.LOCKED_OUT
    assert states["o/open"].state is AvailabilityState.CIRCUIT_OPEN


def test_healthy_connection_without_passport_stays_degraded() -> None:
    report = _adapter({}).evaluate(SUBAGENT_ROLE_REQUIREMENTS["worker"], now=NOW)
    ok = _states(report)["p/ok"]
    assert ok.state is AvailabilityState.DEGRADED
    assert ok.reasons == ("token headroom unknown",)
    assert report.eligible == ()


def test_expired_passport_does_not_waive_token_headroom() -> None:
    report = _adapter({"p/ok": _passport(NOW - timedelta(seconds=1))}).evaluate(
        SUBAGENT_ROLE_REQUIREMENTS["worker"], now=NOW
    )
    assert _states(report)["p/ok"].state is AvailabilityState.DEGRADED
    assert report.eligible == ()


def test_fresh_passport_and_healthy_connection_make_worker_and_reviewer_eligible() -> None:
    passports = {"p/ok": _passport(NOW + timedelta(minutes=5))}
    for role in ("worker", "reviewer"):
        report = _adapter(passports).evaluate(SUBAGENT_ROLE_REQUIREMENTS[role], now=NOW)
        assert [c.model.id for c in report.eligible] == ["p/ok"], role


def test_passport_waiver_is_off_for_roles_that_did_not_opt_in() -> None:
    passports = {"p/ok": _passport(NOW + timedelta(minutes=5))}
    report = _adapter(passports).evaluate(SUBAGENT_ROLE_REQUIREMENTS["oracle"], now=NOW)
    assert _states(report)["p/ok"].state is AvailabilityState.DEGRADED


def test_from_environment_accepts_gateway_cached_health_but_not_stale(tmp_path, monkeypatch):
    """BOD-387: the gateway caches health ~20 s; 50 s is current, 120 s is stale."""
    from verdict.availability import CandidateRequirements
    from verdict.subagent_models import SubagentModelSelector

    for name in ("LLMGATE_AVAILABILITY_PROFILE", "LLMGATE_UPSTREAM_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("OMNIROUTE_BASE_URL", "http://127.0.0.1:20128/v1")
    monkeypatch.setenv("VERDICT_OMNIROUTE_API_KEY", "catalog-secret")
    monkeypatch.setenv("OMNIROUTE_MANAGEMENT_TOKEN", "management-secret")
    monkeypatch.setenv("VERDICT_PROVE_AT_REST_STATE", str(tmp_path / "state.json"))
    adapter = SubagentModelSelector.from_environment().availability_cache.source.__self__

    def report_at(age_seconds: int):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/v1/models":
                return httpx.Response(200, json={"data": MODELS[:1]})
            if request.url.path == "/api/monitoring/health":
                stamp = NOW - timedelta(seconds=age_seconds)
                # The provider row comes from health, so it carries the gateway's stamp.
                return httpx.Response(
                    200,
                    json={
                        "timestamp": stamp.isoformat(),
                        "providerHealth": {"p": {"state": "closed"}},
                    },
                )
            return httpx.Response(200, json={"connections": [_connection("p")]})

        adapter.transport.transport = httpx.MockTransport(handler)
        adapter.transport.clock = lambda: NOW
        report = adapter.evaluate(CandidateRequirements(required=frozenset({"tools"})), now=NOW)
        return report.candidates[0]

    assert report_at(50).state is AvailabilityState.ELIGIBLE
    stale = report_at(120)
    assert stale.state is AvailabilityState.UNKNOWN
    assert stale.reasons == ("stale observation",)
