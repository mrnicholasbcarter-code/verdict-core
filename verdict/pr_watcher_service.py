"""Explicit, idempotent systemd user setup for the PR watcher (every 5 minutes)."""

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

SERVICE = "verdict-pr-watch.service"
TIMER = "verdict-pr-watch.timer"
INTERVAL_MINUTES = 5


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
    repos: list[str] | None = None,
    run: Callable[[list[str]], None] = _systemctl,
) -> dict[str, Any]:
    """Render or apply the PR watcher units. Dry-run never writes or runs systemctl.

    The service runs ``verdict pr-watch --json --repo ...`` from this interpreter,
    package and working directory. It makes no model call for a local checkout.
    Host pressure is bounded by systemd: lowest CPU and IO priority, a small CPU
    weight, a memory ceiling and a start timeout.
    """
    directory = unit_dir or (
        Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / "systemd" / "user"
    )
    root = str(Path(__file__).resolve().parent.parent)

    # Build the repos list for the command
    repo_args = " ".join(f"--repo {_quote(repo)}" for repo in (repos or []))
    command = f"{_quote(str(Path(sys.executable).absolute()))} -m verdict pr-watch --json{(' ' + repo_args) if repo_args else ''}"

    files = (
        {}
        if uninstall
        else {
            SERVICE: (
                "[Unit]\nDescription=Poll PRs, classify CI, bounded reruns, emit transitions (Verdict)\n"
                "[Service]\nType=oneshot\n"
                f"WorkingDirectory={_escape_path(root)}\n"
                f"ExecStart={command}\n"
                "Nice=19\nIOSchedulingClass=idle\nCPUWeight=20\nMemoryMax=512M\n"
                "TimeoutStartSec=15min\n"
            ),
            TIMER: (
                "[Unit]\nDescription=Poll PRs every 5 minutes\n"
                f"[Timer]\nOnBootSec=10min\nOnUnitActiveSec={INTERVAL_MINUTES}min\n"
                "Persistent=true\n"
                f"Unit={SERVICE}\nAccuracySec=30s\n"
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
