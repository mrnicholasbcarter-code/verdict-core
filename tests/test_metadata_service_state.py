"""Metadata timer lifecycle regressions (all state is temporary/local)."""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

from verdict.commands.parsers_autodev import register
from verdict.metadata_service import SERVICE, TIMER, manage_service, timer_state


def test_metadata_install_service_cli_accepts_dry_run() -> None:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    register(commands)

    args = parser.parse_args(["metadata", "install-service", "--dry-run"])

    assert args.command == "metadata"
    assert args.metadata_command == "install-service"
    assert args.dry_run is True


def test_metadata_timer_dry_run_install_and_repeat_are_side_effect_bounded(tmp_path: Path) -> None:
    unit_dir = tmp_path / "systemd" / "user"
    calls: list[list[str]] = []

    preview = manage_service(unit_dir=unit_dir, dry_run=True, run=calls.append)
    assert preview["changed"] is True
    assert calls == []
    assert not unit_dir.exists()
    assert "OnBootSec=5m" in preview["files"][TIMER]
    assert "OnUnitInactiveSec=6h" in preview["files"][TIMER]
    assert "metadata refresh --include-p1" in preview["files"][SERVICE]

    installed = manage_service(unit_dir=unit_dir, run=calls.append)
    assert installed["changed"] is True
    assert (unit_dir / TIMER).read_text() == preview["files"][TIMER]
    assert (unit_dir / SERVICE).read_text() == preview["files"][SERVICE]
    assert calls == [
        ["systemctl", "--user", "daemon-reload"],
        ["systemctl", "--user", "enable", "--now", TIMER],
        ["systemctl", "--user", "try-restart", SERVICE],
    ]

    calls.clear()
    prior_stat = (unit_dir / TIMER).stat()
    repeat = manage_service(unit_dir=unit_dir, run=calls.append)
    assert repeat["changed"] is False
    assert calls == []
    assert (unit_dir / TIMER).stat().st_mtime_ns == prior_stat.st_mtime_ns


def test_metadata_timer_state_exposes_failure_and_trigger() -> None:
    def run(args: list[str]) -> subprocess.CompletedProcess[str]:
        assert "ExecMainExitTimestamp" in args[-2]
        unit = args[-1]
        result = "exit-code" if unit == SERVICE else "success"
        return subprocess.CompletedProcess(
            args,
            0,
            stdout=(
                "LoadState=loaded\nActiveState=active\nSubState=waiting\n"
                "LastTriggerUSec=Sun 2026-10-18 12:00:00 UTC\n"
                "ExecMainExitTimestamp=Sun 2026-10-18 12:00:05 UTC\n"
                f"Result={result}\nExecMainStatus=1\n"
            ),
            stderr="",
        )

    state = timer_state(run=run)
    assert state["installed"] is True
    assert state["active"] is False
    assert state["last_trigger"] == "Sun 2026-10-18 12:00:00 UTC"
    assert state["last_completion"] == "Sun 2026-10-18 12:00:05 UTC"
    assert state["result"] == "exit-code"
    assert state["failed"] is True
    assert state["state"] == "failed"
