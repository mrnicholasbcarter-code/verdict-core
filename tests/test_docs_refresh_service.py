"""``verdict memory docs --install-service``: the six-hourly documentation refresh units."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

import verdict
from verdict.docs_refresh_service import SERVICE, TIMER, manage_service

ENABLE = ["systemctl", "--user", "enable", "--now", TIMER]


def test_dry_run_renders_bounded_units_and_writes_nothing(tmp_path: Path) -> None:
    calls: list[list[str]] = []
    preview = manage_service(unit_dir=tmp_path / "units", dry_run=True, run=calls.append)
    assert calls == [] and not (tmp_path / "units").exists()
    assert preview["plan"] == {SERVICE: "create", TIMER: "create"}
    service, timer = preview["files"][SERVICE], preview["files"][TIMER]
    root = Path(verdict.__file__).resolve().parent.parent
    for line in (
        "Type=oneshot",
        f"WorkingDirectory={root}",
        f'ExecStart="{Path(sys.executable).absolute()}" -m verdict memory docs --fix '
        f'--repo-root "{root}"',
        "Nice=19",
        "IOSchedulingClass=idle",
        "CPUWeight=20",
        "MemoryMax=512M",
        "TimeoutStartSec=15min",
    ):
        assert f"{line}\n" in service, line
    for line in (
        "OnBootSec=10min",
        "OnCalendar=*-*-* 00/6:00:00",
        "RandomizedDelaySec=5min",
        "Persistent=true",
        f"Unit={SERVICE}",
    ):
        assert f"{line}\n" in timer, line
    assert timer.endswith("[Install]\nWantedBy=timers.target\n")


def test_interval_is_below_half_the_document_life_plus_renewal_margin() -> None:
    """A six-hour cadence with a 12 h margin leaves 6 h of slack before a 24 h expiry."""
    from verdict.docs_refresh_service import INTERVAL_HOURS
    from verdict.documentation_preflight import (
        DEFAULT_FRESHNESS_SECONDS,
        DEFAULT_RENEW_MARGIN_SECONDS,
    )

    worst_gap = INTERVAL_HOURS * 3600  # a run is missed or delayed by one full interval
    assert worst_gap < DEFAULT_RENEW_MARGIN_SECONDS < DEFAULT_FRESHNESS_SECONDS


def test_install_repeat_and_uninstall(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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

    calls.clear()
    removed = manage_service(unit_dir=units, uninstall=True, run=calls.append)
    assert calls == [
        ["systemctl", "--user", "disable", "--now", TIMER, SERVICE],
        ["systemctl", "--user", "daemon-reload"],
    ]
    assert removed["files"] == {}
    assert not (units / SERVICE).exists() and not (units / TIMER).exists()


def test_rendered_timer_is_valid_systemd_calendar() -> None:
    import shutil

    if shutil.which("systemd-analyze") is None:
        pytest.skip("systemd-analyze not available")
    out = subprocess.run(
        ["systemd-analyze", "calendar", "*-*-* 00/6:00:00"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert out.returncode == 0, out.stderr
    assert "Normalized form: *-*-* 00/6:00:00" in out.stdout
    elapse = next(line for line in out.stdout.splitlines() if "Next elapse" in line)
    assert elapse.split()[-2].split(":")[0] in {"00", "06", "12", "18"}


def test_cli_dry_run_prints_the_plan_and_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from verdict import cli

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("VERDICT_SKIP_SETUP", "1")
    monkeypatch.setattr(
        sys, "argv", ["verdict", "memory", "docs", "--install-service", "--dry-run"]
    )
    cli.main()
    report: dict[str, Any] = json.loads(capsys.readouterr().out)
    assert report["dry_run"] is True and set(report["files"]) == {SERVICE, TIMER}
    assert report["unit_dir"] == str(tmp_path / "systemd" / "user")
    assert not (tmp_path / "systemd").exists()
