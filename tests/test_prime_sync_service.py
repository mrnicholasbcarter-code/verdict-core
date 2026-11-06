"""``verdict harness prime install-service``: the prime-sync units come from the package.

The doctor repair for a missing ``verdict-prime-sync.timer`` once named
``cp <package_root>/deploy/systemd/...``. ``package_root()`` is ``site-packages`` in
an installed wheel and ``deploy/`` is not shipped, so that repair could not work
on an installed host. The units are now rendered from this interpreter and this
package, as the acceptance, identify and metadata timers are.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Any

import pytest

import verdict
from verdict import doctor_diagnostics as dd
from verdict.doctor_presentation import repair_command

ROOT = Path(__file__).resolve().parents[1]
INSTALL = "verdict harness prime install-service"


def test_missing_timer_repair_names_the_package_installer() -> None:
    repair = dd._prime_sync_timer_repair("missing")
    assert "deploy/" not in repair
    assert repair_command("Prime-sync timer failed x" + repair) == INSTALL


@pytest.fixture(scope="module")
def installed_site(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The built wheel extracted into a bare ``site-packages`` with no checkout."""
    if shutil.which("uv") is None:
        pytest.skip("uv is required to build the wheel")
    out_dir = tmp_path_factory.mktemp("wheel")
    subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(out_dir), str(ROOT)],
        check=True,
        capture_output=True,
        text=True,
    )
    (wheel,) = out_dir.glob("verdict_core-*.whl")
    site = tmp_path_factory.mktemp("installed") / "site-packages"
    with zipfile.ZipFile(wheel) as archive:
        archive.extractall(site)
    assert not (site / "deploy").exists()
    return site


