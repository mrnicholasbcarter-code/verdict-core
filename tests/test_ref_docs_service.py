"""``verdict memory ref-docs install-service``: the hourly reference-docs sync units."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

import verdict
from verdict.ref_docs_service import SERVICE, TIMER, manage_service

ENABLE = ["systemctl", "--user", "enable", "--now", TIMER]


def test_dry_run_renders_bounded_units_and_writes_nothing(tmp_path: Path) -> None:
    calls: list[list[str]] = []
    preview = manage_service(unit_dir=tmp_path / "units", dry_run=True, run=calls.append)
    assert calls == [] and not (tmp_path / "units").exists()
    assert preview["plan"] == {SERVICE: "create", TIMER: "create"}
    service, timer = preview["files"][SERVICE], preview["files"][TIMER]
    package_parent = Path(verdict.__file__).resolve().parent.parent
    for line in (
        "Type=oneshot",
        "After=ai-memory.service network-online.target",
        "EnvironmentFile=-%h/.config/ai-memory/env",
        f"WorkingDirectory={package_parent}",
        f'ExecStart="{Path(sys.executable).absolute()}" -m verdict memory ref-docs sync',
        "Nice=19",
        "IOSchedulingClass=idle",
        "CPUWeight=20",
        "MemoryMax=512M",
        "TimeoutStartSec=30min",
    ):
        assert f"{line}\n" in service, line
    for line in (
        "OnBootSec=15min",
        "OnCalendar=hourly",
        "RandomizedDelaySec=5min",
        "Persistent=true",
        f"Unit={SERVICE}",
    ):
        assert f"{line}\n" in timer, line
    assert timer.endswith("[Install]\nWantedBy=timers.target\n")


def test_install_repeat_replace_and_uninstall(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("VERDICT_HOME", str(tmp_path / ".verdict"))
    units = tmp_path / "units"
    calls: list[list[str]] = []
    first = manage_service(unit_dir=units, run=calls.append)
    assert calls == [["systemctl", "--user", "daemon-reload"], ENABLE]
    assert (units / SERVICE).read_text() == first["files"][SERVICE]
    assert (units / TIMER).read_text() == first["files"][TIMER]

    calls.clear()
    again = manage_service(unit_dir=units, run=calls.append)
    assert again["changed"] is False and calls == [ENABLE] and again["rollback"] is None

    (units / TIMER).write_text("[Timer]\nOnCalendar=daily\n")
    calls.clear()
    retimed = manage_service(unit_dir=units, run=calls.append)
    assert retimed["plan"] == {SERVICE: "keep", TIMER: "replace"}
    assert calls == [
        ["systemctl", "--user", "daemon-reload"],
        ENABLE,
        ["systemctl", "--user", "restart", TIMER],
    ]
    assert "OnCalendar=daily" in (Path(retimed["rollback"]) / TIMER).read_text()

    calls.clear()
    removed = manage_service(unit_dir=units, uninstall=True, run=calls.append)
    assert calls == [
        ["systemctl", "--user", "disable", "--now", TIMER, SERVICE],
        ["systemctl", "--user", "daemon-reload"],
    ]
    assert removed["files"] == {}
    assert not (units / SERVICE).exists() and not (units / TIMER).exists()
    calls.clear()
    assert manage_service(unit_dir=units, uninstall=True, run=calls.append)["commands"] == []
    assert calls == []


def test_cli_dry_run_prints_the_plan_and_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from verdict import cli

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("VERDICT_SKIP_SETUP", "1")
    argv = ["verdict", "memory", "ref-docs", "install-service", "--dry-run"]
    monkeypatch.setattr(sys, "argv", argv)
    cli.main()
    report: dict[str, Any] = json.loads(capsys.readouterr().out)
    assert report["dry_run"] is True and set(report["files"]) == {SERVICE, TIMER}
    assert report["unit_dir"] == str(tmp_path / "systemd" / "user")
    assert not (tmp_path / "systemd").exists()


def test_the_suite_never_lets_the_installer_reach_real_systemctl(tmp_path: Path) -> None:
    with pytest.raises(AssertionError, match="real systemctl"):
        manage_service(unit_dir=tmp_path)
