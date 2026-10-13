"""Verdict's systemd user units: one registry that setup installs and doctor checks.

Units never hold a secret. Each command resolves the gateway key itself through
``credentials_store.resolve_gateway_key``, so the installer only checks that a
user service can read a key source (not just the exported shell environment).
"""

from __future__ import annotations

import math
import os
import re
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from verdict.prove_at_rest import (
    DEFAULT_AGENTIC_CONCURRENCY,
    DEFAULT_AGENTIC_MAX_ROUTES,
    DEFAULT_AGENTIC_POOL_CONCURRENCY,
    DEFAULT_MAX_REQUESTS,
    DEFAULT_MAX_WALL_SECONDS,
)

#: ``run(argv) -> (returncode, combined output)``; tests pass a fake systemd.
Runner = Callable[[Sequence[str]], tuple[int, str]]

PROVER = "verdict-prove-at-rest"
PRIME_SYNC = "verdict-prime-sync"
DEFAULT_PROVER_INTERVAL_SECONDS = 300.0
_NO_KEY = (
    "no gateway key a user service can read: run 'verdict credentials set OMNIROUTE_API_KEY' "
    "or write the key to ~/.config/verdict/omniroute-api-key (mode 0600)"
)


class ServiceError(RuntimeError):
    """A unit could not be rendered, installed or removed."""


def run_systemctl(args: Sequence[str]) -> tuple[int, str]:
    """Run one systemctl/journalctl command; 127 when the binary is absent.

    Agent and SSH shells often lack ``XDG_RUNTIME_DIR`` or carry
    ``DBUS_SESSION_BUS_ADDRESS=disabled:``; both stop ``systemctl --user`` from
    reaching the user manager, so the user's runtime dir is filled in.
    """
    env = dict(os.environ)
    runtime = Path(f"/run/user/{os.getuid()}")
    if not env.get("XDG_RUNTIME_DIR") and runtime.is_dir():
        env["XDG_RUNTIME_DIR"] = str(runtime)
    if not env.get("DBUS_SESSION_BUS_ADDRESS", "unix:").startswith("unix:"):
        del env["DBUS_SESSION_BUS_ADDRESS"]
    try:
        done = subprocess.run(
            list(args), capture_output=True, text=True, timeout=180, check=False, env=env
        )
    except FileNotFoundError:
        return 127, f"{args[0]} not found"
    return done.returncode, (done.stdout + done.stderr).strip()


def _quote(value: str) -> str:
    # systemd specifiers and command expansion must not reinterpret paths.
    return '"' + value.replace("%", "%%").replace("\\", "\\\\").replace('"', '\\"') + '"'


@dataclass(frozen=True)
class ProberBudget:
    """Budgets rendered into the prober unit (same flags as ``prove-at-rest daemon``)."""

    interval: float = DEFAULT_PROVER_INTERVAL_SECONDS
    max_requests: int = DEFAULT_MAX_REQUESTS
    max_wall_seconds: float = DEFAULT_MAX_WALL_SECONDS
    agentic_max_routes: int = DEFAULT_AGENTIC_MAX_ROUTES
    agentic_concurrency: int = DEFAULT_AGENTIC_CONCURRENCY
    agentic_pool_concurrency: int = DEFAULT_AGENTIC_POOL_CONCURRENCY
    agentic_wall_seconds: float | None = None

    def __post_init__(self) -> None:
        seconds = [self.interval, self.max_wall_seconds]
        if self.agentic_wall_seconds is not None:
            seconds.append(self.agentic_wall_seconds)
        counts = [self.max_requests, self.agentic_concurrency, self.agentic_pool_concurrency]
        if (
            any(not math.isfinite(value) or value <= 0 for value in seconds)
            or any(value < 1 for value in counts)
            or self.agentic_max_routes < 0
        ):
            raise ValueError("prober budgets must be positive and finite")

    def args(self) -> tuple[str, ...]:
        args = [
            "prove-at-rest",
            "daemon",
            "--allow-live-probe",
            "--interval",
            f"{self.interval:g}",
            "--max-requests",
            str(self.max_requests),
            "--max-wall-seconds",
            f"{self.max_wall_seconds:g}",
            "--agentic-max-routes",
            str(self.agentic_max_routes),
            "--agentic-concurrency",
            str(self.agentic_concurrency),
            "--agentic-pool-concurrency",
            str(self.agentic_pool_concurrency),
        ]
        if self.agentic_wall_seconds is not None:
            args += ["--agentic-wall-seconds", f"{self.agentic_wall_seconds:g}"]
        return tuple(args)


