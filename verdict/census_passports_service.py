"""Explicit, idempotent systemd user setup for the existing bounded prober."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SERVICE = "verdict-census.service"
TIMER = "verdict-census.timer"
RUNTIME_REPO_DIR = "repo"
VERSION_FILE = "version.json"
LAST_RESULT_FILE = "last-result.json"
# The runtime copy runs the census without the source checkout's build state.
IGNORED_NAMES = (
    ".git",
    ".venv",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".tox",
    ".cache",
    "node_modules",
)
IGNORED_SUFFIXES = (".egg-info", ".pyc")
COPY_IGNORE = shutil.ignore_patterns(*IGNORED_NAMES, "*.egg-info", "*.pyc")


def _ignored_path(path: Path) -> bool:
    return any(part in IGNORED_NAMES for part in path.parts) or path.suffix in IGNORED_SUFFIXES


def default_runtime_root() -> Path:
    data_home = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(data_home) / "verdict-census-runtime"


def _quote(value: str) -> str:
    # systemd specifiers and command expansion must not reinterpret paths.
    return '"' + value.replace("%", "%%").replace("\\", "\\\\").replace('"', '\\"') + '"'


def _escape_specifiers(value: str) -> str:
    # Every unit value (quoted or not) still goes through systemd's "%"
    # specifier expansion; WorkingDirectory= has no quoting of its own, so
    # a literal "%" in the path must be escaped here even without _quote.
    return value.replace("%", "%%")


def _systemctl(args: list[str]) -> None:
    subprocess.run(args, check=True, capture_output=True, text=True, timeout=30)


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _package_version(source_root: Path) -> str:
    init = source_root / "verdict" / "__init__.py"
    if init.is_file():
        match = re.search(
            r"^(?:__version__|VERSION)\s*=\s*[\"\']([^\"\']+)", init.read_text(), re.M
        )
        if match:
            return match.group(1)
    return "unknown"


def census_runtime_version(source_root: Path) -> str:
    """Content identity of the installed verdict package the runtime copy runs."""
    package = source_root / "verdict"
    digest = hashlib.sha256()
    for path in sorted(p for p in package.rglob("*") if p.is_file() and not _ignored_path(p)):
        digest.update(path.relative_to(source_root).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
    return f"{_package_version(source_root)}+{digest.hexdigest()[:12]}"


def _update_runtime_repo(source_root: Path, runtime_root: Path) -> dict[str, Any]:
    """Bring runtime/repo to the installed source; a matching stamp is a no-op.

    Only the ``verdict`` package is copied: for an installed (non-checkout)
    source the package dir is all the runtime needs, and copying a whole
    site-packages would shadow the interpreter's own dependencies with frozen
    snapshots.  The swap goes through a staging dir so a failed copy never
    leaves the timer without a runnable repo.
    """
    source_root = source_root.resolve()
    runtime_root = runtime_root.resolve()
    repo = runtime_root / RUNTIME_REPO_DIR
    if source_root == repo or repo in source_root.parents:
        raise ValueError("source_root must not be the runtime copy itself")
    if not (source_root / "verdict").is_dir():
        raise ValueError(f"source root has no verdict package: {source_root}")
    version = census_runtime_version(source_root)
    stamp = _read_json(runtime_root / VERSION_FILE)
    if (
        stamp is not None
        and stamp.get("runtime_version") == version
        and (repo / BUILD_STAMP).is_file()
    ):
        return {"runtime_version": version, "runtime_updated": False}
    runtime_root.mkdir(parents=True, exist_ok=True)
    staging = runtime_root / f".{RUNTIME_REPO_DIR}.staging"
    previous = runtime_root / f".{RUNTIME_REPO_DIR}.old"
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir()
    shutil.copytree(source_root / "verdict", staging / "verdict", ignore=COPY_IGNORE)
    (staging / BUILD_STAMP).write_text(
        json.dumps({"build": package_build(source_root / "verdict"), "source": str(source_root)})
        + "\n"
    )
    shutil.rmtree(previous, ignore_errors=True)
    if repo.exists():
        repo.rename(previous)
    staging.rename(repo)
    shutil.rmtree(previous, ignore_errors=True)
    (runtime_root / VERSION_FILE).write_text(
        json.dumps(
            {
                "runtime_version": version,
                "installed_at": datetime.now(timezone.utc).isoformat(),
                "source_root": str(source_root),
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )
    return {"runtime_version": version, "runtime_updated": True}


def _git_head(checkout: Path) -> str | None:
    """Commit of a checkout or worktree, read from its git files (no git binary)."""
    git = checkout / ".git"
    if git.is_file():
        pointer = git.read_text().strip()
        if not pointer.startswith("gitdir:"):
            return None
        git = (checkout / pointer.removeprefix("gitdir:").strip()).resolve()
    head_file = git / "HEAD"
    if not head_file.is_file():
        return None
    head = head_file.read_text().strip()
    if not head.startswith("ref:"):
        return head or None
    ref = head.removeprefix("ref:").strip()
    common = git
    if (git / "commondir").is_file():
        common = (git / (git / "commondir").read_text().strip()).resolve()
    for base in (git, common):
        if (base / ref).is_file():
            return (base / ref).read_text().strip() or None
    packed = common / "packed-refs"
    if packed.is_file():
        for line in packed.read_text().splitlines():
            sha, _, name = line.partition(" ")
            if name == ref:
                return sha
    return None


BUILD_STAMP = "verdict-build.json"


def package_build(package: Path) -> str:
    """Recorded build of a ``verdict`` package: the git commit it was built from.

    An installed distribution records it in ``direct_url.json``, a checkout in
    its git HEAD and a census runtime copy in its ``verdict-build.json`` stamp.
    A package with none of these is ``unrecorded``.
    """
    root = package.parent
    for dist in sorted(root.glob("verdict_core-*.dist-info")):
        try:
            payload = json.loads((dist / "direct_url.json").read_text())
        except (OSError, ValueError):
            continue
        commit = payload.get("vcs_info", {}).get("commit_id") if isinstance(payload, dict) else None
        if isinstance(commit, str) and commit:
            return f"git {commit[:12]}"
    try:
        stamp = json.loads((root / BUILD_STAMP).read_text())
    except (OSError, ValueError):
        stamp = None
    if isinstance(stamp, dict) and isinstance(stamp.get("build"), str):
        return str(stamp["build"])
    try:
        commit = _git_head(root)
    except OSError:
        commit = None
    return f"git {commit[:12]}" if commit else "unrecorded"


def write_census_last_result(
    report: dict[str, Any], runtime_root: Path | None = None
) -> dict[str, Any]:
    """Persist the finished census report for doctor and status readers."""
    runtime = runtime_root or default_runtime_root()
    payload = {**report, "finished_at": datetime.now(timezone.utc).isoformat()}
    runtime.mkdir(parents=True, exist_ok=True)
    (runtime / LAST_RESULT_FILE).write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    return payload


CENSUS_INTERVAL_SECONDS = 240


def manage_census_service(
    *,
    unit_dir: Path | None = None,
    interval: float = CENSUS_INTERVAL_SECONDS,
    dry_run: bool = False,
    uninstall: bool = False,
    run: Callable[[list[str]], None] = _systemctl,
    source_root: Path | None = None,
    runtime_root: Path | None = None,
) -> dict[str, Any]:
    """Render or apply user units. Dry-run never writes or invokes systemctl.

    Each timer activation runs one bounded census from the runtime copy and
    exits. Install refreshes the runtime copy to the installed source, so a
    merged main never leaves the timer probing stale code.
    """
    from verdict.prove_at_rest_service import plan_units, save_rollback

    if not math.isfinite(interval) or interval <= 0:
        raise ValueError("interval must be positive and finite")
    directory = unit_dir or (
        Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / "systemd" / "user"
    )
    runtime = runtime_root or default_runtime_root()
    source = source_root or Path(__file__).resolve().parent.parent
    command = (
        f"{_quote(str(Path(sys.executable).absolute()))} -m verdict census --max-per-provider 2"
    )
    files = (
        {}
        if uninstall
        else {
            SERVICE: (
                "[Unit]\nDescription=Verdict bounded background route proof\n"
                "[Service]\nType=oneshot\n"
                f"WorkingDirectory={_escape_specifiers(str(runtime / RUNTIME_REPO_DIR))}\n"
                f"ExecStart={command}\n"
            ),
            TIMER: (
                "[Unit]\nDescription=Start Verdict background route proof\n"
                "[Timer]\nOnStartupSec=30\n"
                f"OnUnitInactiveSec={interval:g}\nUnit={SERVICE}\n"
                "[Install]\nWantedBy=timers.target\n"
            ),
        }
    )
    plan = plan_units(
        directory, dict.fromkeys((SERVICE, TIMER), "") if uninstall else files, uninstall=uninstall
    )
    changed = any(step in {"create", "replace", "remove"} for step in plan.values())
    commands = (
        [["systemctl", "--user", "disable", "--now", TIMER, SERVICE]]
        if uninstall and changed
        else []
    )
    if not uninstall:
        commands.append(["systemctl", "--user", "enable", "--now", TIMER])
    runtime_report: dict[str, Any] = {"runtime_version": None, "runtime_updated": False}
    rollback = None
    if not dry_run:
        rollback = save_rollback(directory, plan)
        if uninstall:
            for command_args in commands:
                run(command_args)
            for name in (SERVICE, TIMER):
                (directory / name).unlink(missing_ok=True)
        else:
            runtime_report = _update_runtime_repo(source, runtime)
            directory.mkdir(parents=True, exist_ok=True)
            for name, body in files.items():
                path = directory / name
                if not path.exists() or path.read_text() != body:
                    path.write_text(body)
        if changed:
            run(["systemctl", "--user", "daemon-reload"])
        if not uninstall:
            for command_args in commands:
                run(command_args)
            if changed:
                run(["systemctl", "--user", "try-restart", SERVICE])
    return {
        "unit_dir": str(directory),
        "files": files,
        "changed": changed,
        "dry_run": dry_run,
        "uninstall": uninstall,
        "commands": commands,
        "plan": plan,
        "rollback": rollback,
        "runtime_root": str(runtime),
        **runtime_report,
    }
