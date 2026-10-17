"""Census carries tool and context probes; prove-at-rest untouched (spec v6.1 step 6)."""

from __future__ import annotations

import inspect
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from verdict import census_passports, prove_at_rest
from verdict.omniroute_pipeline.probes import PROBES_FLAG_ENV, ProbeOutcome
from verdict.omniroute_pipeline.state import STATE_FLAG_ENV


@pytest.fixture(autouse=True)
def isolate_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("VERDICT_HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("VERDICT_SKIP_SETUP", "1")
    monkeypatch.delenv(PROBES_FLAG_ENV, raising=False)
    monkeypatch.delenv(STATE_FLAG_ENV, raising=False)


def test_census_runs_tool_probes_and_prove_at_rest_untouched() -> None:
    sig = inspect.signature(census_passports.run_census)
    assert "tool_probe_interval_s" in sig.parameters
    assert sig.parameters["context_probe_max_per_tick"].default == 1
    assert "agentic_max_routes" not in sig.parameters
    assert hasattr(prove_at_rest, "agentic_candidates")


def test_census_cli_accepts_pipeline_budgets() -> None:
    import argparse

    from verdict.commands.parsers_census import register

    parser = argparse.ArgumentParser()
    register(parser.add_subparsers(dest="command"))
    args = parser.parse_args(["census", "--tool-probe-interval-s", "1800"])
    assert args.tool_probe_interval_s == 1800
    assert args.context_probe_max_per_tick == 1


def _snapshot() -> Any:
    import httpx

    from verdict.free_tier_admit import load_omniroute_admit_snapshot

    def gateway(request: httpx.Request) -> httpx.Response:
        data: dict[str, object] = {
            "/v1/models": {"data": [{"id": "good/a", "owned_by": "good"}]},
            "/api/v1/me/status": {"accountQuotas": []},
            "/api/free-tier/summary": {
                "perModel": [{"provider": "good", "modelId": "a", "freeType": "keyless"}]
            },
            "/api/providers": {
                "connections": [{"provider": "good", "isActive": True, "testStatus": "active"}]
            },
            "/api/usage/model-latency-stats": {"entries": []},
            "/api/resilience/connections": {
                "connections": [
                    {
                        "provider": "good",
                        "isActive": True,
                        "connectionStatus": "healthy",
                        "cooldownRemainingMs": 0,
                        "breaker": {"state": "CLOSED"},
                        "lockouts": [],
                    }
                ]
            },
        }
        return httpx.Response(200, json=data[request.url.path])

    return load_omniroute_admit_snapshot("http://gateway", transport=httpx.MockTransport(gateway))


def _tick(tmp_path: Path, prober: Any, **kw: Any) -> dict[str, Any]:
    from verdict.orchestration.health_cache import HealthCache
    from verdict.prove_at_rest import ProbeExchange

    return census_passports.run_census(
        _snapshot(),
        lambda *a: ProbeExchange(http_status=200, ok=True, chat_exact=True, tool_called=True),
        cache=HealthCache(tmp_path / "health.json"),
        passport_store_path=tmp_path / "state.json",
        now=datetime.now(timezone.utc),
        context_prober=prober,
        context_store_path=tmp_path / "usable-context.json",
        **kw,
    )


def test_context_probe_dark_by_default(tmp_path: Path) -> None:
    def never(route: str, size: int) -> ProbeOutcome:
        raise AssertionError("no context probe while the flag is off")

    first = _tick(tmp_path, never)
    second = _tick(tmp_path, never)
    assert "context_probed" not in first and "context_probed" not in second
    assert "pipeline_states" not in second


def test_context_probe_one_per_tick_on_proven_route(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from verdict.omniroute_pipeline.context_store import load_usable_context

    monkeypatch.setenv(PROBES_FLAG_ENV, "1")
    monkeypatch.setenv(STATE_FLAG_ENV, "1")
    calls: list[tuple[str, int]] = []

    def prober(route: str, size: int) -> ProbeOutcome:
        calls.append((route, size))
        return ProbeOutcome.PASS

    first = _tick(tmp_path, prober, tool_probe_interval_s=1800)
    assert first["pipeline_states"] == {"good/a": "tool_ok"}
    second = _tick(tmp_path, prober, tool_probe_interval_s=1800)
    assert second["context_probed"] == 1
    assert calls == [("good/a", 128_000)]
    assert load_usable_context(tmp_path / "usable-context.json")["good/a"]["usable_context"] == (
        128_000
    )


def test_pipeline_budgets_validated(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        _tick(tmp_path, None, context_probe_max_per_tick=-1)
    with pytest.raises(ValueError):
        _tick(tmp_path, None, tool_probe_interval_s=0)