@dataclass(frozen=True)
class UserUnit:
    """One ``<name>.service`` plus the ``<name>.timer`` that starts it."""

    name: str
    description: str
    service_type: str
    args: tuple[str, ...]
    schedule: str

    @property
    def service(self) -> str:
        return f"{self.name}.service"

    @property
    def timer(self) -> str:
        return f"{self.name}.timer"

    def files(self) -> dict[str, str]:
        command = " ".join(
            [_quote(str(Path(sys.executable).absolute())), "-m", "verdict", *self.args]
        )
        restart = "Restart=on-failure\nRestartSec=30\n" if self.service_type == "simple" else ""
        return {
            self.service: (
                f"[Unit]\nDescription={self.description}\nAfter=network-online.target\n"
                "[Service]\n"
                f"Type={self.service_type}\n"
                "# No secret here: the command resolves the gateway key itself\n"
                "# (credentials.env, ~/.config/verdict/omniroute-api-key, Prime auth.json).\n"
                f"WorkingDirectory={_quote(str(Path(__file__).resolve().parent.parent))}\n"
                f"ExecStart={command}\n{restart}"
            ),
            self.timer: (
                f"[Unit]\nDescription=Start {self.description}\n"
                f"[Timer]\n{self.schedule}Unit={self.service}\n"
                "[Install]\nWantedBy=timers.target\n"
            ),
        }


def prover_unit(budget: ProberBudget | None = None) -> UserUnit:
    plan = budget or ProberBudget()
    # The daemon owns its cycles; the timer only restarts it after it exits.
    return UserUnit(
        PROVER,
        "Verdict bounded background route proof",
        "simple",
        plan.args(),
        f"OnStartupSec=30\nOnUnitInactiveSec={plan.interval:g}\n",
    )


def prime_sync_unit() -> UserUnit:
    return UserUnit(
        PRIME_SYNC,
        "Verdict refresh of Prime's OmniRoute model registry",
        "oneshot",
        ("harness", "prime", "sync-models"),
        "OnBootSec=2min\nOnUnitActiveSec=15min\nPersistent=true\n",
    )


def verdict_units(budget: ProberBudget | None = None) -> tuple[UserUnit, ...]:
    """Units this host needs: the prober always, Prime sync only when Prime is installed."""
    from verdict.harness_prime import resolve_paths

    units = [prover_unit(budget)]
    if resolve_paths().models.is_file():
        units.append(prime_sync_unit())
    return tuple(units)


def default_unit_dir() -> Path:
    config = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(config) / "systemd" / "user"


def _checked(run: Runner, args: list[str]) -> None:
    code, output = run(args)
    if code != 0:
        raise ServiceError(f"{' '.join(args)} failed: {output}")


