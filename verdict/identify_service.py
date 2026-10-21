"""Passive identify maintenance and explicitly bounded daily qualification units."""

from __future__ import annotations

import json
import os
import sys
import tempfile
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from verdict.metadata_service import _run_show, _show
from verdict.paths import verdict_home
from verdict.prove_at_rest_service import (
    _escape_path,
    _quote,
    _systemctl,
    plan_units,
    save_rollback,
)

SERVICE = "verdict-identify.service"
TIMER = "verdict-identify.timer"
SCREEN_SERVICE = "verdict-identify-screen.service"
SCREEN_TIMER = "verdict-identify-screen.timer"
# D3 defaults approved by the operator on 2026-10-21; measure host outcomes.
PASSIVE_INTERVAL_SECONDS = 3 * 60 * 60
SCREEN_CALENDAR = "daily"
SCREEN_MAX_PER_PROVIDER = 1
SCREEN_MAX_ROUTES = 8
SCREEN_CAPACITY = "free,subscription"


def latest_report_path() -> Path:
    return verdict_home() / "identify-latest.json"


def save_report(report: dict[str, Any]) -> None:
    """Opt-in local snapshot; ordinary passive identify still never writes."""
    path = latest_report_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", dir=path.parent, prefix=".identify-", delete=False
    ) as pending:
        temporary = Path(pending.name)
        try:
            os.fchmod(pending.fileno(), 0o600)
            json.dump(report, pending, sort_keys=True, allow_nan=False)
            pending.flush()
            os.fsync(pending.fileno())
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def report_row(*, now: datetime | None = None) -> dict[str, str]:
    path = latest_report_path()
    checked_at = now or datetime.now(timezone.utc)
    detail = ""
    try:
        report = json.loads(path.read_text())
        if not isinstance(report, dict):
            raise ValueError("report is not an object")
        fetched = datetime.fromisoformat(report["fetched_at"].replace("Z", "+00:00"))
        age = (checked_at - fetched).total_seconds()
        if age < 0 or age >= 2 * PASSIVE_INTERVAL_SECONDS:
            detail = f"{path} is stale or future-dated (age {int(age)}s)"
        elif report.get("source_errors") != []:
            detail = f"{path} has source errors or unknown source status"
        elif report.get("mode") != "passive" or report.get("model_calls") != 0:
            detail = f"{path} is not a zero-call passive report"
        else:
            return {
                "label": "Identify passive report",
                "status": "ok",
                "detail": f"{path} age {int(age)}s; source errors none; model calls 0",
            }
    except (OSError, ValueError, KeyError, TypeError, AttributeError, OverflowError):
        detail = f"{path} missing or unreadable; wait for {SERVICE}"
    return {"label": "Identify passive report", "status": "failed", "detail": detail}


def timer_state(unit: str = TIMER) -> dict[str, Any]:
    """State plus last attempt/result and next scheduled run for doctor."""
    values = _show(unit, _run_show)
    service = _show(SCREEN_SERVICE if unit == SCREEN_TIMER else SERVICE, _run_show)
    active = values.get("ActiveState", "inactive")
    result = service.get("Result", "unknown")
    if values.get("LoadState") != "loaded":
        state = "missing"
    elif active == "failed" or service.get("ActiveState") == "failed":
        state = "failed"
    else:
        state = "active" if active == "active" else "inactive"
    return {
        "state": state,
        "installed": state != "missing",
        "active": state == "active",
        "last_trigger": values.get("LastTriggerUSec") or "unknown",
        "last_completion": service.get("ExecMainExitTimestamp") or "unknown",
        "result": result,
        # Monotonic timestamps are relative to boot, not calendar dates.
        "next_run": (
            values["NextElapseUSecRealtime"]
            if values.get("NextElapseUSecRealtime") not in (None, "", "n/a", "0")
            else "unknown"
        ),
    }


def manage_service(
    *,
    unit_dir: Path | None = None,
    dry_run: bool = False,
    uninstall: bool = False,
    run: Callable[[list[str]], None] = _systemctl,
) -> dict[str, Any]:
    """Install only identify units. Never activate the retired qualification loops."""
    directory = (
        unit_dir
        or Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
        / "systemd"
        / "user"
    )
    prefix = f"{_quote(str(Path(sys.executable).absolute()))} -m verdict identify"
    common = (
        "[Service]\nType=oneshot\nUMask=0077\n"
        f"WorkingDirectory={_escape_path(str(Path(__file__).resolve().parent.parent))}\n"
    )
    files = {
        SERVICE: "[Unit]\nDescription=Verdict passive identification\nAfter=network-online.target\n"
        + common
        + f"ExecStart={prefix} --passive --json --save-report\nTimeoutStartSec=2min\n",
        SCREEN_SERVICE: "[Unit]\nDescription=Verdict bounded missing-proof screening\nAfter=network-online.target\n"
        + common
        + f"ExecStart={prefix} --screen --allow-live-probe --capacity {SCREEN_CAPACITY} --max-per-provider {SCREEN_MAX_PER_PROVIDER} --max-routes {SCREEN_MAX_ROUTES} --json\nTimeoutStartSec=12min\n",
        TIMER: "[Unit]\nDescription=Verdict passive identification timer\n[Timer]\nOnBootSec=2min\n"
        + f"OnUnitActiveSec={PASSIVE_INTERVAL_SECONDS}\nUnit={SERVICE}\n[Install]\nWantedBy=timers.target\n",
        SCREEN_TIMER: "[Unit]\nDescription=Verdict daily bounded screening timer\n[Timer]\n"
        + f"OnCalendar={SCREEN_CALENDAR}\nPersistent=true\nUnit={SCREEN_SERVICE}\n[Install]\nWantedBy=timers.target\n",
    }
    plan = plan_units(directory, files, uninstall=uninstall)
    changed = any(step in {"create", "replace", "remove"} for step in plan.values())
    commands = (
        (
            [
                [
                    "systemctl",
                    "--user",
                    "disable",
                    "--now",
                    TIMER,
                    SCREEN_TIMER,
                    SERVICE,
                    SCREEN_SERVICE,
                ]
            ]
            if changed
            else []
        )
        if uninstall
        else [["systemctl", "--user", "enable", "--now", TIMER, SCREEN_TIMER]]
    )
    rollback = None
    if not dry_run:
        rollback = save_rollback(directory, plan)
        if uninstall:
            for args in commands:
                run(args)
            for name in files:
                (directory / name).unlink(missing_ok=True)
        else:
            directory.mkdir(parents=True, exist_ok=True)
            for name, body in files.items():
                if plan[name] != "keep":
                    (directory / name).write_text(body)
        if changed:
            run(["systemctl", "--user", "daemon-reload"])
        if not uninstall:
            for args in commands:
                run(args)
    return {
        "unit_dir": str(directory),
        "files": {} if uninstall else files,
        "plan": plan,
        "changed": changed,
        "dry_run": dry_run,
        "uninstall": uninstall,
        "commands": commands,
        "rollback": rollback,
    }
