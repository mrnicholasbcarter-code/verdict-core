"""Gateway lifecycle is visible where the CLI and the serve path already report.

Offline: every probe is a fake, no gateway is started, and the shared operator
gateway on 127.0.0.1:20128 is never contacted. HOME points at tmp_path, so no
test reads or writes the real ~/.verdict.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import verdict.api as api
from verdict import cli, gateway_lifecycle

_GATEWAY_CONFIG = (
    "primary_model: anthropic/claude-opus-5\n"
    "providers:\n"
    "  omniroute:\n"
    "    base_url: http://127.0.0.1:29999/v1\n"
    "    api_key_env: OMNIROUTE_API_KEY\n"
)

HEALTHY = gateway_lifecycle.GatewayHealth(reachable=True, responded=True, detail="inventory ok")
CLOSED = gateway_lifecycle.GatewayHealth(reachable=False, responded=False, detail="refused")


@pytest.fixture(autouse=True)
def _no_live_gateway(no_gateway_network: None) -> None:
    """Every test in this module is offline, enforced rather than asserted.

    The conftest guard turns any real httpx call or socket connect into a
    ``BaseException``, so a future change that reaches the operator's gateway on
    127.0.0.1:20128 fails loudly instead of passing differently depending on
    whether that gateway happens to be up.
    """


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Never read the operator's config, credentials or state directory."""
    home = tmp_path / "home"
    (home / ".config" / "verdict").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("VERDICT_HOME", str(home / ".verdict"))
    for name in (
        "OMNIROUTE_BASE_URL",
        "OMNIROUTE_API_KEY",
        "VERDICT_GATEWAY_START_COMMAND",
        "VERDICT_GATEWAY_READY_TIMEOUT_S",
        "VERDICT_ENSURE_GATEWAY",
        "VERDICT_SERVE_ENSURE_GATEWAY",
    ):
        monkeypatch.delenv(name, raising=False)


def _write_config(body: str = _GATEWAY_CONFIG) -> Path:
    path = Path.home() / ".config" / "verdict" / "verdict.yaml"
    path.write_text(body, encoding="utf-8")
    return path


def _fake_probe(answer: gateway_lifecycle.GatewayHealth) -> Any:
    calls: list[str] = []

    def probe(url: str, **kwargs: Any) -> gateway_lifecycle.GatewayHealth:
        calls.append(url)
        return answer

    probe.calls = calls  # type: ignore[attr-defined]
    return probe


def _forbidden_probe() -> Any:
    """A probe that fails the test if it is called at all."""

    def probe(url: str, **kwargs: Any) -> gateway_lifecycle.GatewayHealth:
        raise AssertionError(f"no gateway probe may be issued here, saw {url}")

    return probe


# --- verdict doctor ---------------------------------------------------------


def test_doctor_reports_a_healthy_gateway_as_already_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    _write_config()
    monkeypatch.setattr(gateway_lifecycle, "http_gateway_probe", _fake_probe(HEALTHY))
    diag = cli.DoctorDiagnostics()

    cli._doctor_gateway_lifecycle(diag)

    assert diag.gateway_lifecycle["state"] == "already_ready"
    assert diag.gateway_lifecycle["ready"] is True
    assert (
        "Gateway lifecycle",
        "ok",
        "gateway already_ready url=http://127.0.0.1:29999/v1: inventory ok",
    ) in diag.sections


def test_doctor_reports_an_absent_gateway_without_starting_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_config()
    monkeypatch.setattr(gateway_lifecycle, "http_gateway_probe", _fake_probe(CLOSED))

    launches: list[Any] = []

    class ForbiddenLauncher:
        def launch(self, argv: tuple[str, ...], *, cwd: Path) -> Any:
            launches.append(argv)
            raise AssertionError("doctor must never start a gateway")

    monkeypatch.setattr(gateway_lifecycle, "SubprocessGatewayLauncher", ForbiddenLauncher)
    diag = cli.DoctorDiagnostics()

    cli._doctor_gateway_lifecycle(diag)

    assert diag.gateway_lifecycle["state"] == "unhealthy"
    assert diag.gateway_lifecycle["ready"] is False
    assert launches == []
    labels = [label for label, _state, _detail in diag.sections]
    assert "Gateway lifecycle" in labels
    assert "Gateway remediation" in labels


