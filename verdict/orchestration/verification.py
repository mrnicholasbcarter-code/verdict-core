"""Shared verification argv resolution for worker prompts and runtime checks."""

from __future__ import annotations

import os
import re
import shlex
import shutil
import sys
from collections.abc import Sequence
from pathlib import Path

from verdict.orchestration.contracts import path_is_owned

# pytest stops with exit 2 and this line when a test file cannot be imported or collected.
_COLLECTION_INTERRUPTED_RE = re.compile(r"\b\d+ errors? during collection\b")
# A collection error appears as ``ERROR <path>[ - <reason>]`` in the short summary and as
# ``____ ERROR collecting <path> ____`` above it. Only plain repo-path characters are kept,
# so no output text beyond a file path reaches the reason.
_PATH_CHARS = r"[\w./+@-]+"
_COLLECTION_ERROR_RES = (
    re.compile(rf"^ERROR ({_PATH_CHARS})(?:::\S*)?(?: - .*)?$", re.MULTILINE),
    re.compile(rf"^_+ ERROR collecting ({_PATH_CHARS}) _+$", re.MULTILINE),
)
_REASON_PATHS_NAMED = 3


def _runs_pytest(argv: Sequence[str]) -> bool:
    """True when argv is ``pytest ...``, ``py.test ...`` or ``<python> -m pytest ...``."""
    tokens = list(argv)
    if any(os.path.basename(token) in {"pytest", "py.test"} for token in tokens):
        return True
    return any(
        token == "-m" and index + 1 < len(tokens) and tokens[index + 1] == "pytest"
        for index, token in enumerate(tokens)
    )


def _unowned_collection_errors(
    argv: Sequence[str], code: int, out: str, owned_files: Sequence[str]
) -> str | None:
    """The reason when pytest stopped on collection errors and the node owns none of them.

    ``None`` when this is not that case: another exit code, no collection marker, no
    erroring path to read, or at least one erroring path the node owns (its own broken test
    or import is a wrong implementation, not an environment problem).
    """
    if code != 2 or not _runs_pytest(argv) or not _COLLECTION_INTERRUPTED_RE.search(out):
        return None
    erroring: list[str] = []
    for pattern in _COLLECTION_ERROR_RES:
        for match in pattern.finditer(out):
            path = match.group(1)
            if path not in erroring:
                erroring.append(path)
    if not erroring or any(path_is_owned(owned_files, path) for path in erroring):
        return None
    named = ", ".join(erroring[:_REASON_PATHS_NAMED])
    more = len(erroring) - _REASON_PATHS_NAMED
    if more > 0:
        named += f" and {more} more"
    return (
        f"environment_error: `{shlex.join(argv)}` stopped on collection errors in files "
        f"this node does not own (first: {named}); fix the verification command or the "
        "environment, not the implementation"
    )


def classify_verify_env_error(
    argv: Sequence[str], code: int, out: str, owned_files: Sequence[str] | None = None
) -> str | None:
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
    - With ``owned_files`` (the node's own files; ``()`` for a node that owns none):
      a pytest run that exits 2 on collection errors, every one in a file the node
      does not own, is an env error that names the command and the first erroring paths.
      A collection error in an owned file, an erroring path that cannot be read from
      the output, exit 4 (a named file is missing), exit 5 (no tests) and ordinary test
      failures stay ``verification_failed``. Without ``owned_files`` this rule is off.
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
    if owned_files is not None:
        return _unowned_collection_errors(argv_list, code, out, owned_files)
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
    """Resolve bare ruff/mypy to the worktree's own environment.

    Resolution order:
    1. ``<worktree>/.venv/bin/<tool>`` — explicit virtualenv in the worktree.
    2. ``uv run --frozen <tool>`` — when ``uv.lock`` is present.  ``uv`` must
       be on PATH; if it is not, the gate cannot run and ``FileNotFoundError``
       is raised (a named environment failure, never a silent pass).
    3. ``Path(sys.executable).parent/<tool>`` — Verdict's own interpreter env.
    4. ``shutil.which(<tool>)`` — OS PATH as a last resort.
    5. ``FileNotFoundError`` — gate cannot run; callers must fail closed.

    Other executable names (including npm) are returned unchanged.
    """
    resolved = list(argv)
    if not argv or argv[0] not in {"ruff", "mypy"}:
        return resolved
    executable = argv[0]

    # 1. Explicit worktree venv takes priority over everything.
    venv_candidate = worktree / ".venv" / "bin" / executable
    if venv_candidate.is_file():
        resolved[0] = str(venv_candidate)
        return resolved

    # 2. uv-managed repo: uv.lock is the authoritative signal.  Running through
    #    ``uv run`` guarantees the project's locked environment is used, not a
    #    global tool that may lack the project's type stubs or plugins.
    if (worktree / "uv.lock").is_file():
        uv = shutil.which("uv")
        if uv is None:
            raise FileNotFoundError(
                f"gate executable {executable!r}: uv.lock is present but uv is not on PATH"
                " — environment_error: uv not found"
            )
        return [uv, "run", "--frozen", *argv]

    # 3. Fall back to Verdict's own interpreter environment (non-uv-managed repos).
    verdict_candidate = Path(sys.executable).parent / executable
    if verdict_candidate.is_file():
        resolved[0] = str(verdict_candidate)
        return resolved

    # 4. Last resort: OS PATH.
    on_path = shutil.which(executable)
    if on_path is None:
        raise FileNotFoundError(f"gate executable not found: {executable}")
    resolved[0] = on_path
    return resolved
