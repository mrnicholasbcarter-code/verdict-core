"""Shared verification argv resolution for worker prompts and runtime checks."""

from __future__ import annotations

import shutil
import sys
from collections.abc import Sequence
from pathlib import Path


def classify_verify_env_error(argv: Sequence[str], code: int, out: str) -> str | None:
    """Return an ``"environment_error: <reason>"`` string when the verification
    failure is an environment problem rather than a route/implementation issue.

    Returns ``None`` for normal failures that should be classified as
    ``verification_failed`` (the route may have produced wrong output).

    Rules:
    - Exit code 126 (not executable) or 127 (not found) is always an env error.
      Note: ``subprocess_runner`` returns 127 when the binary is missing even
      before the process starts, so this also covers FileNotFoundError.
    - ``python -m MODULE`` where the interpreter prints "No module named MODULE"
      (exit 1) is an env error.  Only the exact ``-m`` target is matched to
      avoid misclassifying a ModuleNotFoundError traceback from within the test
      suite (which is a genuine wrong-result failure).
    """
    if code in (126, 127):
        return f"environment_error: exit {code}: {out[:200]}"
    # Detect missing module when -m was used: e.g. ``python -m pytest`` with
    # no pytest installed exits 1 and prints "<python>: No module named pytest".
    argv_list = list(argv)
    if "-m" in argv_list:
        m_idx = argv_list.index("-m")
        if m_idx + 1 < len(argv_list):
            module = argv_list[m_idx + 1]
            if f"No module named {module}" in out:
                return f"environment_error: No module named {module}"
    return None


def check_verify_runner(argv: Sequence[str]) -> str | None:
    """Return an ``"environment_error: <reason>"`` string when the verification
    command's runner cannot be resolved before dispatch.

    Checks only the executable (argv[0]); module presence is not checked here
    because that requires the worktree.  Returns ``None`` when the runner is
    resolvable (or the command is empty).
    """
    if not argv:
        return None
    runner = argv[0]
    # Python substitution: resolve_verify_argv replaces bare python/python3
    # with sys.executable when they are missing from PATH; treat as resolvable.
    if runner in {"python", "python3"} and (shutil.which(runner) is not None or sys.executable):
        return None
    if shutil.which(runner) is None:
        return f"environment_error: verification runner not found: {runner}"
    return None


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
