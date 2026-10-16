"""Explicit, idempotent systemd user setup for the existing bounded prober."""

from __future__ import annotations

import math
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

SERVICE = "verdict-prove-at-rest.service"
TIMER = "verdict-prove-at-rest.timer"


def _quote(value: str) -> str:
    # systemd specifiers and command expansion must not reinterpret paths.
    return '"' + value.replace("%", "%%").replace("\\", "\\\\").replace('"', '\\"') + '"'


def _escape_path(value: str) -> str:
    # systemd rejects quoted WorkingDirectory values; spaces need \x20 and % needs %%.
    return value.replace("%", "%%").replace("\\", "\\\\").replace(" ", r"\x20")


def _systemctl(args: list[str]) -> None:
    subprocess.run(args, check=True, capture_output=True, text=True, timeout=30)


def manage_service(
    *,
    unit_dir: Path | None = None,
    interval: float = 300,
    max_requests: int = 300,
    dry_run: bool = False,
    uninstall: bool = False,
    run: Callable[[list[str]], None] = _systemctl,
) -> dict[str, Any]:
    """Render or apply user units. Dry-run never writes or invokes systemctl.

    The timer starts the long-running daemon after login; the daemon owns
    cycle intervals and bounded requests. No second probing loop is added.
    """
    if not math.isfinite(interval) or interval <= 0 or max_requests < 1:
        raise ValueError("interval and max-requests must be positive finite budgets")
    directory = unit_dir or (
        Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / "systemd" / "user"
    )
    command = (
        f"{_quote(str(Path(sys.executable).absolute()))} -m verdict prove-at-rest daemon --allow-live-probe "
        f"--interval {interval:g} --max-requests {max_requests} --max-wall-seconds 600"
    )
    files = (
        {}
        if uninstall
        else {
            SERVICE: (
                "[Unit]\nDescription=Verdict bounded background route proof\n"
                "[Service]\nType=simple\n"
                f"WorkingDirectory={_escape_path(str(Path(__file__).resolve().parent.parent))}\n"
                f"ExecStart={command}\nRestart=on-failure\nRestartSec=30\n"
            ),
            TIMER: (
                "[Unit]\nDescription=Start Verdict background route proof\n"
                "[Timer]\nOnStartupSec=30\n"
                f"OnUnitInactiveSec={interval:g}\nUnit={SERVICE}\n"
                "[Install]\nWantedBy=timers.target\n"
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
    if not uninstall:
        commands.append(["systemctl", "--user", "enable", "--now", TIMER])
    if not dry_run:
        if uninstall:
            for command_args in commands:
                run(command_args)
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
            for command_args in commands:
                run(command_args)
            if changed:
                run(["systemctl", "--user", "try-restart", SERVICE])
    return {
        "unit_dir": str(directory),
        "files": files,
        "changed": changed,
        "dry_run": dry_run,
        "uninstall": uninstall,
        "commands": commands,
    }


def _systemctl_show(
    unit: str, run: Callable[[list[str]], subprocess.CompletedProcess[str]]
) -> dict[str, str]:
    """``LoadState``/``ActiveState``/``SubState`` for one unit. Never raises."""
    try:
        result = run(["systemctl", "--user", "show", "-p", "LoadState,ActiveState,SubState", unit])
    except (OSError, subprocess.SubprocessError):
        return {}
    values: dict[str, str] = {}
    for line in result.stdout.splitlines():
        key, _, value = line.partition("=")
        if key:
            values[key] = value
    return values


def _run_systemctl_show(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, capture_output=True, text=True, timeout=10)


def timer_state(
    unit: str, *, run: Callable[[list[str]], subprocess.CompletedProcess[str]] = _run_systemctl_show
) -> dict[str, Any]:
    """Installed/failed/active state of a systemd user timer, for doctor and setup.

    Returns ``{"installed": bool, "failed": bool, "active": bool, "state": str}``.
    ``state`` is one of ``"missing"`` (unit file not loaded), ``"failed"``
    (``is-failed``), ``"inactive"`` or ``"active"``. A unit that is loaded but
    not currently running (e.g. waiting for its next trigger) is still
    ``"active"`` when systemd reports ``ActiveState=active``.
    """
    values = _systemctl_show(unit, run)
    load_state = values.get("LoadState", "not-found")
    active_state = values.get("ActiveState", "inactive")
    if load_state != "loaded":
        return {"installed": False, "failed": False, "active": False, "state": "missing"}
    if active_state == "failed":
        return {"installed": True, "failed": True, "active": False, "state": "failed"}
    if active_state == "active":
        return {"installed": True, "failed": False, "active": True, "state": "active"}
    return {"installed": True, "failed": False, "active": False, "state": "inactive"}
