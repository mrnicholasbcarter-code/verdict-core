"""Offline checks for the eligibility gateway contract across TUI and CLI."""

from __future__ import annotations

import argparse
import io
import json
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from rich.console import Console

import verdict.home as home_module
from tests.test_orch_eligibility import conn, row
from verdict.actions.registry import run_action
from verdict.free_tier_admit import normalize_omniroute_origin
from verdict.home import HomeState, _run_command
from verdict.orchestration import cli, eligibility_report
from verdict.orchestration import run as orch_run
from verdict.orchestration.eligibility import HarnessVisibility
from verdict.terminal_ui import TerminalUI


def _isolated_console() -> Console:
    """A Console whose presentation inputs never depend on ambient process
    state (COLUMNS/LINES/NO_COLOR/TERM, the real stdout, or Rich's module-
    global theme stack). Every test in this module must build its own
    Console this way instead of reusing a shared instance, so a TUI render
    here cannot perturb an unrelated test's captured stdout."""
    return Console(
        file=io.StringIO(),
        width=110,
        height=40,
        force_terminal=False,
        no_color=True,
        color_system=None,
        legacy_windows=False,
        _environ={},
    )


@pytest.fixture(autouse=True)
def _isolate_tui_globals(monkeypatch: pytest.MonkeyPatch) -> None:
    """Root-cause isolation for the intermittent order-dependent leak.

    ``verdict.home._COMMAND_INDEX`` is a module-level cache populated lazily
    on first ``_run_command`` call and never reset between tests. This
    module calls ``_run_command`` directly (not through a subprocess), so it
    is the only file in the suite that can populate that cache from a
    PALETTE/LAUNCH snapshot taken mid-test-session. Force a rebuild on each
    test, and pin the presentation-relevant environment variables Rich's
    ``Console`` and ``verdict.design.presentation_mode`` read directly from
    ``os.environ`` (COLUMNS/LINES/NO_COLOR/TERM/CI), so this module's
    explicit-width Console construction can never be shadowed by whatever
    terminal env the process happened to inherit.
    """
    monkeypatch.setattr(home_module, "_COMMAND_INDEX", None)
    for name in ("COLUMNS", "LINES", "NO_COLOR", "TERM", "CI", "FORCE_COLOR"):
        monkeypatch.delenv(name, raising=False)
    yield
    monkeypatch.setattr(home_module, "_COMMAND_INDEX", None)


@pytest.mark.parametrize(
    "gateway", ["http://x:20128", "http://x:20128/", "http://x:20128/v1", "http://x:20128/v1/"]
)
def test_gateway_origin(gateway: str) -> None:
    assert normalize_omniroute_origin(gateway) == "http://x:20128"


