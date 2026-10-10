"""Startup routing reuses the doctor action and setup wizard, not a second checker."""

import os
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from rich.text import Text

from verdict.home_action_render import render_doctor
from verdict.home_setup_render import _ask, run_setup_wizard
from verdict.orchestration.run import resolve_api_key
from verdict.setup_config import config_path
from verdict.terminal_ui import TerminalUI


def setup_complete(home: Path) -> bool:
    return (
        (home / "setup-complete").is_file()
        and config_path().is_file()
        and bool(resolve_api_key() or resolve_api_key("OMNIROUTE_API_KEY"))
    )


def _preflight(run: Callable[..., tuple[bool, Any]]) -> tuple[bool, Any] | None:
    """A daemon read-only collector has a launch deadline; never delay home forever."""
    result: list[tuple[bool, Any]] = []
    done = threading.Event()

    def collect() -> None:
        try:
            result.append(run("doctor", {"fix": False, "preflight_timeout": 2.0}))
        except Exception:
            pass
        finally:
            done.set()

    threading.Thread(target=collect, daemon=True).start()
    if not done.wait(3.0):
        return None
    return result[0] if result else None


def _backup_doctor_paths(home: Path) -> None:
    """Snapshot known doctor-repaired files before giving the canonical fix consent."""
    import tempfile

    candidates = [
        config_path(),
        config_path().with_name("config.yaml"),
        config_path().with_name("credentials.env"),
        Path.cwd() / ".mcp.json",
        Path.home() / ".mcp.json",
        Path.cwd() / ".verdict" / "config.toml",
    ]
    backup_dir = home / "doctor-backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    for path in candidates:
        if path.is_file():
            with tempfile.NamedTemporaryFile(
                dir=backup_dir, prefix=path.name + "-", delete=False
            ) as target:
                os.fchmod(target.fileno(), 0o600)
                target.write(path.read_bytes())


def route_startup(
    tui: TerminalUI,
    *,
    home: Path,
    gateway: str,
    run: Callable[..., tuple[bool, Any]],
    reader: Callable[[], str | None] | None = None,
) -> int | None:
    if not setup_complete(home):
        tui.header("First-run setup")
        if reader is None and not tui.interactive:
            tui.console.print(
                Text(
                    "Setup required. Run verdict setup in a terminal, or use --skip-setup / VERDICT_SKIP_SETUP=1 for scripts."
                )
            )
            return 0
        ok, plan = run("setup.plan")
        if ok:
            run_setup_wizard(
                tui, gateway=gateway, plan=plan, state_dir=home / "bootstrap", reader=reader
            )
        return 0 if ok else 1
    result = _preflight(run)
    if result is None:
        tui.console.print(
            Text("Startup health unknown: doctor preflight timed out or failed. Run /doctor.")
        )
        return None
    ok, report = result
    if not isinstance(report, dict):
        tui.console.print(Text("Startup health unknown. Run /doctor."))
        return None
    if not ok or report.get("issues"):
        render_doctor(tui, report)
        if reader is None and not tui.interactive:
            return 1
        tui.console.print(
            Text(
                "Safe doctor repairs may change configuration. Existing files will be backed up first."
            )
        )
        if _ask(tui, reader, "Confirm automatic doctor repairs? [y/N]").lower() in {"y", "yes"}:
            _backup_doctor_paths(home)
            fixed_ok, fixed = run("doctor", {"fix": True, "preflight_timeout": 2.0})
            render_doctor(tui, fixed)
            if not fixed_ok:
                tui.console.print(
                    Text("Unresolved findings remain; continue only after reviewing repair hints.")
                )
        return None
    for warning in report.get("warnings", []):
        tui.console.print(Text("Startup warning: " + str(warning)))
    return None
