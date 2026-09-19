"""Bounded default InstallRunner for capability bootstrap APPLY (BOD-124).

Consent/allowlist gating lives in ``capability_bootstrap.apply_bootstrap_actions``.
This module only executes the documented OmniRoute upstream install path and
fails closed for every other provider — never silent third-party installs.
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

# Documented upstream OmniRoute install (catalog + CLI hints). Never invent alternates.
OMNIROUTE_PROVIDER_ID = "gateway.omniroute"
OMNIROUTE_INSTALL_ARGV: tuple[str, ...] = ("npm", "install", "-g", "omniroute")
OMNIROUTE_INSTALL_COMMAND = "npm install -g omniroute"
OMNIROUTE_INSTALL_SOURCE = "https://github.com/NeuronZero/omniroute"
DEFAULT_INSTALL_PROVIDERS = frozenset({OMNIROUTE_PROVIDER_ID})
_INSTALL_COMMAND_TIMEOUT_S = 300.0
_INSTALL_OUTPUT_LIMIT = 4000

InstallCommandRunner = Callable[[Sequence[str]], Mapping[str, Any]]
PathResolver = Callable[[str], str | None]
# Accepts BootstrapAction (or any object with provider_id/install_* attrs).
InstallRunner = Callable[[Any], Mapping[str, Any]]


def _truncate_install_output(text: str) -> str:
    if len(text) <= _INSTALL_OUTPUT_LIMIT:
        return text
    return text[:_INSTALL_OUTPUT_LIMIT] + "…[truncated]"


def run_install_command(argv: Sequence[str]) -> Mapping[str, Any]:
    """Execute an exact argv list with shell=False; fail closed on OS errors."""

    try:
        completed = subprocess.run(
            list(argv),
            check=False,
            capture_output=True,
            text=True,
            timeout=_INSTALL_COMMAND_TIMEOUT_S,
            shell=False,
        )
    except FileNotFoundError as exc:
        return {
            "returncode": 127,
            "stdout": "",
            "stderr": _truncate_install_output(str(exc)),
            "error": "executable_not_found",
        }
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout if isinstance(exc.stdout, str) else ""
        stderr = exc.stderr if isinstance(exc.stderr, str) else "timeout"
        return {
            "returncode": 124,
            "stdout": _truncate_install_output(stdout or ""),
            "stderr": _truncate_install_output(stderr or "timeout"),
            "error": "timeout",
        }
    except OSError as exc:
        return {
            "returncode": 1,
            "stdout": "",
            "stderr": _truncate_install_output(str(exc)),
            "error": "os_error",
        }
    return {
        "returncode": int(completed.returncode),
        "stdout": _truncate_install_output(completed.stdout or ""),
        "stderr": _truncate_install_output(completed.stderr or ""),
    }


def write_install_marker(state_dir: Path, provider_id: str) -> Path:
    marker_dir = state_dir / "markers"
    marker_dir.mkdir(parents=True, exist_ok=True)
    marker = marker_dir / f"{provider_id}.installed"
    marker.write_text("ok\n", encoding="utf-8")
    return marker


def run_default_provider_install(
    action: Any,
    *,
    state_dir: Path,
    command_runner: InstallCommandRunner | None = None,
    path_resolver: PathResolver | None = None,
) -> Mapping[str, Any]:
    """Bounded default installer for catalog-documented safe providers only.

    Consent/allowlist gating happens in ``apply_bootstrap_actions`` before this runs.
    Success requires a zero exit *and* postcondition proof (binary on PATH) — never
    guess installed from a record-only stub.
    """

    provider_id = action.provider_id or ""
    if provider_id not in DEFAULT_INSTALL_PROVIDERS:
        return {
            "status": "blocked",
            "provider_id": provider_id,
            "install_command": action.install_command,
            "note": (
                "no default install runner for this provider; "
                "pass an explicit install_runner or allowlist a supported provider "
                f"(e.g. {OMNIROUTE_PROVIDER_ID})"
            ),
        }

    if provider_id != OMNIROUTE_PROVIDER_ID:
        return {
            "status": "blocked",
            "provider_id": provider_id,
            "note": "default install runner refuses unknown provider",
        }

    planned = (action.install_command or "").strip()
    if planned and planned != OMNIROUTE_INSTALL_COMMAND:
        return {
            "status": "blocked",
            "provider_id": provider_id,
            "install_command": planned,
            "expected_command": OMNIROUTE_INSTALL_COMMAND,
            "install_source": action.install_source or OMNIROUTE_INSTALL_SOURCE,
            "note": "plan install_command diverges from documented OmniRoute install; refusing",
        }

    resolver = path_resolver or shutil.which
    existing = resolver("omniroute")
    if existing:
        marker = write_install_marker(state_dir, provider_id)
        return {
            "status": "installed",
            "provider_id": provider_id,
            "install_command": OMNIROUTE_INSTALL_COMMAND,
            "install_source": action.install_source or OMNIROUTE_INSTALL_SOURCE,
            "argv": list(OMNIROUTE_INSTALL_ARGV),
            "skipped_command": True,
            "note": "omniroute already present on PATH; recorded Verdict ownership without reinstall",
            "path": existing,
            "marker": str(marker),
        }

    npm_path = resolver("npm")
    if not npm_path:
        return {
            "status": "failed",
            "provider_id": provider_id,
            "install_command": OMNIROUTE_INSTALL_COMMAND,
            "install_source": action.install_source or OMNIROUTE_INSTALL_SOURCE,
            "argv": list(OMNIROUTE_INSTALL_ARGV),
            "note": "npm not found on PATH; cannot run documented OmniRoute install",
        }

    runner = command_runner or run_install_command
    command_result = runner(OMNIROUTE_INSTALL_ARGV)
    raw_code = command_result.get("returncode", 1)
    returncode = int(1 if raw_code is None else raw_code)
    stdout = str(command_result.get("stdout") or "")
    stderr = str(command_result.get("stderr") or "")
    base: dict[str, Any] = {
        "provider_id": provider_id,
        "install_command": OMNIROUTE_INSTALL_COMMAND,
        "install_source": action.install_source or OMNIROUTE_INSTALL_SOURCE,
        "argv": list(OMNIROUTE_INSTALL_ARGV),
        "npm_path": npm_path,
        "returncode": returncode,
        "stdout": stdout,
        "stderr": stderr,
    }
    if command_result.get("error"):
        base["error"] = command_result["error"]

    if returncode != 0:
        base["status"] = "failed"
        base["note"] = "documented OmniRoute install command exited non-zero; not marking installed"
        return base

    installed_path = resolver("omniroute")
    if not installed_path:
        base["status"] = "failed"
        base["note"] = (
            "install command exited 0 but omniroute binary not found on PATH; "
            "refusing to guess success"
        )
        return base

    marker = write_install_marker(state_dir, provider_id)
    base["status"] = "installed"
    base["path"] = installed_path
    base["marker"] = str(marker)
    base["note"] = "documented OmniRoute install completed; binary verified on PATH"
    return base


def build_default_install_runner(
    *,
    state_dir: Path,
    command_runner: InstallCommandRunner | None = None,
    path_resolver: PathResolver | None = None,
) -> InstallRunner:
    """Return an InstallRunner that only executes documented default installs."""

    def _runner(action: Any) -> Mapping[str, Any]:
        return run_default_provider_install(
            action, state_dir=state_dir, command_runner=command_runner, path_resolver=path_resolver
        )

    return _runner


__all__ = [
    "DEFAULT_INSTALL_PROVIDERS",
    "OMNIROUTE_INSTALL_ARGV",
    "OMNIROUTE_INSTALL_COMMAND",
    "OMNIROUTE_INSTALL_SOURCE",
    "OMNIROUTE_PROVIDER_ID",
    "InstallCommandRunner",
    "InstallRunner",
    "PathResolver",
    "build_default_install_runner",
    "run_default_provider_install",
    "run_install_command",
    "write_install_marker",
]
