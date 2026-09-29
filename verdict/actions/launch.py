"""Domain launch entries for BOD-275 Lane E.

Long-running / interactive CLI surfaces point their ``LaunchSpec.entry`` at
functions in this module (or another domain module), never at the CLI layer.
This keeps the TUI independent of the CLI.

Every entry is imported and called by the matching CLI handler through the
shared function reference, and by the TUI palette through the LaunchSpec.
"""

from __future__ import annotations

import asyncio
import importlib.util
import subprocess
import sys
from pathlib import Path
from typing import Any


def launch_dashboard() -> int:
    """Launch the Streamlit analytics dashboard as a subprocess.

    Returns the streamlit process exit code. Used by ``verdict ui``.
    """
    spec = importlib.util.find_spec("verdict.dashboard")
    if spec is None or spec.origin is None:
        return 1
    completed = subprocess.run([sys.executable, "-m", "streamlit", "run", spec.origin], check=False)
    return completed.returncode


def supervise_run(
    *,
    run_id: str,
    runs_dir: Path,
    stall_seconds: float,
    poll_seconds: float,
    max_restarts: int,
    total_deadline_seconds: float,
    command_factory: Any,
) -> dict[str, Any]:
    """Run the supervisor controller against an orchestration run.

    Returns the outcome payload; the CLI handler wraps this with argparse
    plumbing. The heavy lifting stays in ``verdict.orchestration.supervisor``.
    """
    from verdict.orchestration.supervisor import ControllerSupervisor

    supervisor = ControllerSupervisor(
        command_factory,
        runs_dir / run_id,
        stall_seconds=stall_seconds,
        poll_seconds=poll_seconds,
        max_restarts=max_restarts,
        total_deadline_seconds=total_deadline_seconds,
    )
    outcome = asyncio.run(supervisor.run())
    return {
        "run_id": run_id,
        "state": outcome.state,
        "reason": outcome.reason,
        "restarts": outcome.restarts,
        "generations": outcome.generations,
    }
