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


def _systemctl(args: list[str]) -> None:
    subprocess.run(args, check=True, capture_output=True, text=True, timeout=30)


def manage_service(
    *, unit_dir: Path | None = None, interval: float = 300, max_requests: int = 300,
    dry_run: bool = False, uninstall: bool = False,
    run: Callable[[list[str]], None] = _systemctl,
) -> dict[str, Any]:
    """Render or apply user units. Dry-run never writes or invokes systemctl.

    The timer starts the long-running daemon after login; the daemon owns
    cycle intervals and bounded requests. No second probing loop is added.
    """
    if not math.isfinite(interval) or interval <= 0 or max_requests < 1:
        raise ValueError("interval and max-requests must be positive finite budgets")
    directory = unit_dir or (Path(os.environ.get(
        "XDG_CONFIG_HOME", str(Path.home() / ".config"))) / "systemd" / "user")
    command = (f"{_quote(sys.executable)} -m verdict prove-at-rest daemon --allow-live-probe "
               f"--interval {interval:g} --max-requests {max_requests} --max-wall-seconds 600")
    files = {} if uninstall else {
        SERVICE: ("[Unit]\nDescription=Verdict bounded background route proof\n"
                  "[Service]\nType=simple\n"
                  f"WorkingDirectory={_quote(str(Path(__file__).resolve().parent.parent))}\n"
                  f"ExecStart={command}\nRestart=on-failure\nRestartSec=30\n"),
        TIMER: ("[Unit]\nDescription=Start Verdict background route proof\n"
                "[Timer]\nOnStartupSec=30\n"
                f"OnUnitInactiveSec={interval:g}\nUnit={SERVICE}\n"
                "[Install]\nWantedBy=timers.target\n"),
    }
    changed = (any((directory / name).exists() for name in (SERVICE, TIMER))
               if uninstall else any(not (directory / name).exists() or
                   (directory / name).read_text() != body for name, body in files.items()))
    commands = ([["systemctl", "--user", "disable", "--now", TIMER, SERVICE]] if uninstall
                and changed else [])
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
    return {"unit_dir": str(directory), "files": files, "changed": changed,
            "dry_run": dry_run, "uninstall": uninstall, "commands": commands}