@pytest.fixture
def offline_gateway(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Fake HTTP at the transport, and keep Prime's registry untouched."""
    requests: list[str] = []
    monkeypatch.setenv("VERDICT_HOME", str(tmp_path))
    monkeypatch.setattr(
        eligibility_report,
        "prime_visibility",
        lambda **_kwargs: HarnessVisibility(None, source="none"),
    )

    def get_json(url: str, *, api_key: str | None, timeout: float) -> Any:
        requests.append(url)
        if url.endswith("/v1/models"):
            return {"data": [row("kr/model", owned_by="kiro"), row("cc/model", owned_by="claude")]}
        if url.endswith("/api/providers"):
            return {"connections": [conn("kiro"), conn("claude")]}
        raise AssertionError(f"unexpected endpoint: {url}")

    monkeypatch.setattr(orch_run, "_get_json", get_json)
    return requests


def _args(**kwargs: Any) -> argparse.Namespace:
    values: dict[str, Any] = {
        "gateway": "http://x:20128/v1",
        "scope": "kr/",
        "prefer": "claude",
        "provider_family": ["kr"],
        "reasoning": False,
        "frontier": False,
        "probe": False,
        "json": True,
        "no_pager": True,
    }
    return argparse.Namespace(**(values | kwargs))


def test_action_uses_origin_endpoints_and_never_probes(
    offline_gateway: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden_probe(_candidate: Any) -> Any:
        pytest.fail("non-probe eligibility called the live probe")

    def probe_factory(base: str, **_kwargs: Any) -> Any:
        assert base == "http://x:20128/v1"
        return forbidden_probe

    monkeypatch.setattr("verdict.subagent_selection.openai_health_probe", probe_factory)
    result = run_action("eligibility", vars(_args()))
    assert result.ok, result.data
    assert result.data["evaluated_count"] == 1
    assert offline_gateway == ["http://x:20128/v1/models", "http://x:20128/api/providers"]


def test_action_default_uses_home_cli_gateway_env(
    offline_gateway: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("VERDICT_GATEWAY", "http://x:20128/v1/")
    monkeypatch.setattr(
        "verdict.subagent_selection.openai_health_probe",
        lambda *_a, **_k: lambda _c: pytest.fail("probe"),
    )
    result = run_action("eligibility")
    assert result.ok, result.data
    assert offline_gateway == ["http://x:20128/v1/models", "http://x:20128/api/providers"]


def test_home_passes_configured_gateway_without_changing_other_actions() -> None:
    console = _isolated_console()
    with patch("verdict.home.run_palette_action", return_value=(True, {"status": "ok"})) as spy:
        _run_command(
            "/eligibility", tui=TerminalUI(console), state=HomeState(gateway="http://x:20128")
        )
        spy.assert_called_once()
        name, params = spy.call_args.args
        assert name == "models.verified"
        assert params["gateway"] == "http://x:20128"
        assert params["query"].status is None and params["query"].provider is None
        assert params["_consumer"] is True


def test_cli_tui_payload_parity_without_probe(
    offline_gateway: list[str], capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "verdict.subagent_selection.openai_health_probe",
        lambda *_a, **_k: lambda _c: pytest.fail("probe"),
    )
    monkeypatch.setenv("VERDICT_AUTO_REFRESH", "0")
    from verdict.actions import verified_models as surfaces

    original_consumer = surfaces.consume_verified_models
    fixed_now = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)

    def fixed_consumer(**kwargs: Any) -> Any:
        return original_consumer(**kwargs, clock=lambda: fixed_now)

    monkeypatch.setattr(surfaces, "consume_verified_models", fixed_consumer)
    args = _args(scope="", provider_family=[], verified=True)
    assert cli._eligibility(args) == 0
    cli_payload = json.loads(capsys.readouterr().out)
    console = _isolated_console()
    rendered: list[tuple[bool, dict[str, Any]]] = []

    def capture(_tui: TerminalUI, ok: bool, data: dict[str, Any], *, width: int) -> None:
        rendered.append((ok, data))

    with patch("verdict.home._render_action_result", side_effect=capture):
        _run_command("/eligibility", tui=TerminalUI(console), state=HomeState(gateway=args.gateway))
    assert len(rendered) == 1
    assert rendered[0] == (True, cli_payload)
    assert cli_payload["schema"] == "verdict.verified-models/v1"
    assert cli_payload["filters"] == {"status": None, "provider": None, "search": None}
    assert offline_gateway == ["http://x:20128/v1/models", "http://x:20128/api/providers"] * 2


@pytest.mark.parametrize("endpoint", ["/v1/models", "/api/providers"])
@pytest.mark.parametrize("failure", ["404", "connection"])
def test_gateway_failure_reports_endpoint_without_secrets(
    endpoint: str,
    failure: str,
    offline_gateway: list[str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("VERDICT_OMNIROUTE_API_KEY", "super-secret-token")

    def broken_get_json(url: str, *, api_key: str | None, timeout: float) -> Any:
        if url.endswith(endpoint):
            if failure == "404":
                raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
            raise urllib.error.URLError("connection refused")
        if url.endswith("/v1/models"):
            return {"data": [row("kr/model", owned_by="kiro")]}
        raise AssertionError(url)

    monkeypatch.setattr(orch_run, "_get_json", broken_get_json)
    args = _args()
    assert cli._eligibility(args) != 0
    payload = json.loads(capsys.readouterr().out)
    assert endpoint in payload["error"]
    assert "gateway" in payload["error"]
    assert "super-secret-token" not in json.dumps(payload)
    assert "verdicts" not in payload
    args.json = False
    assert cli._eligibility(args) != 0
    human = capsys.readouterr()
    assert endpoint in human.err
    assert "super-secret-token" not in human.err
    assert not human.out
    console = _isolated_console()
    _run_command("/eligibility", tui=TerminalUI(console), state=HomeState(gateway=args.gateway))
    output = console.file.getvalue()
    assert endpoint in output
    assert "super-secret-token" not in output
    assert "error" in output.lower()
