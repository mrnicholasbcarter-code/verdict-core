"""Shared verification argv resolution for worker prompts and runtime checks."""

from __future__ import annotations

import shutil
import sys
from collections.abc import Sequence
from pathlib import Path


def resolve_verify_argv(argv: Sequence[str]) -> tuple[list[str], str]:
    """Return the executed argv and an interpreter substitution, if one was needed.

    Bare python/python3 names missing from PATH use Verdict's interpreter.
    The second return value is empty when no substitution was made; it never
    records a guessed executable path.
    """
    resolved = list(argv)
    resolved_argv0 = ""
    if argv and argv[0] in {"python", "python3"} and shutil.which(argv[0]) is None:
        resolved[0] = sys.executable
        resolved_argv0 = sys.executable
    return resolved, resolved_argv0


def resolve_gate_argv(argv: Sequence[str], worktree: Path) -> list[str]:
    """Resolve bare ruff/mypy in the worktree, Verdict environment, then PATH.

    Other executable names (including npm) stay unchanged. A declared Python
    gate with no executable raises FileNotFoundError; callers must fail closed.
    """
    resolved = list(argv)
    if not argv or argv[0] not in {"ruff", "mypy"}:
        return resolved
    executable = argv[0]
    for candidate in (
        worktree / ".venv" / "bin" / executable,
        Path(sys.executable).parent / executable,
    ):
        if candidate.is_file():
            resolved[0] = str(candidate)
            return resolved
    on_path = shutil.which(executable)
    if on_path is None:
        raise FileNotFoundError(f"gate executable not found: {executable}")
    resolved[0] = on_path
    return resolved
