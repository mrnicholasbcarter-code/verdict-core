"""Shared verification argv resolution for worker prompts and runtime checks."""

from __future__ import annotations

import os
import re
import shlex
import shutil
import sys
from collections.abc import Sequence
from pathlib import Path, PurePosixPath

from verdict.orchestration.contracts import path_is_owned
from verdict.orchestration.repo_gates import _load_pyproject

# The runner keeps only the last VERIFY_OUTPUT_TAIL_CHARS characters of a verification
# command's output, so a long list of collection errors can lose its first entries.
VERIFY_OUTPUT_TAIL_CHARS = 4000
# pytest stops with exit 2 and this line when a test file cannot be imported or collected.
_COLLECTION_INTERRUPTED_RE = re.compile(r"\b(\d+) errors? during collection\b")
# A collection error appears as ``ERROR <path>[ - <reason>]`` in the short summary and as
# ``____ ERROR collecting <path> ____`` above it. Only plain repo-path characters are read
# as a path, so no output text beyond a file path reaches the reason.
_PATH_CHARS = r"[\w./+@-]+"
_SUMMARY_ERROR_LINE_RE = re.compile(r"^ERROR .*$", re.MULTILINE)
_SUMMARY_ERROR_RE = re.compile(rf"^ERROR ({_PATH_CHARS})(?:::\S*)?(?: - .*)?$")
_HEADER_ERROR_LINE_RE = re.compile(r"^_+ ERROR collecting .* _+$", re.MULTILINE)
_HEADER_ERROR_RE = re.compile(rf"^_+ ERROR collecting ({_PATH_CHARS}) _+$")
_REASON_PATHS_NAMED = 3
# mypy prints one line per unresolved import: ``<file>:<line>: error: <message>  [<code>]``.
# The module is the first quoted name in the message, for all three wordings mypy uses:
# ``Cannot find implementation or library stub for module named "x"``,
# ``Library stubs not installed for "x"`` and ``Skipping analyzing "x": module is installed, but
# missing library stubs or py.typed marker``.
_UNRESOLVED_IMPORT_RE = re.compile(
    r"^[^\n]*: error: (?P<message>[^\n]*?)  \[(?:import-not-found|import-untyped)\]$", re.MULTILINE
)
_QUOTED_MODULE_RE = re.compile(r'"([^"\n]+)"')


def _runs_pytest(argv: Sequence[str]) -> bool:
    """True when argv is ``pytest ...``, ``py.test ...`` or ``<python> -m pytest ...``."""
    tokens = list(argv)
    if any(os.path.basename(token) in {"pytest", "py.test"} for token in tokens):
        return True
    return any(
        token == "-m" and index + 1 < len(tokens) and tokens[index + 1] == "pytest"
        for index, token in enumerate(tokens)
    )


def _runs_mypy(argv: Sequence[str]) -> bool:
    """True when argv is ``mypy ...`` or ``<python> -m mypy ...``."""
    tokens = list(argv)
    if any(os.path.basename(token) == "mypy" for token in tokens):
        return True
    return any(
        token == "-m" and index + 1 < len(tokens) and tokens[index + 1] == "mypy"
        for index, token in enumerate(tokens)
    )


def _is_repo_relative(path: str) -> bool:
    """True for a path that ``path_is_owned`` can judge: not absolute and without ``..``."""
    parsed = PurePosixPath(path)
    return not parsed.is_absolute() and ".." not in parsed.parts


def _erroring_paths(out: str, expected_errors: int) -> list[str] | None:
    """The distinct files pytest reports as failing to collect, or ``None`` when that list
    cannot be trusted to be complete.

    ``None`` when an ``ERROR`` line holds anything but a plain path (for example a path
    with a space), when a path is not repo-relative (absolute, or with a ``..`` part: with no
    repository root to normalize against it cannot be matched to an owned file, so it is not
    known to be unowned), and when the output hit the runner's length cap and shows fewer
    paths than the ``expected_errors`` pytest counted, because the missing ones may be owned.
    """
    erroring: list[str] = []
    for line_re, path_re in (
        (_SUMMARY_ERROR_LINE_RE, _SUMMARY_ERROR_RE),
        (_HEADER_ERROR_LINE_RE, _HEADER_ERROR_RE),
    ):
        for line in line_re.findall(out):
            match = path_re.match(line)
            if match is None or not _is_repo_relative(match.group(1)):
                return None
            if match.group(1) not in erroring:
                erroring.append(match.group(1))
    if len(out) >= VERIFY_OUTPUT_TAIL_CHARS and len(erroring) < expected_errors:
        return None
    return erroring


