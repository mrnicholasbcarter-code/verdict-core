"""Local redirects preserve caller failure paths without forwarding Bearer keys."""

from __future__ import annotations

import urllib.error
from pathlib import Path

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


@pytest.mark.parametrize("caller", ["probe", "worker", "rest", "payload"])
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


@pytest.mark.parametrize("caller", ["patch", "decomposer"])
def test_wrapper_defaults_refuse_redirect(caller: str, tmp_path: Path) -> None:
    from verdict.decomposer import Decomposer, DecompositionConfig, DecompositionError
    from verdict.patch_executor import PatchExecutor, PatchExecutorConfig
    from verdict.work_unit import WorkUnit

    (tmp_path / ".git").mkdir()
    (tmp_path / "unit.py").write_text("pass\n", encoding="utf-8")
    key = "secret-wrapper-key"
    source_seen: list[str | None] = []
    target_seen: list[str | None] = []
    with server(200, target_seen) as target, server(302, source_seen, location=target) as source:
        if caller == "patch":
            executor = PatchExecutor(
                tmp_path, PatchExecutorConfig(model="fixture/model", base_url=source, api_key=key)
            )
            unit = WorkUnit("unit", "fixture task", ("unit.py",), ("true",))
            attempt = executor.execute_unit(unit)
            assert attempt.outcome == "error"
            message = attempt.reason
        else:
            decomposer = Decomposer(DecompositionConfig(base_url=source, api_key=key))
            with pytest.raises(DecompositionError) as raised:
                decomposer.decompose("fixture task", repo_root=tmp_path)
            message = str(raised.value)
    assert source_seen == [f"Bearer {key}"]
    assert target_seen == []
    assert "redirect refused" in message
    assert key not in message and source not in message and target not in message
