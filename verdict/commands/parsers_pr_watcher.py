"""Argument registration for ``verdict pr-watch``."""

from __future__ import annotations

import os
from typing import Any


def _default_state_dir() -> str | None:
    """Return the default state directory from the git common directory, or None if not in a git repo."""
    try:
        import subprocess

        result = subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0 and result.stdout.strip():
            git_common = result.stdout.strip()
            return os.path.join(git_common, "verdict-prime", "pr-watcher")
    except Exception:
        pass
    return None


def _repos_from_env() -> list[str]:
    """Parse repos from VERDICT_PR_WATCH_REPOS environment variable."""
    env = os.environ.get("VERDICT_PR_WATCH_REPOS", "")
    if not env:
        return []
    return [r.strip() for r in env.split(",") if r.strip()]


def register(subparsers: Any) -> None:
    parser = subparsers.add_parser(
        "pr-watch", help="Poll PRs, classify CI, bounded reruns, emit transitions"
    )
    parser.add_argument(
        "--repo",
        action="append",
        default=[],
        help="Repository to watch (OWNER/NAME), repeatable; defaults to VERDICT_PR_WATCH_REPOS",
    )
    parser.add_argument(
        "--state-dir",
        default=None,
        help="Watcher state directory (default: git common dir/verdict-prime/pr-watcher)",
    )
    parser.add_argument(
        "--linear-team",
        action="append",
        default=[],
        dest="linear_team_keys",
        help="Linear team key to extract from branch/PR (repeatable)",
    )
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON result")
    parser.add_argument(
        "--max-attempts", type=int, default=1, help="Maximum rerun attempts per check (default: 1)"
    )
