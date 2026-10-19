"""Relay alternatives can only narrow; they never broaden admission."""

from __future__ import annotations

import json as _json
import time as _time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path as _Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.relay_proof_helpers import with_relay_proofs
from tests.test_proxy import (
    FixedIntelligence,
    RecordingTransport,
    ResponsesTransport,
    _configure_test_app,
)
from tests.test_verified_models_projection import at_rest_entry, conn, health_cache, row
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
            return with_relay_proofs(replace(decision, model=DEAD), context)

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
            return with_relay_proofs(
                replace(
                    decision,
                    model="kr/ok",
                    alternatives=[DEAD],
                    candidate_states=[{"model_id": DEAD, "admitted": True, "state": "ready"}],
                ),
                context,
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


# ---------------------------------------------------------------------------
# BOD-435 / S07: public /v1/models is a local verified projection; admission
# readiness recovers through the lifespan refresh task, never a request probe.
# ---------------------------------------------------------------------------

S07_NOW = datetime(2026, 10, 19, 12, 0, tzinfo=timezone.utc)
S07_GATEWAY = "http://127.0.0.1:20128"


def _s07_env(monkeypatch: Any, tmp_path: _Path, *, clock: datetime = S07_NOW) -> None:
    monkeypatch.setenv("VERDICT_HOME", str(tmp_path))
    monkeypatch.delenv("VERDICT_HEALTH_CACHE", raising=False)
    monkeypatch.delenv("VERDICT_CATALOG_CACHE", raising=False)
    monkeypatch.setenv("OMNIROUTE_BASE_URL", S07_GATEWAY)
    monkeypatch.setattr(api, "_public_models_clock", lambda: clock)
    monkeypatch.setattr(api, "PUBLIC_MODELS_REFRESH_SECONDS", 0.02)
    _forbid_network(monkeypatch)


def _forbid_network(monkeypatch: Any) -> None:
    import socket
    import urllib.request

    import httpx

    def forbidden(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("public catalog / readiness must make no gateway or provider call")

    monkeypatch.setattr(urllib.request, "urlopen", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(httpx.AsyncClient, "send", forbidden)


def _s07_seed(
    tmp_path: _Path,
    *,
    ids: tuple[str, ...] = ("cc/sonnet", "kr/ok"),
    proven: tuple[str, ...] = ("cc/sonnet",),
    proof_age: float = 60.0,
    inventory_age: float = 30.0,
    providers_age: float = 30.0,
    catalog: Any = None,
) -> None:
    from verdict.catalog_cache import put_cached_catalog
    from verdict.free_tier_admit import (
        _write_last_good,
        default_admit_last_good_path,
        omniroute_endpoint_from_env,
    )

    endpoint = omniroute_endpoint_from_env()
    assert endpoint is not None
    origin, key = S07_GATEWAY, endpoint[1]
    payload = catalog if catalog is not None else {"data": [row(i) for i in ids]}
    put_cached_catalog(origin, key, payload, now=S07_NOW - timedelta(seconds=inventory_age))
    _write_last_good(
        default_admit_last_good_path(),
        origin,
        {"providers": {"connections": [conn("cc"), conn("kr")]}},
        S07_NOW - timedelta(seconds=providers_age),
    )
    (tmp_path / "health-cache.json").write_text(
        _json.dumps(
            health_cache(
                *(
                    at_rest_entry(i, checked_at=S07_NOW - timedelta(seconds=proof_age))
                    for i in proven
                )
            )
        )
    )


def _s07_wait(client: TestClient, status: int, timeout: float = 3.0) -> Any:
    deadline = _time.monotonic() + timeout
    response = client.get("/v1/models")
    while response.status_code != status and _time.monotonic() < deadline:
        _time.sleep(0.02)
        response = client.get("/v1/models")
    return response


def _forbid_refresh(monkeypatch: Any) -> None:
    import verdict.actions.verified_models as surfaces

    def forbidden(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("public catalog GET must never start a qualifier")

    monkeypatch.setattr(surfaces, "refresh_for_consumer", forbidden)
    monkeypatch.setattr(surfaces, "consume_verified_models", forbidden)


def test_public_models_are_local_verified_projection_and_readiness_recovers(
    monkeypatch: Any, tmp_path: _Path
) -> None:
    transport = RecordingTransport()
    _configure_test_app(monkeypatch, transport)
    _s07_env(monkeypatch, tmp_path)
    _forbid_refresh(monkeypatch)
    monkeypatch.setenv("LLMGATE_MODEL_DENYLIST", "kr/ok2")

    with TestClient(api.app) as client:
        # Cold start: no source snapshot at all is classified unavailable.
        cold = client.get("/v1/models")
        assert cold.status_code == 503
        assert "inventory_absent" in cold.text

        # A valid refresh recovers readiness without restarting the app.
        _s07_seed(tmp_path, ids=("cc/sonnet", "kr/ok", "kr/ok2"), proven=("cc/sonnet", "kr/ok2"))
        recovered = _s07_wait(client, 200)
        assert recovered.status_code == 200, recovered.text
        body = recovered.json()
        assert body["object"] == "list"
        # Only fresh positive proof publishes; the denylist applies afterwards.
        assert [m["id"] for m in body["data"]] == ["cc/sonnet"]
        assert body["data"][0]["verdict"]["availability_state"] == "verified"

        # Corrupt authority drops readiness at the next refresh.
        from verdict.catalog_cache import default_catalog_cache_path

        default_catalog_cache_path().write_text("{not json")
        assert _s07_wait(client, 503).status_code == 503

        # And a later valid refresh recovers again.
        _s07_seed(tmp_path)
        assert _s07_wait(client, 200).status_code == 200

    assert transport.requests == []


@pytest.mark.parametrize(
    ("proof_age", "published"), [(599.999, True), (600.0, False), (601.0, False)]
)
def test_public_models_proof_age_is_strictly_below_600_seconds(
    monkeypatch: Any, tmp_path: _Path, proof_age: float, published: bool
) -> None:
    _configure_test_app(monkeypatch, RecordingTransport())
    _s07_env(monkeypatch, tmp_path)
    _s07_seed(tmp_path, proof_age=proof_age)
    with TestClient(api.app) as client:
        response = client.get("/v1/models")
    assert response.status_code == 200
    assert [m["id"] for m in response.json()["data"]] == (["cc/sonnet"] if published else [])


@pytest.mark.parametrize(
    ("inventory_age", "providers_age", "status", "code"),
    [
        (599.999, 10.0, 200, ""),
        (600.0, 10.0, 503, "inventory_expired"),
        (10.0, 600.0, 503, "inventory_expired"),
    ],
)
def test_public_models_inventory_authority_is_strictly_below_600_seconds(
    monkeypatch: Any,
    tmp_path: _Path,
    inventory_age: float,
    providers_age: float,
    status: int,
    code: str,
) -> None:
    _configure_test_app(monkeypatch, RecordingTransport())
    _s07_env(monkeypatch, tmp_path)
    _s07_seed(tmp_path, inventory_age=inventory_age, providers_age=providers_age)
    with TestClient(api.app) as client:
        response = client.get("/v1/models")
    assert response.status_code == status
    assert code in response.text


def test_public_models_valid_current_empty_inventory_is_200_empty(
    monkeypatch: Any, tmp_path: _Path
) -> None:
    _configure_test_app(monkeypatch, RecordingTransport())
    _s07_env(monkeypatch, tmp_path)
    _s07_seed(tmp_path, catalog={"data": []}, proven=())
    with TestClient(api.app) as client:
        response = client.get("/v1/models")
    assert response.status_code == 200
    assert response.json()["data"] == []


def test_public_models_incomplete_inventory_never_publishes(
    monkeypatch: Any, tmp_path: _Path
) -> None:
    _configure_test_app(monkeypatch, RecordingTransport())
    _s07_env(monkeypatch, tmp_path)
    _s07_seed(tmp_path, catalog={"data": {"cc/sonnet": {}}})
    with TestClient(api.app) as client:
        response = client.get("/v1/models")
    assert response.status_code == 503
    assert "inventory_incomplete" in response.text


def test_public_models_last_good_never_outlives_its_original_deadline(
    monkeypatch: Any, tmp_path: _Path
) -> None:
    _configure_test_app(monkeypatch, RecordingTransport())
    _s07_env(monkeypatch, tmp_path)
    _s07_seed(tmp_path, inventory_age=300.0)
    clock = {"now": S07_NOW}
    monkeypatch.setattr(api, "_public_models_clock", lambda: clock["now"])
    with TestClient(api.app) as client:
        assert client.get("/v1/models").status_code == 200
        # The source file is untouched; only time passes. A repeated refresh must
        # not renew the source time, so the same snapshot expires at 600s age.
        clock["now"] = S07_NOW + timedelta(seconds=299.0)
        assert client.get("/v1/models").status_code == 200
        clock["now"] = S07_NOW + timedelta(seconds=300.0)
        _time.sleep(0.1)
        expired = client.get("/v1/models")
    assert expired.status_code == 503
    assert "inventory_expired" in expired.text


def test_lifespan_stop_closes_the_public_models_refresh_task(
    monkeypatch: Any, tmp_path: _Path
) -> None:
    _configure_test_app(monkeypatch, RecordingTransport())
    _s07_env(monkeypatch, tmp_path)
    with TestClient(api.app):
        task = api.public_models_task
        assert task is not None and not task.done()
    assert task.done()
    assert api.public_models_task is None
    assert api.public_models_readiness is None


def test_relay_admission_cache_recovers_from_local_source_and_never_outlives_it(
    monkeypatch: Any, tmp_path: _Path
) -> None:
    transport = ResponsesTransport()
    _configure_test_app(monkeypatch, transport)
    _s07_env(monkeypatch, tmp_path)
    monkeypatch.setenv("VERDICT_RELAY_ADMISSION_CACHE", "true")
    monkeypatch.setenv("VERDICT_RELAY_ADMISSION_TTL_SECONDS", "60")
    clock = {"now": S07_NOW}
    monkeypatch.setattr(api, "_public_models_clock", lambda: clock["now"])

    import verdict.admission as admission

    def forbidden(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("relay admission must not perform a gateway GET")

    monkeypatch.setattr(admission, "load_live_admission", forbidden)

    with TestClient(api.app):
        assert api.relay_admission_provider is not None
        with pytest.raises(RuntimeError, match="cache_empty"):
            api.relay_admission_provider()
        _s07_seed(tmp_path, inventory_age=550.0, providers_age=550.0)
        deadline = _time.monotonic() + 3
        while _time.monotonic() < deadline:
            try:
                admitted = api.relay_admission_provider()
                break
            except RuntimeError:
                _time.sleep(0.02)
        else:
            raise AssertionError("relay admission never recovered")
        assert "cc/sonnet" in admitted
        # The 60s TTL cannot extend the 600s source deadline (50s remain).
        clock["now"] = S07_NOW + timedelta(seconds=51.0)
        with pytest.raises(RuntimeError, match="stale"):
            api.relay_admission_provider(now=clock["now"])  # type: ignore[call-arg]


def test_cached_relay_admission_source_deadline_caps_the_ttl() -> None:
    from verdict.api import CachedRelayAdmission

    cache = CachedRelayAdmission(_admitted, ttl_seconds=60)
    cache.refresh(now=NOW, source_expires_at=NOW + timedelta(seconds=10))
    assert cache.get(now=NOW + timedelta(seconds=9.999)) is not None
    with pytest.raises(RuntimeError, match="stale"):
        cache.get(now=NOW + timedelta(seconds=10))