def _unowned_collection_errors(
    argv: Sequence[str], code: int, out: str, owned_files: Sequence[str]
) -> str | None:
    """The reason when pytest stopped on collection errors and the node owns none of them.

    ``None`` when this is not that case: another exit code, no collection marker, no
    erroring path to read, an erroring list that may be incomplete, or at least one
    erroring path the node owns (its own broken test or import is a wrong implementation,
    not an environment problem).
    """
    if code != 2 or not _runs_pytest(argv):
        return None
    marker = _COLLECTION_INTERRUPTED_RE.search(out)
    if marker is None:
        return None
    erroring = _erroring_paths(out, int(marker.group(1)))
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
      the output, is not repo-relative (absolute or with ``..``) or may have been cut
      from it, exit 4 (a named file is missing), exit 5 (no tests) and ordinary test
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


def _first_party_roots(worktree: Path) -> list[Path]:
    """The directories mypy finds the project's own modules in: the worktree root, ``src/``, the
    ``[tool.mypy] mypy_path`` entries of its ``pyproject.toml`` (a list, or one string separated
    by ``:`` or ``,``; ``$MYPY_CONFIG_FILE_DIR`` is the worktree) and the ``MYPYPATH``
    environment variable (``:``-separated, relative to the worktree the gate runs in)."""
    entries: list[str] = []
    try:
        data = _load_pyproject(worktree)
    except (ValueError, OSError):
        data = {}
    tool = data.get("tool")
    mypy = tool.get("mypy") if isinstance(tool, dict) else None
    configured = mypy.get("mypy_path") if isinstance(mypy, dict) else None
    if isinstance(configured, str):
        entries.extend(part for chunk in configured.split(os.pathsep) for part in chunk.split(","))
    elif isinstance(configured, list):
        entries.extend(item for item in configured if isinstance(item, str))
    entries.extend(os.environ.get("MYPYPATH", "").split(os.pathsep))
    roots = [worktree, worktree / "src"]
    for entry in entries:
        entry = entry.strip().replace("$MYPY_CONFIG_FILE_DIR", str(worktree))
        if entry:
            roots.append(worktree / entry)  # an absolute entry stays absolute
    return roots


def _is_first_party(module: str, worktree: Path) -> bool:
    """True when ``module``'s top-level name is a package or module of the project itself, in
    one of the places mypy looks for them (:func:`_first_party_roots`): such an import is the
    implementation's to keep resolvable, never the environment's."""
    top = module.split(".", 1)[0]
    return any(
        (base / top).is_dir() or (base / f"{top}.py").is_file()
        for base in _first_party_roots(worktree)
    )


def classify_gate_env_error(argv: Sequence[str], code: int, out: str, worktree: Path) -> str | None:
    """Return an ``"environment_error: <reason>"`` string when a failed repo gate failed
    because its environment lacks the project's dependencies, not because of the code.

    The one signal read is mypy's own: at least one ``import-not-found`` or ``import-untyped``
    error, every one of them for a third-party module (the name is not a package or module of the
    project: at the worktree root, under ``src/`` or in a ``mypy_path``).  mypy's other errors in
    the same output are not told apart: the observed output mixes the import errors with others
    (``untyped-decorator``, ``no-any-return``) that pass once the libraries are installed, so a
    genuine type error beside them shows once the environment is fixed.  ``None`` for a passing
    gate, any other gate, output without such an error, and output with an unresolved import of
    the project's own modules (a renamed or deleted module is a wrong implementation).  Only the
    output the runner kept is read; a first-party error in a cut-off part is not seen.

    The reason names the first three packages only: no other output text reaches it.
    """
    if code == 0 or not _runs_mypy(argv):
        return None
    modules: list[str] = []
    for match in _UNRESOLVED_IMPORT_RE.finditer(out):
        quoted = _QUOTED_MODULE_RE.search(match.group("message"))
        if quoted is None:
            return None  # an import error whose module cannot be read is not trusted
        module = quoted.group(1)
        if _is_first_party(module, worktree):
            return None
        package = module.split(".", 1)[0]
        if package not in modules:
            modules.append(package)
    if not modules:
        return None
    named = ", ".join(modules[:_REASON_PATHS_NAMED])
    more = len(modules) - _REASON_PATHS_NAMED
    if more > 0:
        named += f" and {more} more"
    return (
        f"environment_error: `{shlex.join(argv)}` cannot resolve third-party imports ({named}); "
        "the gate ran without the project's dependencies: fix the environment, "
        "not the implementation"
    )


