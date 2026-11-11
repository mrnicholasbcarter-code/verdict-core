"""Explicit, idempotent systemd user setup for the six-hourly documentation refresh."""

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

SERVICE = "verdict-docs-refresh.service"
TIMER = "verdict-docs-refresh.timer"
#: ``memory docs --fix`` renews documents with under 12 h left; the 24 h life then never lapses.
INTERVAL_HOURS = 6


def timer_state(unit: str = TIMER) -> dict[str, Any]:
    """Timer state plus the service's last result, for doctor."""
    from verdict import metadata_service

    values = metadata_service._show(unit, metadata_service._run_show)
    service = metadata_service._show(SERVICE, metadata_service._run_show)
    if values.get("LoadState") != "loaded":
        return {"state": "missing", "result": "unknown"}
    if values.get("ActiveState") == "failed" or service.get("ActiveState") == "failed":
        state = "failed"
    else:
        state = "active" if values.get("ActiveState") == "active" else "inactive"
    return {"state": state, "result": service.get("Result") or "unknown"}


def manage_service(
    *,
    unit_dir: Path | None = None,
    dry_run: bool = False,
    uninstall: bool = False,
    run: Callable[[list[str]], None] = _systemctl,
) -> dict[str, Any]:
    """Render or apply the documentation-refresh units. Dry-run never writes or runs systemctl.

    The service runs ``verdict memory docs --fix`` from this interpreter, package and
    working directory: the documentation manifest is keyed to that checkout's commit.
    It makes no model call and no network call for a local checkout. Host pressure is
    bounded by systemd: lowest CPU and IO priority, a small CPU weight, a memory ceiling
    and a start timeout.
    """
    directory = unit_dir or (
        Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / "systemd" / "user"
    )
    root = str(Path(__file__).resolve().parent.parent)
    command = (
        f"{_quote(str(Path(sys.executable).absolute()))} -m verdict memory docs --fix "
        f"--repo-root {_quote(root)}"
    )
    files = (
        {}
        if uninstall
        else {
            SERVICE: (
                "[Unit]\nDescription=Refresh authoritative project documentation memory (Verdict)\n"
                "[Service]\nType=oneshot\n"
                f"WorkingDirectory={_escape_path(root)}\n"
                f"ExecStart={command}\n"
                "Nice=19\nIOSchedulingClass=idle\nCPUWeight=20\nMemoryMax=512M\n"
                "TimeoutStartSec=15min\n"
            ),
            TIMER: (
                "[Unit]\nDescription=Refresh authoritative project documentation every 6 hours\n"
                f"[Timer]\nOnBootSec=10min\nOnCalendar=*-*-* 00/{INTERVAL_HOURS}:00:00\n"
                "RandomizedDelaySec=5min\nPersistent=true\n"
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
        present = [name for name in (TIMER, SERVICE) if plan[name] == "remove"]
        commands = [["systemctl", "--user", "disable", "--now", *present]] if present else []
    else:
        commands = [["systemctl", "--user", "enable", "--now", TIMER]]
        if plan[TIMER] == "replace":
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