def manage_units(
    units: Sequence[UserUnit],
    *,
    unit_dir: Path | None = None,
    dry_run: bool = False,
    uninstall: bool = False,
    run: Runner | None = None,
) -> dict[str, Any]:
    """Render, install or remove *units*. Dry-run never writes or calls systemctl."""
    from verdict.credentials_store import resolve_gateway_key

    runner = run or run_systemctl
    directory = unit_dir or default_unit_dir()
    files = {name: body for unit in units for name, body in unit.files().items()}
    reload = ["systemctl", "--user", "daemon-reload"]
    key = None
    if uninstall:
        changed = [name for name in files if (directory / name).exists()]
        files = {}
        disable = [
            ["systemctl", "--user", "disable", "--now", unit.timer, unit.service]
            for unit in units
            if unit.timer in changed or unit.service in changed
        ]
        commands = disable + ([reload] if changed else [])
    else:
        key = resolve_gateway_key(environ={})
        if key is None:
            raise ServiceError(_NO_KEY)
        changed = [
            name
            for name, body in files.items()
            if not (directory / name).exists() or (directory / name).read_text() != body
        ]
        commands = [reload] if changed else []
        for unit in units:
            commands.append(["systemctl", "--user", "enable", "--now", unit.timer])
            if unit.service in changed:
                # Run a changed unit once now: a oneshot proves its fix (start
                # waits for the run), a daemon picks up its new budgets.
                verb = "start" if unit.service_type == "oneshot" else "restart"
                commands.append(["systemctl", "--user", verb, unit.service])
    if not dry_run and uninstall:
        for args in disable:
            _checked(runner, args)
        for name in changed:
            (directory / name).unlink(missing_ok=True)
        if changed:
            _checked(runner, reload)
    elif not dry_run:
        directory.mkdir(parents=True, exist_ok=True)
        for name in changed:
            (directory / name).write_text(files[name])
        for args in commands:
            _checked(runner, args)
    return {
        "unit_dir": str(directory),
        "files": files,
        "changed": bool(changed),
        "changed_files": changed,
        "dry_run": dry_run,
        "uninstall": uninstall,
        "commands": commands,
        "key_source": key[1] if key else None,
    }


# ---------------------------------------------------------------------------
# Checks (doctor and ``setup --check``)
# ---------------------------------------------------------------------------

_SHOW_PROPERTIES = (
    "Id,LoadState,ActiveState,SubState,Result,ExecMainStatus,InvocationID,ExecMainExitTimestamp"
)


def _show(run: Runner, names: Sequence[str]) -> tuple[dict[str, dict[str, str]] | None, str]:
    code, output = run(["systemctl", "--user", "show", *names, "-p", _SHOW_PROPERTIES])
    if code != 0:
        return None, output
    blocks: dict[str, dict[str, str]] = {}
    for block in output.split("\n\n"):
        props = dict(line.split("=", 1) for line in block.splitlines() if "=" in line)
        if "Id" in props:
            blocks[props["Id"]] = props
    return blocks, ""


def _last_error_line(run: Runner, invocation: str) -> str:
    from verdict.security import redact_text

    if not re.fullmatch(r"[0-9a-f]{32}", invocation):
        return ""
    code, output = run(
        [
            "journalctl",
            "--user",
            f"_SYSTEMD_INVOCATION_ID={invocation}",
            "-o",
            "cat",
            "-n",
            "20",
            "--no-pager",
            "-q",
        ]
    )
    lines = [line.strip() for line in output.splitlines() if line.strip()] if code == 0 else []
    return redact_text(lines[-1])[:300] if lines else ""


@dataclass
class ServiceCheck:
    rows: list[dict[str, Any]] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def check_units(units: Sequence[UserUnit], *, run: Runner | None = None) -> ServiceCheck:
    """Installed, active, last result and last error line for each unit."""
    run = run or run_systemctl
    check = ServiceCheck()
    names = [name for unit in units for name in (unit.timer, unit.service)]
    shown, error = _show(run, names)
    if shown is None:
        if error.endswith("not found"):
            check.warnings.append("Verdict services not checked: systemd is not available here")
        else:
            check.issues.append(f"Cannot check Verdict services (systemctl --user): {error}")
        return check
    for unit in units:
        timer = shown.get(unit.timer, {})
        service = shown.get(unit.service, {})
        installed = timer.get("LoadState") == "loaded" and service.get("LoadState") == "loaded"
        result = service.get("Result", "")
        failed = service.get("ActiveState") == "failed" or result not in {"", "success"}
        row: dict[str, Any] = {
            "unit": unit.timer,
            "installed": installed,
            "timer_active": timer.get("ActiveState") == "active",
            "service_state": f"{service.get('ActiveState', '')}/{service.get('SubState', '')}",
            "last_result": result or None,
            "last_exit_status": service.get("ExecMainStatus") or None,
            "last_run": service.get("ExecMainExitTimestamp") or None,
            "last_error": "",
        }
        if not installed:
            row["state"] = "missing"
            check.issues.append(f"{unit.timer} is not installed. Run: verdict setup services")
        elif failed:
            row["state"] = "failed"
            row["last_error"] = _last_error_line(run, service.get("InvocationID", ""))
            detail = f": {row['last_error']}" if row["last_error"] else ""
            check.issues.append(
                f"{unit.service} failed (result {result}, exit {row['last_exit_status']}){detail}"
            )
        elif not row["timer_active"]:
            row["state"] = "inactive"
            check.issues.append(
                f"{unit.timer} is {timer.get('ActiveState', 'unknown')}. "
                f"Run: systemctl --user enable --now {unit.timer}"
            )
        else:
            row["state"] = "ok"
        check.rows.append(row)
    return check


