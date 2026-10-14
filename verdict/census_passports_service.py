"""Explicit, idempotent systemd user setup for the existing bounded prober."""

from __future__ import annotations

import math
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

SERVICE = "verdict-census.service"
TIMER = "verdict-census.timer"


def _quote(value: str) -> str:
    # systemd specifiers and command expansion must not reinterpret paths.
    return '"' + value.replace("%", "%%").replace("\\", "\\\\").replace('"', '\\"') + '"'


def _escape_specifiers(value: str) -> str:
    # Every unit value (quoted or not) still goes through systemd's "%"
    # specifier expansion; WorkingDirectory= has no quoting of its own, so
    # a literal "%" in the path must be escaped here even without _quote.
    return value.replace("%", "%%")


def _systemctl(args: list[str]) -> None:
    subprocess.run(args, check=True, capture_output=True, text=True, timeout=30)


CENSUS_INTERVAL_SECONDS = 240


def manage_census_service(
    *,
    unit_dir: Path | None = None,
    interval: float = CENSUS_INTERVAL_SECONDS,
    dry_run: bool = False,
    uninstall: bool = False,
    run: Callable[[list[str]], None] = _systemctl,
) -> dict[str, Any]:
    """Render or apply user units. Dry-run never writes or invokes systemctl.

    Each timer activation runs one bounded census and exits.
    """
    if not math.isfinite(interval) or interval <= 0:
        raise ValueError("interval must be positive and finite")
    directory = unit_dir or (
        Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / "systemd" / "user"
    )
    command = f"{_quote(str(Path(sys.executable).absolute()))} -m verdict census --max-candidates 3 --max-tokens 768"
    files = (
        {}
        if uninstall
        else {
            SERVICE: (
                "[Unit]\nDescription=Verdict bounded background route proof\n"
                "[Service]\nType=oneshot\n"
                f"WorkingDirectory={_escape_specifiers(str(Path(__file__).resolve().parent.parent))}\n"
                f"ExecStart={command}\n"
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
