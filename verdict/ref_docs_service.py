"""Explicit, idempotent systemd user setup for the hourly reference-docs sync."""

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

SERVICE = "verdict-ref-docs.service"
TIMER = "verdict-ref-docs.timer"


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
    """Render or apply the reference-docs units. Dry-run never writes or invokes systemctl.

    The service runs ``verdict memory ref-docs sync`` from this interpreter and
    package, 15 minutes after boot and then hourly. ai-memory's own env file
    supplies ``AI_MEMORY_AUTH_TOKEN``; Verdict never reads it. Host pressure is
    bounded by systemd: lowest CPU and IO priority, a small CPU weight, a memory
    ceiling and a start timeout.
    """
    directory = unit_dir or (
        Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / "systemd" / "user"
    )
    command = f"{_quote(str(Path(sys.executable).absolute()))} -m verdict memory ref-docs sync"
    files = (
        {}
        if uninstall
        else {
            SERVICE: (
                "[Unit]\nDescription=Sync upstream reference docs into ai-memory (Verdict)\n"
                "After=ai-memory.service network-online.target\n"
                "[Service]\nType=oneshot\n"
                "EnvironmentFile=-%h/.config/ai-memory/env\n"
                f"WorkingDirectory={_escape_path(str(Path(__file__).resolve().parent.parent))}\n"
                f"ExecStart={command}\n"
                "Nice=19\nIOSchedulingClass=idle\nCPUWeight=20\nMemoryMax=512M\n"
                "TimeoutStartSec=30min\n"
            ),
            TIMER: (
                "[Unit]\nDescription=Sync upstream reference docs into ai-memory hourly\n"
                "[Timer]\nOnBootSec=15min\nOnCalendar=hourly\nRandomizedDelaySec=5min\n"
                "Persistent=true\n"
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
        # Disable only units whose file exists: systemctl fails on a missing unit file.
        present = [name for name in (TIMER, SERVICE) if plan[name] == "remove"]
        commands = [["systemctl", "--user", "disable", "--now", *present]] if present else []
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
