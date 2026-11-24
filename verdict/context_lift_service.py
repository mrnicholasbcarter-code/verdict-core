"""Explicit, idempotent systemd user setup for the daily context-lift bench.

The timer runs ``verdict bench context-lift --json`` once a day on FREE capacity
only.  A blocked result (no free identity, gateway down) is written as evidence
and the unit exits 2 — verdict doctor reports it as a warning so the operator
knows why no free routes are available, without blocking a healthy install.
"""

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

SERVICE = "verdict-context-lift.service"
TIMER = "verdict-context-lift.timer"


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
    return {
        "state": state,
        "result": service.get("Result") or "unknown",
        # ExecMainStatus is the actual exit code from the last service run;
        # "2" means the bench exited intentionally with blocked.
        "exec_main_status": service.get("ExecMainStatus"),
        # ActiveEnterTimestamp is when the service last started (systemd format,
        # e.g. "Thu 2026-01-01 12:00:00 UTC"); used to verify the receipt is
        # from the same invocation.
        "active_enter_timestamp": service.get("ActiveEnterTimestamp") or None,
    }


def manage_service(
    *,
    unit_dir: Path | None = None,
    dry_run: bool = False,
    uninstall: bool = False,
    run: Callable[[list[str]], None] = _systemctl,
) -> dict[str, Any]:
    """Render or apply the daily context-lift units.  Dry-run never writes or runs systemctl.

    The service runs this interpreter from this checkout on FREE capacity.
    A blocked result (no qualified identity) exits 2 — the unit records a
    non-success so doctor can surface the block as a warning.  Resource bounds
    keep host pressure minimal.
    """
    directory = unit_dir or (
        Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / "systemd" / "user"
    )
    root = str(Path(__file__).resolve().parent.parent)
    command = f"{_quote(str(Path(sys.executable).absolute()))} -m verdict bench context-lift --json"

    files = (
        {}
        if uninstall
        else {
            SERVICE: (
                "[Unit]\nDescription=Verdict daily context-lift bench (free capacity)\n"
                "After=network-online.target\n"
                "[Service]\nType=oneshot\n"
                f"WorkingDirectory={_escape_path(root)}\n"
                f"ExecStart={command}\n"
                "TimeoutStartSec=10min\n"
                "Nice=19\nIOSchedulingClass=idle\nCPUWeight=10\nMemoryMax=256M\n"
            ),
            TIMER: (
                "[Unit]\nDescription=Start Verdict daily context-lift bench\n"
                "[Timer]\nOnCalendar=daily\nRandomizedDelaySec=10800\nPersistent=true\n"
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
    rollback = None
    if not dry_run:
        rollback = save_rollback(directory, plan)
        if not uninstall:
            directory.mkdir(parents=True, exist_ok=True)
            for name, body in files.items():
                path = directory / name
                if not path.exists() or path.read_text() != body:
                    path.write_text(body)
        if changed:
            run(["systemctl", "--user", "daemon-reload"])
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
