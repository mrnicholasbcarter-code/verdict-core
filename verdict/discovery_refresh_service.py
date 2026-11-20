"""Explicit, idempotent systemd user setup for the discovery registry refresh timer.

The timer runs ``verdict discovery refresh --free-only --force`` every
``INTERVAL_S`` seconds (after a 2-minute startup delay) so that
``$VERDICT_HOME/discovery/registry.json`` stays within ``REGISTRY_TTL_S``
(12 hours).  Using ``--free-only`` ensures that no paid or subscription model
calls are made; using ``--force`` re-probes on every fire so entries are never
older than ``INTERVAL_S`` regardless of the registry's own TTL.

A single proof-time-bounded ``--wall-budget 300`` keeps the timer from
stalling the host.  ``TimeoutStartSec=10min`` gives the fetch room to finish
even on a loaded host.  ``MemoryMax=512M`` and ``Nice=10`` keep the job
well-behaved.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from verdict.discovery.registry import REGISTRY_TTL_S
from verdict.prove_at_rest_service import (
    _escape_path,
    _quote,
    _systemctl,
    plan_units,
    save_rollback,
)

SERVICE = "verdict-discovery-refresh.service"
TIMER = "verdict-discovery-refresh.timer"

#: Fire every half-TTL so entries are always refreshed before they expire.
#: ``INTERVAL_S <= REGISTRY_TTL_S`` is an invariant checked by tests.
INTERVAL_S: int = REGISTRY_TTL_S // 2  # 21 600 s = 6 h

#: Warn in doctor when the registry is older than this many seconds.
#: At the full TTL the registry is on the expiry boundary; warn then so there
#: is time to repair before admission starts seeing ``None`` from load_registry.
REGISTRY_FRESHNESS_WARN_S: int = REGISTRY_TTL_S  # 43 200 s = 12 h


def timer_state(unit: str = TIMER) -> dict[str, Any]:
    """Timer state plus the service's last result, for doctor.

    Returns ``{"state": "missing", "result": "unknown"}`` when systemd or
    systemctl is unavailable (not installed, container, CI runner, etc.) so
    that doctor rows stay ``failed``/``not-configured`` rather than raising.
    """
    try:
        from verdict import metadata_service

        values = metadata_service._show(unit, metadata_service._run_show)
        service = metadata_service._show(SERVICE, metadata_service._run_show)
    except Exception:
        return {"state": "missing", "result": "unknown"}
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
    """Render or apply the discovery-refresh timer units.

    Dry-run never writes or runs systemctl.  The service probes FREE-capacity
    routes only (``--free-only --force``) with a 5-minute wall budget and no
    paid or subscription calls.  ``--force`` bypasses the registry's own TTL
    so every timer fire produces a fresh probe pass.
    """
    directory = unit_dir or (
        Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / "systemd" / "user"
    )
    root = str(Path(__file__).resolve().parent.parent)
    command = (
        f"{_quote(str(Path(sys.executable).absolute()))}"
        " -m verdict discovery refresh --free-only --force --wall-budget 300"
    )

    files = (
        {}
        if uninstall
        else {
            SERVICE: (
                "[Unit]\nDescription=Verdict discovery registry refresh (FREE routes only)\n"
                "After=network-online.target\n"
                "[Service]\nType=oneshot\n"
                f"WorkingDirectory={_escape_path(root)}\n"
                f"ExecStart={command}\n"
                "TimeoutStartSec=10min\n"
                "Nice=10\nMemoryMax=512M\n"
            ),
            TIMER: (
                "[Unit]\nDescription=Refresh Verdict discovery registry every 6 hours "
                "(FREE routes only)\n"
                f"[Timer]\nOnStartupSec=120\nOnUnitInactiveSec={INTERVAL_S}\n"
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
        # Create the directory before save_rollback so it can write the
        # rollback manifest; on a fresh host the directory may not exist yet.
        if not uninstall:
            directory.mkdir(parents=True, exist_ok=True)
        rollback = save_rollback(directory, plan)
        if not uninstall:
            for name, body in files.items():
                path = directory / name
                if plan.get(name) in {"create", "replace"}:
                    path.write_text(body, encoding="utf-8")
            # systemd must load changed unit files before enable --now.
            if changed:
                run(["systemctl", "--user", "daemon-reload"])
        for args in commands:
            run(args)
        if uninstall:
            for name in (SERVICE, TIMER):
                (directory / name).unlink(missing_ok=True)
            if changed:
                run(["systemctl", "--user", "daemon-reload"])
    return {"plan": plan, "changed": changed, "rollback": rollback, "dry_run": dry_run}