def _run_installed(site: Path, home: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run the extracted wheel with a temporary HOME and a ``systemctl`` that only logs."""
    fake = home / "bin" / "systemctl"
    if not fake.exists():
        fake.parent.mkdir(parents=True)
        fake.write_text(f'#!/bin/sh\necho "$*" >> "{home}/systemctl.log"\n')
        fake.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{fake.parent}{os.pathsep}{os.environ.get('PATH', '')}",
        "PYTHONPATH": str(site),
        "HOME": str(home),
        "VERDICT_HOME": str(home / ".verdict"),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "VERDICT_SKIP_SETUP": "1",
    }
    return subprocess.run(
        [sys.executable, *args], cwd=home, env=env, capture_output=True, text=True, timeout=120
    )


def test_installed_wheel_repair_and_installer_work_without_a_checkout(
    installed_site: Path, tmp_path: Path
) -> None:
    probe = (
        "import json, verdict; from verdict.doctor_diagnostics import _prime_sync_timer_repair;"
        "print(json.dumps([verdict.__file__, _prime_sync_timer_repair('missing')]))"
    )
    result = _run_installed(installed_site, tmp_path, "-c", probe)
    assert result.returncode == 0, result.stderr
    where, repair = json.loads(result.stdout)
    assert Path(where).resolve().parent == (installed_site / "verdict").resolve()
    assert repair_command("Prime-sync timer failed x" + repair) == INSTALL

    preview = _run_installed(
        installed_site, tmp_path, "-m", "verdict", *INSTALL.split()[1:], "--dry-run"
    )
    assert preview.returncode == 0, preview.stderr
    report = json.loads(preview.stdout)
    service = report["files"]["verdict-prime-sync.service"]
    assert f"WorkingDirectory={installed_site.resolve()}\n" in service
    assert (
        f'ExecStart="{Path(sys.executable).absolute()}" -m verdict harness prime sync-models\n'
        in service
    )
    units = tmp_path / ".config" / "systemd" / "user"
    assert report["dry_run"] is True and not units.exists()
    assert not (tmp_path / "systemctl.log").exists()

    installed = _run_installed(installed_site, tmp_path, "-m", "verdict", *INSTALL.split()[1:])
    assert installed.returncode == 0, installed.stderr
    assert (units / "verdict-prime-sync.service").read_text() == service
    assert (tmp_path / "systemctl.log").read_text().splitlines() == [
        "--user daemon-reload",
        "--user enable --now verdict-prime-sync.timer",
    ]
    removed = _run_installed(
        installed_site, tmp_path, "-m", "verdict", *INSTALL.split()[1:], "--uninstall"
    )
    assert removed.returncode == 0, removed.stderr
    assert not (units / "verdict-prime-sync.service").exists()
    assert not (units / "verdict-prime-sync.timer").exists()


def test_dry_run_renders_the_units_from_this_interpreter_and_package(tmp_path: Path) -> None:
    from verdict.prime_sync_service import SERVICE, TIMER, manage_service

    calls: list[list[str]] = []
    preview = manage_service(unit_dir=tmp_path / "units", dry_run=True, run=calls.append)
    assert calls == [] and not (tmp_path / "units").exists()
    assert preview["plan"] == {SERVICE: "create", TIMER: "create"}
    service, timer = preview["files"][SERVICE], preview["files"][TIMER]
    package_parent = Path(verdict.__file__).resolve().parent.parent
    assert f"WorkingDirectory={package_parent}\n" in service
    assert (
        f'ExecStart="{Path(sys.executable).absolute()}" -m verdict harness prime sync-models\n'
        in service
    )
    assert "VERDICT_CHECKOUT" not in service and "Type=oneshot\n" in service
    for line in ("OnBootSec=2min", "OnUnitActiveSec=15min", "Persistent=true", f"Unit={SERVICE}"):
        assert f"{line}\n" in timer
    assert timer.endswith("[Install]\nWantedBy=timers.target\n")


def test_install_repeat_replace_and_uninstall_are_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from verdict.prime_sync_service import SERVICE, TIMER, manage_service

    monkeypatch.setenv("VERDICT_HOME", str(tmp_path / ".verdict"))
    units = tmp_path / "units"
    enable = ["systemctl", "--user", "enable", "--now", TIMER]
    calls: list[list[str]] = []
    first = manage_service(unit_dir=units, run=calls.append)
    assert calls == [["systemctl", "--user", "daemon-reload"], enable]
    assert (units / SERVICE).read_text() == first["files"][SERVICE]

    calls.clear()
    again = manage_service(unit_dir=units, run=calls.append)
    assert again["changed"] is False and calls == [enable] and again["rollback"] is None

    # A unit copied by hand from the old deploy/systemd template is replaced, with a rollback.
    (units / SERVICE).write_text("[Service]\nEnvironment=VERDICT_CHECKOUT=%h/dev/verdict-core\n")
    replaced = manage_service(unit_dir=units, run=lambda _args: None)
    assert replaced["plan"] == {SERVICE: "replace", TIMER: "keep"}
    assert "VERDICT_CHECKOUT" in (Path(replaced["rollback"]) / SERVICE).read_text()
    assert replaced["commands"] == [enable]

    # A replaced timer is restarted: enable --now keeps a running timer on its old schedule.
    (units / TIMER).write_text("[Timer]\nOnUnitActiveSec=1h\n")
    calls.clear()
    retimed = manage_service(unit_dir=units, run=calls.append)
    assert calls == [
        ["systemctl", "--user", "daemon-reload"],
        enable,
        ["systemctl", "--user", "restart", TIMER],
    ]
    assert "OnUnitActiveSec=1h" in (Path(retimed["rollback"]) / TIMER).read_text()

    calls.clear()
    removed = manage_service(unit_dir=units, uninstall=True, run=calls.append)
    assert calls[0] == ["systemctl", "--user", "disable", "--now", TIMER, SERVICE]
    assert removed["files"] == {} and not (units / SERVICE).exists()
    assert not (units / TIMER).exists()

    # A partial install disables only the unit that exists; nothing left is a no-op.
    (units / TIMER).write_text("[Timer]\n")
    calls.clear()
    partial = manage_service(unit_dir=units, uninstall=True, run=calls.append)
    assert partial["plan"] == {SERVICE: "absent", TIMER: "remove"}
    assert calls == [
        ["systemctl", "--user", "disable", "--now", TIMER],
        ["systemctl", "--user", "daemon-reload"],
    ]
    assert not (units / TIMER).exists()
    calls.clear()
    assert manage_service(unit_dir=units, uninstall=True, run=calls.append)["commands"] == []
    assert calls == []


def test_cli_dry_run_writes_nothing_and_the_action_is_a_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from verdict import cli
    from verdict.actions.registry import get_action

    entry = get_action("harness.prime.install-service")
    assert entry is not None and entry[0].kind == "mutation"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("VERDICT_SKIP_SETUP", "1")
    monkeypatch.setattr(sys, "argv", ["verdict", *INSTALL.split()[1:], "--dry-run"])
    cli.main()
    report: dict[str, Any] = json.loads(capsys.readouterr().out)
    assert report["dry_run"] is True and "verdict-prime-sync.timer" in report["files"]
    assert not (tmp_path / "systemd").exists()
