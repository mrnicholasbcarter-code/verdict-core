"""Explicit systemd user timer for periodic model metadata refreshes."""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

SERVICE = "verdict-metadata-refresh.service"
TIMER = "verdict-metadata-refresh.timer"


def _systemctl(args: list[str]) -> None:
    subprocess.run(args, check=True, capture_output=True, text=True, timeout=30)


def _quote(value: str) -> str:
    return '"' + value.replace("%", "%%").replace("\\", "\\\\").replace('"', '\\"') + '"'


def _escape_path(value: str) -> str:
    return value.replace("%", "%%").replace("\\", "\\\\").replace(" ", r"\x20")


def manage_service(
    *,
    unit_dir: Path | None = None,
    dry_run: bool = False,
    uninstall: bool = False,
    run: Callable[[list[str]], None] = _systemctl,
) -> dict[str, Any]:
    """Render or install one timer; dry-run never writes or calls systemctl."""
    directory = unit_dir or (
        Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / "systemd" / "user"
    )
    files = (
        {}
        if uninstall
        else {
            SERVICE: (
                "[Unit]\nDescription=Refresh Verdict model metadata\n"
                "[Service]\nType=oneshot\n"
                f"WorkingDirectory={_escape_path(str(Path(__file__).resolve().parent.parent))}\n"
                f"ExecStart={_quote(str(Path(sys.executable).absolute()))} -m verdict metadata refresh --include-p1\n"
            ),
            TIMER: (
                "[Unit]\nDescription=Refresh Verdict model metadata every six hours\n"
                "[Timer]\nOnBootSec=5m\nOnUnitInactiveSec=6h\n"
                f"Unit={SERVICE}\nPersistent=true\n[Install]\nWantedBy=timers.target\n"
            ),
        }
    )
    changed = (
        any((directory / name).exists() for name in (SERVICE, TIMER))
        if uninstall
        else any(
            not (directory / name).exists() or (directory / name).read_text() != body
            for name, body in files.items()
        )
    )
    commands = (
        [["systemctl", "--user", "disable", "--now", TIMER, SERVICE]]
        if uninstall and changed
        else []
    )
    if not uninstall and changed:
        commands.append(["systemctl", "--user", "enable", "--now", TIMER])
    if not dry_run:
        if uninstall:
            for args in commands:
                run(args)
            for name in (SERVICE, TIMER):
                (directory / name).unlink(missing_ok=True)
        else:
            directory.mkdir(parents=True, exist_ok=True)
            for name, body in files.items():
                path = directory / name
                if not path.exists() or path.read_text() != body:
                    path.write_text(body)
        if changed:
            run(["systemctl", "--user", "daemon-reload"])
            if not uninstall:
                for args in commands:
                    run(args)
                run(["systemctl", "--user", "try-restart", SERVICE])
    return {
        "unit_dir": str(directory),
        "files": files,
        "changed": changed,
        "dry_run": dry_run,
        "uninstall": uninstall,
        "commands": commands,
    }


def _show(
    unit: str, run: Callable[[list[str]], subprocess.CompletedProcess[str]]
) -> dict[str, str]:
    try:
        result = run(
            [
                "systemctl",
                "--user",
                "show",
                "-p",
                "LoadState,ActiveState,SubState,LastTriggerUSec,Result,ExecMainStatus,ExecMainExitTimestamp,NextElapseUSecRealtime,NextElapseUSecMonotonic",
                unit,
            ]
        )
    except (OSError, subprocess.SubprocessError):
        return {}
    values: dict[str, str] = {}
    for line in result.stdout.splitlines():
        key, _, value = line.partition("=")
        if key:
            values[key] = value
    return values


def _run_show(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, capture_output=True, text=True, timeout=10)


def timer_state(
    unit: str = TIMER, *, run: Callable[[list[str]], subprocess.CompletedProcess[str]] | None = None
) -> dict[str, Any]:
    """Report timer installation, activation, last trigger and unit result."""
    run = run or _run_show
    values = _show(unit, run)
    service_values = _show(SERVICE, run) if unit == TIMER else {}
    load = values.get("LoadState", "not-found")
    active = values.get("ActiveState", "inactive")
    result = service_values.get("Result", values.get("Result", "unknown"))
    failed = (
        active == "failed"
        or result not in {"success", "unknown"}
        or service_values.get("ActiveState") == "failed"
    )
    if load != "loaded":
        state = "missing"
    elif failed:
        state = "failed"
    elif active == "active":
        state = "active"
    else:
        state = "inactive"
    return {
        "installed": load == "loaded",
        "failed": failed,
        "active": state == "active",
        "state": state,
        "last_trigger": values.get("LastTriggerUSec") or None,
        "last_completion": service_values.get("ExecMainExitTimestamp") or None,
        "result": result,
        "exit_status": service_values.get("ExecMainStatus", values.get("ExecMainStatus")),
    }
