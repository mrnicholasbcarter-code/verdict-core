"""Explicit, idempotent systemd user setup for the 15-minute Prime registry sync."""

from __future__ import annotations

import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from verdict.prove_at_rest_service import (
    _escape_path,
    _quote,
    _systemctl,
    plan_units,
    save_rollback,
)

SERVICE = "verdict-prime-sync.service"
TIMER = "verdict-prime-sync.timer"


def manage_service(
    *,
    unit_dir: Path | None = None,
    dry_run: bool = False,
    uninstall: bool = False,
    run: Callable[[list[str]], None] = _systemctl,
) -> dict[str, Any]:
    """Render or apply the prime-sync units. Dry-run never writes or invokes systemctl.

    Like the acceptance timer, the units are rendered from this interpreter and
    the directory that holds this ``verdict`` package, so an installed package
    needs no checkout. The timer runs ``verdict harness prime sync-models`` 2
    minutes after boot and then every 15 minutes.
    """
    directory = unit_dir or (
        Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / "systemd" / "user"
    )
    command = f"{_quote(str(Path(sys.executable).absolute()))} -m verdict harness prime sync-models"
    files = (
        {}
        if uninstall
        else {
            SERVICE: (
                "[Unit]\nDescription=Refresh Prime's OmniRoute model registry from the live "
                "gateway (Verdict)\nAfter=network-online.target\n"
                "[Service]\nType=oneshot\n"
                f"WorkingDirectory={_escape_path(str(Path(__file__).resolve().parent.parent))}\n"
                f"ExecStart={command}\n"
            ),
            TIMER: (
                "[Unit]\nDescription=Refresh Prime's OmniRoute model registry every 15 minutes\n"
                "[Timer]\nOnBootSec=2min\nOnUnitActiveSec=15min\nPersistent=true\n"
                f"Unit={SERVICE}\n"
                "[Install]\nWantedBy=timers.target\n"
            ),
        }
    )
    plan = plan_units(
        directory, dict.fromkeys((SERVICE, TIMER), "") if uninstall else files, uninstall=uninstall
    )
    changed = any(step in {"create", "replace", "remove"} for step in plan.values())
    if uninstall:
        commands = [["systemctl", "--user", "disable", "--now", TIMER, SERVICE]] if changed else []
    else:
        commands = [["systemctl", "--user", "enable", "--now", TIMER]]
        if plan[TIMER] == "replace":
            # enable --now leaves an already running timer on its old schedule.
            commands.append(["systemctl", "--user", "restart", TIMER])
    rollback = None
    if not dry_run:
        rollback = save_rollback(directory, plan)
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
    return {
        "unit_dir": str(directory),
        "files": files,
        "changed": changed,
        "dry_run": dry_run,
        "uninstall": uninstall,
        "commands": commands,
        "plan": plan,
        "rollback": rollback,
    }
