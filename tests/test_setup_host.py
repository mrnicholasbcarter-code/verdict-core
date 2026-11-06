"""Host prerequisites ``verdict setup`` installs (BOD-219 part 2).

Every test builds a ``HostEnvironment`` on a temporary HOME, XDG_CONFIG_HOME and
VERDICT_HOME with a temporary unit directory, a recording ``systemctl``, a fake
ocr pin and download, and fake metadata-refresh and sync-models steps. Nothing
here downloads, calls a provider, runs systemctl or reads the real ``~/.prime``.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import stat
from pathlib import Path
from typing import Any

import pytest

from verdict import harness_prime, ocr_release, setup_host
from verdict.capability_bootstrap import (
    BootstrapMode,
    BootstrapScope,
    rollback_bootstrap_actions,
    run_bootstrap,
)

PIN = "v1.12.9"
ASSET = b"\x7fELF pinned ocr asset\n"
ASSET_SHA = hashlib.sha256(ASSET).hexdigest()
GATEWAY = "http://127.0.0.1:20128"
HOST_IDS = [
    "host.ocr",
    "host.metadata",
    "host.acceptance-timer",
    "host.identify-timers",
    "host.serve",
    "host.prime-registry",
]


class FakeHost:
    """A host with nothing installed; ``~/.local/bin`` is on its PATH."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.root = tmp_path
        self.home = tmp_path / "home"
        self.home.mkdir()
        monkeypatch.setenv("HOME", str(self.home))
        monkeypatch.setenv("XDG_CONFIG_HOME", str(self.home / ".config"))
        monkeypatch.setenv("VERDICT_HOME", str(self.home / ".verdict"))
        for name in (
            "PRIME_AGENT_CODING_AGENT_DIR",
            "PRIME_AGENT_HOME",
            "PRIME_HOME",
            "LLMGATE_AUTH_TOKEN",
        ):
            monkeypatch.delenv(name, raising=False)
        self.unit_dir = self.home / ".config" / "systemd" / "user"
        self.active: set[str] = set()
        self.systemctl_calls: list[list[str]] = []
        self.fetches: list[str] = []
        self.refreshes = 0
        self.syncs: list[str] = []
        self.asset = ASSET
        self.serve_up: bool | BaseException = True
        self.restart_heals = True
        self.env = setup_host.HostEnvironment(
            home=self.home,
            unit_dir=self.unit_dir,
            gateway=GATEWAY,
            which=self.which,
            ocr_pin=lambda: (PIN, ASSET_SHA),
            ocr_version=lambda path: None,
            unit_state=self.unit_state,
            unit_result=lambda unit: None,
            systemctl=self.systemctl,
            fetch=self.fetch,
            metadata_refresh=self.refresh,
            prime_sync=self.sync,
            serve_health=self.health,
            sleep=lambda seconds: None,
        )

    @property
    def ocr(self) -> Path:
        return self.home / ".local" / "bin" / "ocr"

    @property
    def models(self) -> Path:
        return self.home / ".prime" / "agent" / "models.json"

    def which(self, name: str) -> str | None:
        candidate = self.home / ".local" / "bin" / name
        return str(candidate) if candidate.is_file() else None

    def unit_state(self, unit: str) -> dict[str, Any]:
        state = "active" if unit in self.active else "missing"
        return {"state": state, "installed": state != "missing", "active": state == "active"}

    def systemctl(self, args: list[str]) -> None:
        self.systemctl_calls.append(list(args))
        if args[2:4] == ["enable", "--now"]:
            self.active.update(args[4:])
        if args[2:3] == ["restart"] and self.restart_heals:
            self.serve_up = True

    def health(self) -> bool:
        if isinstance(self.serve_up, BaseException):
            raise self.serve_up
        return bool(self.serve_up)

    def fetch(self, url: str) -> bytes:
        self.fetches.append(url)
        return self.asset

    def refresh(self) -> dict[str, Any]:
        self.refreshes += 1
        store = self.home / ".verdict" / "model-metadata.json"
        store.parent.mkdir(parents=True, exist_ok=True)
        store.write_text("{}\n")
        return {"ok": True, "record_count": 0}

    def sync(self, gateway: str) -> dict[str, Any]:
        self.syncs.append(gateway)
        data = json.loads(self.models.read_text())
        data["providers"]["omniroute"]["models"] = [{"id": "free/model-a", "name": "free/model-a"}]
        self.models.write_text(json.dumps(data))
        return {"ok": True, "total": 1, "added": ["free/model-a"], "removed": []}

    def snapshot(self) -> dict[str, bytes]:
        return {
            str(path.relative_to(self.root)): path.read_bytes()
            for path in sorted(self.root.rglob("*"))
            if path.is_file()
        }

    def apply(self, **kwargs: Any) -> dict[str, Any]:
        # Authorize host actions only: catalog install actions must never run here.
        options: dict[str, Any] = {"allowlist": HOST_IDS, "non_interactive": True, **kwargs}
        report = run_bootstrap(
            providers=(),
            mode=BootstrapMode.APPLY,
            state_dir=self.root / "state",
            **options,
            **setup_host.bootstrap_options(self.env),
        )
        return report.to_dict()


