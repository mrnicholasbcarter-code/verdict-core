"""Explicit, idempotent systemd user setup for the admission snapshot keeper.

The timer runs ``verdict admission refresh`` every 5 minutes (after a 60-second
startup delay) so that ``~/.verdict/admit-last-good.json`` stays fresh.  This
prevents ``verdict serve`` from failing closed when it restarts on a loaded host
and cannot reach OmniRoute within its 30-second aggregate deadline.

The service makes no model or inference calls — only the OmniRoute catalog,
provider, health and quota reads that ``load_omniroute_admit_snapshot`` already
does.  ``TimeoutStartSec=5min`` and a 2-minute ``aggregate_deadline_seconds``
give the fetch room to complete on a loaded host.
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

SERVICE = "verdict-admit-snapshot.service"
TIMER = "verdict-admit-snapshot.timer"


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
    """Render or apply the admit-snapshot keeper units.

    Dry-run never writes or runs systemctl.  The service fetches the
    OmniRoute admission snapshot with no model calls and a 2-minute deadline
    (more generous than the serve path's 30-second deadline so that this
    background job succeeds when the host is loaded).  ``MemoryMax=512M``
    covers the full 27 MB catalog JSON plus parsing headroom; ``Nice=10``
    keeps the host responsive without losing the race to load.
    """
    directory = unit_dir or (
        Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / "systemd" / "user"
    )
    root = str(Path(__file__).resolve().parent.parent)
    command = f"{_quote(str(Path(sys.executable).absolute()))} -m verdict admission refresh"

    files = (
        {}
        if uninstall
        else {
            SERVICE: (
                "[Unit]\nDescription=Verdict admission snapshot keeper (no model calls)\n"
                "After=network-online.target\n"
                "[Service]\nType=oneshot\n"
                f"WorkingDirectory={_escape_path(root)}\n"
                f"ExecStart={command}\n"
                "TimeoutStartSec=5min\n"
                "Nice=10\nMemoryMax=512M\n"
            ),
            TIMER: (
                "[Unit]\nDescription=Run Verdict admission snapshot keeper every 5 minutes\n"
                f"[Timer]\nOnStartupSec=60\nOnUnitInactiveSec=300\n"
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
                if plan.get(name) in {"create", "replace"}:
                    path.write_text(body, encoding="utf-8")
        for args in commands:
            run(args)
        if uninstall:
            for name in (SERVICE, TIMER):
                (directory / name).unlink(missing_ok=True)
    return {"plan": plan, "changed": changed, "rollback": rollback, "dry_run": dry_run}