def test_doctor_reports_not_required_when_no_gateway_is_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_config(
        "primary_model: local/model\nproviders:\n  public_ollama:\n    base_url: http://localhost:11434/v1\n"
    )
    probe = _fake_probe(CLOSED)
    monkeypatch.setattr(gateway_lifecycle, "http_gateway_probe", probe)
    diag = cli.DoctorDiagnostics()

    cli._doctor_gateway_lifecycle(diag)

    assert diag.gateway_lifecycle["state"] == "not_required"
    assert probe.calls == []


def test_doctor_gateway_lifecycle_never_raises_on_bad_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_config(
        "gateway_ready_timeout_s: -5\nproviders:\n  omniroute:\n    base_url: http://127.0.0.1:29999/v1\n"
    )
    monkeypatch.setattr(gateway_lifecycle, "http_gateway_probe", _fake_probe(HEALTHY))
    diag = cli.DoctorDiagnostics()

    cli._doctor_gateway_lifecycle(diag)

    assert diag.gateway_lifecycle["state"] == "unknown"
    assert diag.gateway_lifecycle["reason"] == "bootstrap_configuration_invalid"


# --- verdict serve ----------------------------------------------------------


def test_serve_startup_report_names_the_gateway_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """An injected probe names the state; the default path performs no gateway I/O.

    Rewritten: the original version monkeypatched ``gateway_lifecycle.http_gateway_probe``
    and asserted the state on the default path. That hid the fact that the default
    path did a real ``GET`` at all, so the same test passed against the operator's
    live gateway when the patch seam was missed. Readiness is now injected at the
    ``server_bootstrap_diagnostics`` seam, and the default path is asserted to be
    silent. This keeps every original assertion (gateway_required, state, ready,
    ensure_requested) and adds the stricter one: with the opt-in off, the probe is
    never called.
    """
    _write_config()
    probe = _fake_probe(HEALTHY)

    report = api.server_bootstrap_diagnostics(gateway_probe=probe)

    assert report["gateway_required"] is True
    lifecycle = report["gateway_lifecycle"]
    assert lifecycle["state"] == "already_ready"
    assert lifecycle["ready"] is True
    assert lifecycle["ensure_requested"] is False
    assert lifecycle["probed"] is True
    assert probe.calls == ["http://127.0.0.1:29999/v1"]

    default = api.server_bootstrap_diagnostics()

    assert default["gateway_required"] is True
    assert default["gateway_lifecycle"]["state"] == api.GATEWAY_NOT_PROBED
    assert default["gateway_lifecycle"]["probed"] is False
    assert default["gateway_lifecycle"]["ready"] is False
    assert probe.calls == ["http://127.0.0.1:29999/v1"], (
        "startup must not probe the gateway when nothing asked it to"
    )