def _declares_installable_extras(worktree: Path) -> bool:
    """True when the worktree's ``pyproject.toml`` declares optional dependencies that
    ``uv run --all-extras`` can install together.

    False (so the command stays as it is today) with no extras, when ``[tool.uv] conflicts``
    names extras that cannot be installed together (``--all-extras`` would make uv refuse),
    and when the file cannot be read: this decides a flag, never a failure.
    """
    try:
        data = _load_pyproject(worktree)
    except (ValueError, OSError):
        return False
    project = data.get("project")
    extras = project.get("optional-dependencies") if isinstance(project, dict) else None
    if not isinstance(extras, dict) or not extras:
        return False
    tool = data.get("tool")
    uv_table = tool.get("uv") if isinstance(tool, dict) else None
    return not (isinstance(uv_table, dict) and uv_table.get("conflicts"))


def _venv_made_by_uv(venv: Path) -> bool:
    """True when ``venv`` was created by uv: uv writes ``uv = <version>`` into the
    ``pyvenv.cfg`` of every environment it makes (``uv run``, ``uv sync``, ``uv venv``)."""
    try:
        text = (venv / "pyvenv.cfg").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return any(line.partition("=")[0].strip() == "uv" for line in text.splitlines())


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
    1. ``<worktree>/.venv/bin/<tool>`` — explicit virtualenv in the worktree (except an
       environment uv made, for ``mypy`` in a project with extras: see rule 2).
    2. ``uv run --frozen <tool>`` — when ``uv.lock`` is present.  ``uv`` must
       be on PATH; if it is not, the gate cannot run and ``FileNotFoundError``
       is raised (a named environment failure, never a silent pass).  ``mypy`` also gets
       ``--all-extras`` when the project declares optional dependencies (see below): without
       them uv builds an environment of the base dependencies only, and mypy reports every
       typed library of an extra (stubs, ``prompt_toolkit``, ...) as missing.  ``ruff`` reads
       source and imports nothing, so its command is unchanged.
    3. ``Path(sys.executable).parent/<tool>`` — Verdict's own interpreter env.
    4. ``shutil.which(<tool>)`` — OS PATH as a last resort.
    5. ``FileNotFoundError`` — gate cannot run; callers must fail closed.

    Other executable names (including npm) are returned unchanged.
    """
    resolved = list(argv)
    if not argv or argv[0] not in {"ruff", "mypy"}:
        return resolved
    executable = argv[0]

    # mypy needs the project's optional dependencies; an environment uv made without them
    # (the ``uv run --frozen ruff`` of an earlier gate in this worktree builds one) has the
    # tool but not the extras, so it is completed through ``uv run`` below, not used as it is.
    needs_extras = (
        executable == "mypy"
        and (worktree / "uv.lock").is_file()
        and _declares_installable_extras(worktree)
    )

    # 1. Explicit worktree venv takes priority over everything.  A venv that was not made by
    #    uv is the operator's own and is never touched; one uv made is the very environment
    #    ``uv run`` uses, so rule 2 below reaches it and installs what it lacks.
    venv_candidate = worktree / ".venv" / "bin" / executable
    if venv_candidate.is_file() and not (
        needs_extras and _venv_made_by_uv(worktree / ".venv") and shutil.which("uv")
    ):
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
        # ``--all-extras``: the lock already resolves every extra, so this installs and
        # never re-resolves.  Kept off when the project declares no extras (the command
        # stays as it was) or declares conflicting ones (uv would refuse the flag).
        extras = ["--all-extras"] if needs_extras else []
        return [uv, "run", "--frozen", *extras, *argv]

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