@pytest.fixture
def host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeHost:
    return FakeHost(tmp_path, monkeypatch)


_SEAMS = (
    "home",
    "unit_dir",
    "which",
    "ocr_version",
    "unit_state",
    "unit_result",
    "systemctl",
    "fetch",
    "metadata_refresh",
    "prime_sync",
    "serve_health",
    "sleep",
)


def installed_package_env(
    host: FakeHost, monkeypatch: pytest.MonkeyPatch
) -> setup_host.HostEnvironment:
    """``HostEnvironment.from_host()`` with only the fake host's side-effecting seams.

    The ocr pin ships inside the package, so no checkout path is consulted.
    This runs the checkout's modules in-process.
    ``tests/test_wheel_contents.py::test_installed_wheel_runs_the_ocr_doctor_row_and_setup_action``
    runs the same ocr steps from an extracted wheel.
    """
    seams = {name: getattr(host.env, name) for name in _SEAMS}
    return dataclasses.replace(setup_host.HostEnvironment.from_host(), **seams)


def _host_actions(report: dict[str, Any]) -> list[dict[str, Any]]:
    return [a for a in report["plan"]["actions"] if a["kind"] == setup_host.HOST_ACTION_KIND]


def _results(report: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {item["action_id"]: item["result"] for item in report["apply"]["actions"]}


def _stage(report: dict[str, Any], name: str) -> dict[str, Any]:
    return next(stage for stage in report["stages"] if stage["stage"] == name)


# --- Prime registry helpers ---------------------------------------------------


def test_create_omniroute_registry_names_the_key_env_var_never_a_value(host: FakeHost) -> None:
    path = harness_prime.create_omniroute_registry(gateway=GATEWAY)
    assert path == host.models
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert json.loads(path.read_text()) == {
        "providers": {
            "omniroute": {
                "baseUrl": f"{GATEWAY}/v1",
                "api": "openai-completions",
                "apiKey": "OMNIROUTE_API_KEY",
                "models": [],
            }
        }
    }
    with pytest.raises(harness_prime.HarnessPrimeError, match="already exists"):
        harness_prime.create_omniroute_registry(gateway=GATEWAY)


def test_registry_state_names_each_shape(host: FakeHost) -> None:
    assert harness_prime.omniroute_registry_state()["state"] == "missing"
    host.models.parent.mkdir(parents=True)
    host.models.write_text("{not json")
    assert harness_prime.omniroute_registry_state()["state"] == "unreadable"
    host.models.write_text('{"providers": {}}')
    assert harness_prime.omniroute_registry_state()["state"] == "no_provider"
    host.models.unlink()
    harness_prime.create_omniroute_registry(gateway=GATEWAY)
    assert harness_prime.omniroute_registry_state()["state"] == "no_models"
    host.sync(GATEWAY)
    state = harness_prime.omniroute_registry_state()
    assert (state["state"], state["models"]) == ("ok", 1)


# --- planning is mutation-free ---------------------------------------------------


def test_plan_proposes_each_missing_prerequisite_in_order_without_mutating(host: FakeHost) -> None:
    before = host.snapshot()
    actions = setup_host.plan_host_actions(host.env)
    assert [action.provider_id for action in actions] == HOST_IDS
    assert [action.action_id for action in actions] == [
        f"bootstrap:host:{pid.removeprefix('host.')}" for pid in HOST_IDS
    ]
    assert all(action.kind == setup_host.HOST_ACTION_KIND for action in actions)
    assert all(action.requires_consent and action.undo for action in actions)
    assert host.snapshot() == before
    assert (host.systemctl_calls, host.fetches, host.refreshes, host.syncs) == ([], [], 0, [])


def test_bootstrap_plan_mode_includes_host_actions_and_stays_mutation_free(host: FakeHost) -> None:
    before = host.snapshot()
    report = run_bootstrap(
        providers=(), mode=BootstrapMode.PLAN, **setup_host.bootstrap_options(host.env)
    ).to_dict()
    assert [a["provider_id"] for a in _host_actions(report)] == HOST_IDS
    assert report["mutation_free"] is True
    assert host.snapshot() == before
    scoped = run_bootstrap(
        providers=(),
        mode=BootstrapMode.PLAN,
        scope=BootstrapScope.GATEWAYS,
        **setup_host.bootstrap_options(host.env),
    ).to_dict()
    assert _host_actions(scoped) == []


# --- APPLY is consent-gated ----------------------------------------------------------


def test_apply_with_consent_installs_every_prerequisite_through_the_existing_installers(
    host: FakeHost,
) -> None:
    report = host.apply()
    results = _results(report)
    assert {aid: r["status"] for aid, r in results.items()} == {
        f"bootstrap:host:{pid.removeprefix('host.')}": "applied" for pid in HOST_IDS
    }
    assert _stage(report, "apply")["status"] == "ok"

    assert hashlib.sha256(host.ocr.read_bytes()).hexdigest() == ASSET_SHA
    assert host.refreshes == 1
    for unit in (
        "verdict-metadata-refresh.service",
        "verdict-metadata-refresh.timer",
        "verdict-acceptance.service",
        "verdict-acceptance.timer",
        "verdict-identify.service",
        "verdict-identify.timer",
        "verdict-identify-screen.service",
        "verdict-identify-screen.timer",
        "verdict-serve.service",
    ):
        assert (host.unit_dir / unit).is_file(), unit
    enabled = {
        unit
        for call in host.systemctl_calls
        if call[2:4] == ["enable", "--now"]
        for unit in call[4:]
    }
    assert {
        "verdict-metadata-refresh.timer",
        "verdict-acceptance.timer",
        "verdict-identify.timer",
        "verdict-identify-screen.timer",
        "verdict-serve.service",
    } <= enabled
    env_file = host.home / ".config" / "verdict" / "serve.env"
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600
    token = next(
        line.split("=", 1)[1]
        for line in env_file.read_text().splitlines()
        if line.startswith("LLMGATE_AUTH_TOKEN=")
    )
    assert token not in json.dumps(report)

    registry = json.loads(host.models.read_text())
    omniroute = registry["providers"]["omniroute"]
    assert omniroute["baseUrl"] == f"{GATEWAY}/v1"
    assert omniroute["api"] == "openai-completions"
    assert omniroute["apiKey"] == "OMNIROUTE_API_KEY"
    assert omniroute["models"]
    assert registry["providers"]["verdict"]["baseUrl"] == "http://127.0.0.1:8000/v1"
    assert host.syncs == [GATEWAY]

    assert setup_host.plan_host_actions(host.env) == ()


def test_existing_metadata_store_is_not_refreshed_again(host: FakeHost) -> None:
    store = host.home / ".verdict" / "model-metadata.json"
    store.parent.mkdir(parents=True)
    store.write_text("{}\n")
    assert "host.metadata" in [a.provider_id for a in setup_host.plan_host_actions(host.env)]
    report = host.apply(allowlist=["host.metadata"], non_interactive=True)
    assert _results(report)["bootstrap:host:metadata"]["status"] == "applied"
    assert host.refreshes == 0
    assert (host.unit_dir / "verdict-metadata-refresh.timer").is_file()


def test_an_existing_prime_registry_is_left_alone(host: FakeHost) -> None:
    host.models.parent.mkdir(parents=True)
    host.models.write_text('{"providers": {}}')
    before = host.models.read_bytes()
    assert "host.prime-registry" not in [
        a.provider_id for a in setup_host.plan_host_actions(host.env)
    ]
    host.apply()
    assert host.models.read_bytes() == before


def test_non_interactive_apply_without_an_allowlist_mutates_nothing(host: FakeHost) -> None:
    before = host.snapshot()
    report = host.apply(allowlist=[], consent=True)
    assert _stage(report, "consent")["status"] == "blocked"
    assert report["apply"]["mutated"] is False
    assert host.snapshot() == before
    assert (host.systemctl_calls, host.fetches) == ([], [])


def test_an_allowlist_authorizes_only_the_named_prerequisite(host: FakeHost) -> None:
    report = host.apply(allowlist=["host.ocr"], non_interactive=True)
    assert list(_results(report)) == ["bootstrap:host:ocr"]
    assert host.ocr.is_file()
    assert not host.unit_dir.exists()
    assert not host.models.exists()
    assert host.systemctl_calls == []


def test_an_ocr_digest_mismatch_never_installs(host: FakeHost) -> None:
    host.asset = b"tampered asset"
    report = host.apply(allowlist=["host.ocr"], non_interactive=True)
    result = _results(report)["bootstrap:host:ocr"]
    assert result["status"] == "failed"
    assert "nothing installed" in result["note"]
    assert not host.ocr.exists()
    assert _stage(report, "apply")["status"] == "failed"


def test_installed_package_plans_and_runs_the_ocr_install_with_the_pin(
    host: FakeHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Live defect: without ``.github/`` setup found no pin and silently planned no ocr install.

    The fake asset cannot hash to the real pin, so the action fetches the pinned
    URL and refuses the bytes: the pin was read, and nothing touched the network.
    """
    version, digest = ocr_release.ocr_pin()
    env = installed_package_env(host, monkeypatch)
    actions = setup_host.plan_host_actions(env)
    assert [action.provider_id for action in actions][:1] == ["host.ocr"]
    result = setup_host.run_host_action(actions[0], env)
    assert host.fetches == [ocr_release.OCR_ASSET_URL.format(version=version)]
    assert result["status"] == "failed"
    assert digest in result["note"] and "nothing installed" in result["note"]
    assert not host.ocr.exists()


def test_prime_enable_waits_for_serve_and_names_the_repair(host: FakeHost) -> None:
    host.serve_up = False
    report = host.apply(allowlist=["host.prime-registry"], non_interactive=True)
    result = _results(report)["bootstrap:host:prime-registry"]
    assert result["status"] == "failed"
    assert "verdict harness prime enable" in result["note"]
    registry = json.loads(host.models.read_text())
    assert registry["providers"]["omniroute"]["models"]
    assert "verdict" not in registry["providers"]


def test_drifted_prerequisites_are_planned_and_applied_again(host: FakeHost) -> None:
    host.apply(allowlist=["host.ocr"], non_interactive=True)
    host.ocr.unlink()
    assert [a.provider_id for a in setup_host.plan_host_actions(host.env)][:1] == ["host.ocr"]
    report = host.apply(allowlist=["host.ocr"], non_interactive=True)
    assert _results(report)["bootstrap:host:ocr"]["status"] == "applied"
    assert host.ocr.is_file()


def test_rollback_names_manual_undo_for_host_actions(host: FakeHost) -> None:
    host.apply()
    stage = rollback_bootstrap_actions(state_dir=host.root / "state").to_dict()
    assert stage["status"] == "partial"
    assert stage["details"]["rolled_back"] == []
    manual = stage["details"]["manual_undo_required"]
    assert [item["provider_id"] for item in manual] == HOST_IDS
    assert all(item["undo"] for item in manual)


# --- CLI wiring ------------------------------------------------------------------------


@pytest.fixture
def cli_host(host: FakeHost, monkeypatch: pytest.MonkeyPatch) -> FakeHost:
    import verdict.capability_bootstrap as bootstrap

    monkeypatch.setattr(bootstrap, "discover_providers", lambda **kwargs: ())
    monkeypatch.setattr(setup_host.HostEnvironment, "from_host", classmethod(lambda cls: host.env))
    return host


CLASSIC_PLANS = (
    ["setup", "--dry-run"],
    ["setup", "--dry-run", "--json"],
    ["setup", "--dry-run", "--non-interactive", "--json"],
    ["setup", "--plan"],
    ["setup", "--plan", "--json"],
    ["setup", "plan"],
    ["setup", "plan", "--json"],
)


@pytest.mark.parametrize("argv", CLASSIC_PLANS, ids=" ".join)
def test_classic_dry_run_and_plan_never_probe_the_host(
    host: FakeHost,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
) -> None:
    """The classic preview never builds a host environment or observes the host."""
    import sys

    import verdict.prove_at_rest_service as prove_at_rest_service
    from verdict import cli

    def refuse(*args: object, **kwargs: object) -> Any:
        raise AssertionError(f"classic setup plan probed the host: {args!r}")

    monkeypatch.setattr(setup_host.HostEnvironment, "from_host", classmethod(refuse))
    monkeypatch.setattr(setup_host, "bootstrap_options", refuse)
    monkeypatch.setattr(setup_host, "plan_host_actions", refuse)
    monkeypatch.setattr(prove_at_rest_service, "timer_state", refuse)
    monkeypatch.setattr(sys, "argv", ["verdict", *argv])
    before = host.snapshot()
    cli.main()
    out = capsys.readouterr().out
    if "--json" in argv:
        report = json.loads(out)
        assert report["kind"] == "setup_plan" and report["mutation_free"] is True
        assert all(
            a["action_id"] in {"create-config", "preserve-config"} for a in report["actions"]
        )
    else:
        assert "No changes made" in out
    assert host.snapshot() == before


def test_recommended_setup_previews_host_actions_without_mutation(
    cli_host: FakeHost, capsys: pytest.CaptureFixture[str]
) -> None:
    from verdict import cli

    before = cli_host.snapshot()
    cli.cmd_setup(recommended=True, output_json=True)
    payload = json.loads(capsys.readouterr().out)
    assert [a["provider_id"] for a in _host_actions(payload["bootstrap"])] == HOST_IDS
    assert cli_host.snapshot() == before
    assert cli_host.systemctl_calls == []


def test_cli_apply_with_an_allowlist_installs_that_prerequisite(
    cli_host: FakeHost, capsys: pytest.CaptureFixture[str]
) -> None:
    from verdict import cli

    cli.cmd_setup(
        apply=True,
        output_json=True,
        non_interactive=True,
        allowlist=["host.ocr"],
        state_dir=str(cli_host.root / "state"),
    )
    payload = json.loads(capsys.readouterr().out)
    assert _results(payload)["bootstrap:host:ocr"]["status"] == "applied"
    assert cli_host.ocr.is_file()


# --- serve health: unknown is never healthy ---------------------------------------------


def _install_serve(host: FakeHost) -> None:
    host.apply(allowlist=["host.serve"])
    assert "verdict-serve.service" in host.active and setup_host.plan_host_actions(host.env)[0]


def test_active_but_unhealthy_serve_is_planned_and_restarted(host: FakeHost) -> None:
    _install_serve(host)
    assert "host.serve" not in [a.provider_id for a in setup_host.plan_host_actions(host.env)]
    host.serve_up = False
    [action] = [a for a in setup_host.plan_host_actions(host.env) if a.provider_id == "host.serve"]
    assert "http://127.0.0.1:8000/health did not answer" in action.reason
    host.systemctl_calls.clear()
    report = host.apply(allowlist=["host.serve"])
    result = _results(report)["bootstrap:host:serve"]
    assert result["status"] == "applied" and "restarted" in result["note"]
    assert ["systemctl", "--user", "restart", "verdict-serve.service"] in host.systemctl_calls


def test_a_health_probe_exception_is_never_healthy(host: FakeHost) -> None:
    _install_serve(host)
    host.serve_up = ConnectionResetError("probe crashed")
    [action] = [a for a in setup_host.plan_host_actions(host.env) if a.provider_id == "host.serve"]
    assert "ConnectionResetError" in action.reason


def test_serve_still_unhealthy_after_restart_fails_with_its_logs(host: FakeHost) -> None:
    _install_serve(host)
    host.serve_up = False
    host.restart_heals = False
    result = _results(host.apply(allowlist=["host.serve"]))["bootstrap:host:serve"]
    assert result["status"] == "failed"
    assert "journalctl --user -u verdict-serve.service" in result["note"]


# --- host-only allowlists never authorize catalog installs ----------------------------


@pytest.mark.parametrize("non_interactive", [True, False])
def test_a_host_only_allowlist_never_authorizes_catalog_installs(
    host: FakeHost, non_interactive: bool
) -> None:
    calls: list[str] = []

    def install_runner(action: Any) -> dict[str, Any]:
        calls.append(action.action_id)
        return {"status": "installed"}

    report = run_bootstrap(
        providers=(),
        mode=BootstrapMode.APPLY,
        state_dir=host.root / "state",
        allowlist=HOST_IDS,
        non_interactive=non_interactive,
        consent=False,
        install_runner=install_runner,
        **setup_host.bootstrap_options(host.env),
    ).to_dict()
    catalog = [
        a["action_id"]
        for a in report["plan"]["actions"]
        if a["kind"] == "install_provider" and a["requires_consent"]
    ]
    assert catalog, "the empty observation set must plan catalog install actions"
    assert calls == []
    assert sorted(_results(report)) == sorted(
        f"bootstrap:host:{pid.removeprefix('host.')}" for pid in HOST_IDS
    )
    assert set(catalog) <= set(_stage(report, "consent")["details"]["blocked"])
