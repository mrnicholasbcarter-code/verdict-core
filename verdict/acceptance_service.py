"""Explicit, idempotent systemd user setup for the hourly live acceptance job."""

from __future__ import annotations

import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from verdict.acceptance import RED_EXIT
from verdict.prove_at_rest_service import (
    _escape_path,
    _quote,
    _systemctl,
    plan_units,
    save_rollback,
)

SERVICE = "verdict-acceptance.service"
TIMER = "verdict-acceptance.timer"


def manage_service(
    *,
    unit_dir: Path | None = None,
    dry_run: bool = False,
    uninstall: bool = False,
    run: Callable[[list[str]], None] = _systemctl,
) -> dict[str, Any]:
    """Render or apply the hourly units. Dry-run never writes or invokes systemctl.

    The unit runs this interpreter from this checkout, so a merged main is what
    the timer measures. A red result exits RED_EXIT and counts as success; only
    a crash fails the unit. Notifications stay dry-run (operator 2026-10-13).
    """
    directory = unit_dir or (
        Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / "systemd" / "user"
    )
    command = (
        f"{_quote(str(Path(sys.executable).absolute()))} -m verdict acceptance run --notify dry-run"
    )
    files = (
        {}
        if uninstall
        else {
            SERVICE: (
                "[Unit]\nDescription=Verdict live acceptance checks\n"
                "After=network-online.target\n"
                "[Service]\nType=oneshot\n"
                f"WorkingDirectory={_escape_path(str(Path(__file__).resolve().parent.parent))}\n"
                f"ExecStart={command}\n"
                f"SuccessExitStatus={RED_EXIT}\nTimeoutStartSec=50min\n"
            ),
            TIMER: (
                "[Unit]\nDescription=Start Verdict live acceptance checks\n"
                "[Timer]\nOnBootSec=10min\nOnCalendar=hourly\nPersistent=true\n"
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
        if changed:
            # One run takes minutes; start it without waiting for it.
            commands.append(["systemctl", "--user", "start", "--no-block", SERVICE])
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
