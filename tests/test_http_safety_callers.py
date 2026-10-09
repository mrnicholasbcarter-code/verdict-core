"""Local redirects preserve caller failure paths without forwarding Bearer keys."""

from __future__ import annotations

import urllib.error

import pytest
from test_http_safety import server

from verdict.discovery import fetch_models
from verdict.doctor_diagnostics import _omniroute_api_request
from verdict.http_safety import RedirectRefused
from verdict.models import ProviderConfig
from verdict.orchestration.run import _get_json


@pytest.mark.parametrize("caller", ["orchestration", "doctor", "discovery"])
def test_get_callers_refuse_redirect(caller: str, monkeypatch: pytest.MonkeyPatch) -> None:
    key = "secret-caller-key"
    source_seen: list[str | None] = []
    target_seen: list[str | None] = []
    error: urllib.error.HTTPError | None = None
    with server(200, target_seen) as target, server(302, source_seen, location=target) as source:
        if caller == "orchestration":
            try:
                _get_json(source, api_key=key, timeout=2)
            except urllib.error.HTTPError as exc:
                error = exc
        elif caller == "doctor":
            monkeypatch.setenv("OMNIROUTE_BASE_URL", source)
            monkeypatch.setenv("OMNIROUTE_API_KEY", key)
            assert _omniroute_api_request("GET", "/catalog") is None
        else:
            config = ProviderConfig(base_url=source, api_key=key)
            assert fetch_models("redirect-fixture", config, ttl=0) == []
    assert source_seen == [f"Bearer {key}"]
    assert target_seen == []
    if caller == "orchestration":
        assert isinstance(error, RedirectRefused) and error.code == 302
        assert key not in str(error)


@pytest.mark.parametrize("caller", ["worker", "rest", "payload"])
def test_post_callers_refuse_redirect(caller: str) -> None:
    from verdict.probes import openai_probe_transport
    from verdict.prove_at_rest import live_agentic_transport, live_transport
    from verdict.subagent_selection import LaunchCandidate, openai_health_probe

    key = "secret-post-key"
    source_seen: list[str | None] = []
    target_seen: list[str | None] = []
    result: object = None
    error: urllib.error.HTTPError | None = None
    with server(200, target_seen) as target, server(302, source_seen, location=target) as source:
        if caller == "probe":
            transport = openai_probe_transport(source, api_key=key)
            try:
                transport("fixture/model", {}, 2)
            except urllib.error.HTTPError as exc:
                error = exc
        elif caller == "worker":
            candidate = LaunchCandidate(
                "fixture/model",
                "omniroute/fixture/model",
                frozenset(),
                1000,
                0,
                0,
                True,
                False,
                0,
                0,
            )
            result = openai_health_probe(source, api_key=key)(candidate)
            assert not result.healthy and result.status_code == 302
        else:
            exchange = (
                live_transport(source, api_key=key)("fixture/model", "chat", 2)
                if caller == "rest"
                else live_agentic_transport(source, api_key=key)("fixture/model", {}, 2)
            )
            result = exchange
            assert not exchange.ok and exchange.http_status == 302
    assert source_seen == [f"Bearer {key}"]
    assert target_seen == []
    assert key not in str(result) and key not in str(error)
    if caller == "probe":
        assert isinstance(error, RedirectRefused) and error.code == 302