def prober_cycle_seconds(unit_dir: Path | None = None) -> float:
    """Longest normal gap between two probe records, from the installed prober unit.

    The chat phase records a probe after every request; the agentic phase can
    wait on token buckets for up to ``--agentic-wall-seconds`` (default
    ``--max-wall-seconds``) between records; then the daemon sleeps
    ``--interval``. Missing flags use the daemon's own defaults.
    """
    path = (unit_dir or default_unit_dir()) / f"{PROVER}.service"
    try:
        text = path.read_text()
    except OSError:
        text = ""

    def flag(name: str, default: float) -> float:
        match = re.search(rf"--{name} (\S+)", text)
        return float(match.group(1)) if match else default

    agentic_wall = flag("agentic-wall-seconds", flag("max-wall-seconds", DEFAULT_MAX_WALL_SECONDS))
    return flag("interval", DEFAULT_PROVER_INTERVAL_SECONDS) + agentic_wall


def check_health_cache(cycle_seconds: float, *, now: datetime | None = None) -> ServiceCheck:
    """The prober's output: a probe recorded within 2 x *cycle_seconds*, and usable routes.

    Age is measured from the newest ``checked_at``, not the file time, because
    other writers (refresh merges) also rewrite the file.
    """
    from verdict.orchestration.health_cache import HealthCache, HealthCacheError, default_cache_path
    from verdict.prove_at_rest import status_report

    check = ServiceCheck()
    current = now or datetime.now(timezone.utc)
    path = default_cache_path()
    if not path.exists():
        check.issues.append(f"Health cache {path} does not exist; the prober has not run")
        return check
    try:
        cache = HealthCache(path)
    except (OSError, HealthCacheError) as exc:
        check.issues.append(f"Health cache {path} is unreadable: {exc}")
        return check
    counts = status_report(cache, now=current)["counts_by_state"]
    newest = max((entry.checked_at for entry in cache.routes().values()), default=None)
    age = (current - newest).total_seconds() if newest else None
    check.rows.append(
        {"path": str(path), "age_seconds": None if age is None else round(age), **counts}
    )
    if age is None or age > 2 * cycle_seconds:
        observed = "no probe recorded" if age is None else f"last probe {age / 60:.0f} min ago"
        check.issues.append(
            f"Health cache {path} is stale: {observed} (limit {2 * cycle_seconds / 60:g} min); "
            f"check: journalctl --user -u {PROVER}.service"
        )
    if int(counts.get("fresh", 0)) + int(counts.get("stale", 0)) == 0:
        check.issues.append(f"Health cache {path} has no fresh or stale route")
    return check


def check_services(*, run: Runner | None = None, now: datetime | None = None) -> ServiceCheck:
    """Doctor's services section: every Verdict unit, then the prober's cache.

    Without systemd the units cannot exist, so the cache is not judged either.
    """
    from verdict.credentials_store import resolve_gateway_key

    units = check_units(verdict_units(), run=run)
    if units.warnings and not units.rows:
        return units
    if resolve_gateway_key(environ={}) is None:
        units.issues.append("Verdict services cannot authenticate: " + _NO_KEY)
    cache = check_health_cache(prober_cycle_seconds(), now=now)
    return ServiceCheck(
        rows=units.rows + [{"health_cache": row} for row in cache.rows],
        issues=units.issues + cache.issues,
        warnings=units.warnings + cache.warnings,
    )