def test_serve_startup_does_not_start_a_gateway_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Startup neither starts nor contacts a gateway unless asked.

    Rewritten alongside the test above, for the same reason and with the same
    assertions kept: state, ``launched`` and ``ensure_requested`` are still
    asserted on the report-only path, now through the injected probe. The new
    assertion is that the default path calls no probe at all, which is what makes
    this test offline regardless of whether a gateway is listening.
    """
    _write_config(_GATEWAY_CONFIG + "gateway_start_command: [fake-gateway, serve]\n")

    class ForbiddenLauncher:
        def launch(self, argv: tuple[str, ...], *, cwd: Path) -> Any:
            raise AssertionError("serve startup must not start a gateway unless asked")

    monkeypatch.setattr(gateway_lifecycle, "SubprocessGatewayLauncher", ForbiddenLauncher)
    forbidden = _forbidden_probe()
    monkeypatch.setattr(gateway_lifecycle, "http_gateway_probe", forbidden)

    default = api.server_bootstrap_diagnostics()["gateway_lifecycle"]

    assert default["state"] == api.GATEWAY_NOT_PROBED
    assert default["probed"] is False
    assert default["ensure_requested"] is False

    probe = _fake_probe(CLOSED)
    lifecycle = api.server_bootstrap_diagnostics(gateway_probe=probe)["gateway_lifecycle"]

    assert lifecycle["state"] == "unhealthy"
    assert lifecycle["launched"] is False
    assert lifecycle["ensure_requested"] is False
    assert probe.calls == ["http://127.0.0.1:29999/v1"]


def test_serve_startup_can_opt_into_ensuring_the_gateway(monkeypatch: pytest.MonkeyPatch) -> None:
    _write_config(_GATEWAY_CONFIG + "gateway_start_command: [fake-gateway, serve]\n")
    monkeypatch.setenv("VERDICT_SERVE_ENSURE_GATEWAY", "true")
    answers = [CLOSED, CLOSED, HEALTHY]
    seen: list[str] = []

    def counting(url: str, **kwargs: Any) -> gateway_lifecycle.GatewayHealth:
        answer = answers[min(len(seen), len(answers) - 1)]
        seen.append(url)
        return answer

    launches: list[tuple[str, ...]] = []

    class RecordingLauncher:
        def launch(self, argv: tuple[str, ...], *, cwd: Path) -> Any:
            launches.append(argv)

            class _Process:
                pid = 1234

                def poll(self) -> int | None:
                    return None

                def terminate(self) -> None:
                    return None

                def kill(self) -> None:
                    return None

            return _Process()

    monkeypatch.setattr(gateway_lifecycle, "http_gateway_probe", counting)
    monkeypatch.setattr(gateway_lifecycle, "SubprocessGatewayLauncher", RecordingLauncher)

    lifecycle = api.server_bootstrap_diagnostics()["gateway_lifecycle"]

    assert lifecycle["ensure_requested"] is True
    assert lifecycle["state"] == "started"
    assert launches == [("fake-gateway", "serve")]


def test_serve_startup_report_stays_non_fatal_for_a_missing_gateway(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Startup records the state; it must not refuse to boot."""
    _write_config()
    monkeypatch.setattr(gateway_lifecycle, "http_gateway_probe", _fake_probe(CLOSED))

    report = api.server_bootstrap_diagnostics()

    assert report["status"] == "ok"
    assert report["gateway_lifecycle"]["ready"] is False


# --- CLI execution path -----------------------------------------------------


def test_cli_ensure_is_off_unless_the_operator_opts_in(monkeypatch: pytest.MonkeyPatch) -> None:
    _write_config()
    probe = _fake_probe(CLOSED)
    monkeypatch.setattr(gateway_lifecycle, "http_gateway_probe", probe)

    cli._ensure_cli_gateway_ready()

    assert probe.calls == [], "an ordinary route must not probe or start a gateway"


def test_cli_ensure_reuses_a_healthy_gateway(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_config()
    monkeypatch.setenv("VERDICT_ENSURE_GATEWAY", "1")
    probe = _fake_probe(HEALTHY)
    monkeypatch.setattr(gateway_lifecycle, "http_gateway_probe", probe)

    cli._ensure_cli_gateway_ready()

    assert probe.calls == ["http://127.0.0.1:29999/v1"]
    assert "already_ready" in capsys.readouterr().err


def test_cli_ensure_fails_closed_instead_of_switching_paths(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_config()
    monkeypatch.setenv("VERDICT_ENSURE_GATEWAY", "1")
    monkeypatch.setattr(gateway_lifecycle, "http_gateway_probe", _fake_probe(CLOSED))

    with pytest.raises(SystemExit) as excinfo:
        cli._ensure_cli_gateway_ready()

    assert excinfo.value.code == 1
    err = capsys.readouterr().err
    assert "gateway_lifecycle_failed" in err
    assert "gateway_health" in err


def test_cli_ensure_is_silent_when_no_gateway_is_required(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_config(
        "primary_model: local/model\nproviders:\n  public_ollama:\n    base_url: http://localhost:11434/v1\n"
    )
    monkeypatch.setenv("VERDICT_ENSURE_GATEWAY", "1")
    probe = _fake_probe(CLOSED)
    monkeypatch.setattr(gateway_lifecycle, "http_gateway_probe", probe)

    cli._ensure_cli_gateway_ready()

    assert probe.calls == []
    assert capsys.readouterr().err == ""
