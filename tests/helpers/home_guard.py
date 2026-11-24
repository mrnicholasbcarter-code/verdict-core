"""No test can write the operator's real Verdict state (issue #1306).

Two layers, both loaded from ``tests/conftest.py``:

1. Isolation (``tests/conftest.py``): every test gets its own temp ``HOME`` and
   every state-path env var is cleared (``HOST_STATE_ENV``), so each path
   ``verdict.paths`` derives lands under that temp home.
2. Enforcement (this module): a ``sys.addaudithook`` hook, installed at session
   start, refuses any write, create, rename, delete or SQLite open that targets
   the real state roots, BEFORE the syscall runs, and records it. The test (or
   the session, for code outside a test) then fails naming the file and the
   verdict/test frames that asked for it.

Why an audit hook and not a before/after fingerprint of ``~/.verdict``: the
installed services write there every few minutes, so a fingerprint blames tests
for other processes' writes and only reports after the damage. The hook sees
only calls made by THIS pytest process, so every hit is a test leak by
construction, and the write never happens.

Scope (the contract). The guard refuses writes, creates, renames, deletes,
links and SQLite opens whose arguments NAME a protected root, a path beneath it,
or (for tree moves, links and symlinks) an ancestor of it, spelled however
(relative, ``..``, ``//``, ``file:`` URIs). It is lexical: it does not resolve
symlinks. It does not defend against a test that deliberately builds an alias
chain, nor against child processes (they inherit the isolated env), fd-relative
calls (``dir_fd``), links that already existed, or C-level I/O that bypasses the
audited entry points. For those, run the suite with the real state directories
masked by empty mounts, or under a throwaway ``HOME`` with
``tests/helpers/home_snapshot.py``. It loads from ``tests/conftest.py``, so it
does not exist in a run that skips that conftest (``--noconftest``); load it
there with ``-p tests.helpers.home_guard``. Under pytest-xdist a worker's
session-finish status does not reach the controller, so a refused write during
SESSION TEARDOWN does not fail an ``-n`` run; refused writes during a test, or
before the first test, still fail through the per-test fixture.

The real roots are resolved ONCE, when this module is imported (conftest import,
before any fixture monkeypatches the environment).

Escape hatch: there is none. A test that believes it needs the real home does
not; point ``HOME``/``VERDICT_HOME`` at ``tmp_path`` instead.
"""

from __future__ import annotations

import errno
import os
import sys
import sysconfig
import threading
import urllib.parse
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# What the isolation fixture clears (single source of truth; conftest imports it)
# ---------------------------------------------------------------------------

#: Env vars that name a state location, a discovery root or a path-valued
#: switch. An operator export of any of them would point a test at the real
#: ``~/.verdict``, ``~/.config`` or ``~/.local/share`` stores, or at the
#: operator's real harness installs, instead of the per-test home.
HOST_STATE_ENV: tuple[str, ...] = (
    # Verdict state locations (override the HOME-relative defaults).
    "VERDICT_HOME",
    "VERDICT_SESSION_EVIDENCE",
    "VERDICT_HEALTH_CACHE",
    "VERDICT_OUTCOME_SHADOW_ROOTS",
    "VERDICT_PROVE_AT_REST_STATE",
    "VERDICT_RUNTIME_STATE_DIR",
    "VERDICT_CATALOG_CACHE",
    "VERDICT_RELAY_ADMISSION_CACHE",
    "VERDICT_EVIDENCE_DB",
    "VERDICT_MEMORY_DB",
    "VERDICT_CONNECTIONS_SNAPSHOT",
    "VERDICT_MEMORY_PLANE_PATH",
    "VERDICT_OPENROUTER_FREE_SET_CACHE",
    "VERDICT_RUNS_DIR",
    "VERDICT_OPERATOR_POLICY_DIR",
    "VERDICT_GUIDANCE_PATH",
    "VERDICT_GUIDANCE_LOCAL_PATH",
    "VERDICT_CLAUDE_OUTBOX_DIR",
    "VERDICT_CODEX_OUTBOX_DIR",
    # The decision-log writers' override; under ``integration`` the LLMGATE_
    # prefix sweep in conftest is skipped, so name it here.
    "LLMGATE_LOG_PATH",
    # Workspace selectors: redirect context hydration at the operator's repos.
    "VERDICT_CONTEXT_ROOTS",
    "VERDICT_MCP_CONTEXT_ROOT",
    "VERDICT_WORKSPACE_ROOT",
    # Harness homes: redirect discovery at the operator's real installs.
    "CLAUDE_HOME",
    "CLAUDE_CONFIG_DIR",
    "CLINE_HOME",
    "CLINE_DATA_DIR",
    "HERMES_HOME",
    "OPENCODE_CONFIG",
    # XDG bases are the HOME-relative defaults' overrides.
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
    "XDG_STATE_HOME",
    "XDG_CACHE_HOME",
    # Behaviour switches the operator exports in ~/.zshrc.
    "VERDICT_DECISION_SIGNALS_MODE",
    "VERDICT_GATEWAY",
    "VERDICT_SHARED_MEMORY_URL",
    "VERDICT_SHARED_MEMORY_TOKEN",
    "VERDICT_ACTIVE_CONTROLLER_ROUTE",
)

# ---------------------------------------------------------------------------
# The real state roots, resolved once at import
# ---------------------------------------------------------------------------

#: State directories under a home directory, relative to it.
STATE_RELATIVE: tuple[str, ...] = (
    ".verdict",
    ".config/verdict",
    ".local/share/verdict",
    ".config/systemd/user",
    ".prime",
)

#: Env vars whose value (a file or directory) is itself a real state location
#: when exported. Source-tree selectors and XDG bases are deliberately absent:
#: protecting those would protect the whole workspace or ``~/.config``.
_STATE_PATH_ENV: tuple[str, ...] = (
    "VERDICT_HOME",
    "VERDICT_SESSION_EVIDENCE",
    "VERDICT_HEALTH_CACHE",
    "VERDICT_PROVE_AT_REST_STATE",
    "VERDICT_RUNTIME_STATE_DIR",
    "VERDICT_CATALOG_CACHE",
    "VERDICT_RELAY_ADMISSION_CACHE",
    "VERDICT_EVIDENCE_DB",
    "VERDICT_MEMORY_DB",
    "VERDICT_CONNECTIONS_SNAPSHOT",
    "VERDICT_MEMORY_PLANE_PATH",
    "VERDICT_OPENROUTER_FREE_SET_CACHE",
    "LLMGATE_LOG_PATH",
)


#: Verdict's own subdirectories under an exported XDG base. Never the base
#: itself: protecting ``~/.config`` would protect every other tool's config.
_XDG_STATE_SUBDIRS: dict[str, tuple[str, ...]] = {
    "XDG_CONFIG_HOME": ("verdict", "systemd/user"),
    "XDG_DATA_HOME": ("verdict",),
    "XDG_STATE_HOME": ("verdict",),
    "XDG_CACHE_HOME": ("verdict",),
}


def _home_directories(environ: Mapping[str, str] | None = None) -> list[Path]:
    homes: list[Path] = []
    source = os.environ if environ is None else environ
    env_home = (source.get("HOME") or "").strip()
    if env_home:
        homes.append(Path(env_home))
    try:
        import pwd

        homes.append(Path(pwd.getpwuid(os.getuid()).pw_dir))
    except (ImportError, KeyError, OSError):  # no passwd entry (containers)
        pass
    return homes


def real_state_roots(environ: Mapping[str, str] | None = None) -> list[str]:
    """The operator's real state locations as this process was started.

    Both ``$HOME`` and the passwd home count: a fixture that re-points ``HOME``
    does not change where a ``pwd``-based lookup lands.
    """
    source: Mapping[str, str] = dict(os.environ) if environ is None else environ
    roots: list[str] = []
    for home in _home_directories(source):
        roots.extend(str(home / rel) for rel in STATE_RELATIVE)
    for name, subdirs in _XDG_STATE_SUBDIRS.items():
        base = (source.get(name) or "").strip()
        if base:
            roots.extend(os.path.join(os.path.expanduser(base), sub) for sub in subdirs)
    for name in _STATE_PATH_ENV:
        value = (source.get(name) or "").strip()
        if value:
            roots.append(os.path.expanduser(value))
    return roots


# ---------------------------------------------------------------------------
# The guard
# ---------------------------------------------------------------------------

OUTSIDE_TEST = "<outside any test: import, collection or a wider-scope fixture>"

_WRITE_FLAGS = (
    os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_TRUNC | getattr(os, "O_EXCL", 0)
)

#: audit event -> indexes of the argument(s) that name a path the call changes.
_MUTATED_ARGS: dict[str, tuple[int, ...]] = {
    "os.rename": (0, 1),
    "os.remove": (0,),
    "os.rmdir": (0,),
    "os.mkdir": (0,),
    "os.truncate": (0,),
    # A link INTO protected state is refused too: writing through the alias
    # (a hard link shares the inode; a symlink resolves to it) would name a
    # path outside the roots.
    "os.symlink": (0, 1),  # resolved specially in ``_write_target``
    "os.link": (0, 1),
    "os.chmod": (0,),
    "os.chown": (0,),
    "os.utime": (0,),
    "shutil.copyfile": (1,),
    "shutil.copymode": (1,),
    "shutil.copystat": (1,),
    "shutil.copytree": (1,),
    "shutil.move": (0, 1),
    "shutil.rmtree": (0,),
    "sqlite3.connect": (0,),
}

#: Events that move or delete a whole tree: either end may be an ANCESTOR of a
#: protected root. A SOURCE ancestor relocates the protected tree
#: (``os.rename(home / ".config", ...)``, ``shutil.rmtree(home)``); a DESTINATION
#: ancestor populates one (``os.rename(prepared_tree, home / ".config")`` creates
#: ``~/.config/verdict``). Neither argument is then equal to or beneath the root.
#: Only these events get the ancestor check, so an unrelated rename elsewhere
#: under the home stays allowed.
_TREE_ENDS: dict[str, tuple[int, ...]] = {
    "os.rename": (0, 1),
    "shutil.move": (0, 1),
    "shutil.rmtree": (0,),
}

_STDLIB_DIRS = tuple(
    {p for p in (sysconfig.get_paths().get("stdlib"), sysconfig.get_paths().get("platstdlib")) if p}
)


@dataclass(frozen=True)
class Violation:
    phase: str
    event: str
    path: str
    where: tuple[str, ...]

    def render(self) -> str:
        frames = " <- ".join(self.where) if self.where else "(no non-stdlib frame)"
        return f"  [{self.phase}] {self.event} {self.path}\n      requested by: {frames}"


def _as_path(value: object) -> str | None:
    """A str path from an audit argument; None for fds, None and other types."""
    if isinstance(value, (str, bytes, os.PathLike)):
        try:
            return os.fsdecode(os.fspath(value))
        except (TypeError, ValueError):
            return None
    return None


def _sqlite_file_targets(value: str) -> list[str]:
    """Filesystem targets of a ``sqlite3.connect`` argument.

    The audit event carries the raw string, and the caller may have passed
    ``uri=True``, so a ``file:`` string is also decoded (``file:/p?mode=rwc``,
    ``file:///p``, percent escapes). ``mode=ro`` and ``mode=memory`` URIs touch no file, so only
    their decoded target is skipped. The raw string is always checked too:
    without ``uri=True`` it is just a (writable) path.
    """
    targets = [value]
    if value.startswith("file:"):
        split = urllib.parse.urlsplit(value)
        if {"ro", "memory"} & set(urllib.parse.parse_qs(split.query).get("mode", [])):
            # Read-only or in-memory as a URI, but without ``uri=True`` this is
            # a writable literal filename: the decoded target is skipped, the
            # raw one kept.
            return targets
        if split.netloc in ("", "localhost"):
            decoded = urllib.parse.unquote(split.path)
            if decoded:
                targets.append(decoded)
    return targets


def _normalise(path: str) -> str:
    """Absolute, ``..``-free, with one leading slash (``//p`` is ``/p`` on Linux)."""
    if not path.startswith("/"):
        path = os.path.join(os.getcwd(), path)
    if "//" in path or "/." in path:
        path = os.path.normpath(path)
        if path.startswith("//"):  # normpath keeps exactly two leading slashes
            path = "/" + path.lstrip("/")
    return path


def _callers(limit: int = 4) -> tuple[str, ...]:
    """The first few non-stdlib frames above the audited call."""
    found: list[str] = []
    frame: Any = sys._getframe(3)
    while frame is not None and len(found) < limit:
        name = frame.f_code.co_filename
        if (
            not name.startswith("<")
            and not name.endswith("home_guard.py")
            and not any(name.startswith(d) for d in _STDLIB_DIRS)
        ):
            found.append(f"{Path(name).name}:{frame.f_lineno} in {frame.f_code.co_name}")
        frame = frame.f_back
    return tuple(found)


def _symlink_ends(args: tuple[Any, ...]) -> tuple[str | None, str | None]:
    """(link path, resolved target) of an ``os.symlink`` event.

    A relative target resolves against the link's directory, not the cwd.
    """
    target = _as_path(args[0]) if args else None
    link = _as_path(args[1]) if len(args) > 1 else None
    if target and link and not target.startswith("/"):
        target = os.path.join(os.path.dirname(_normalise(link)), target)
    return link, target


class HomeWriteGuard:
    """Refuses and records writes under ``roots``.

    ``check`` is the audit-hook body; it is pure with respect to the filesystem
    (no reads, no writes), so it cannot recurse into itself.
    """

    def __init__(self, roots: Iterable[str | os.PathLike[str]]) -> None:
        self._roots: list[str] = []
        for root in roots:
            self.add_root(root)
        self.violations: list[Violation] = []
        self.phase: str = OUTSIDE_TEST
        self._busy = threading.local()

    @property
    def roots(self) -> tuple[str, ...]:
        return tuple(self._roots)

    @staticmethod
    def _forms(root: str | os.PathLike[str]) -> list[str]:
        raw = _normalise(os.fsdecode(os.fspath(root))).rstrip("/")
        return [raw, os.path.realpath(raw)] if raw else []

    def add_root(self, root: str | os.PathLike[str]) -> None:
        for form in self._forms(root):
            if form not in self._roots:
                self._roots.append(form)

    def remove_root(self, root: str | os.PathLike[str]) -> None:
        for form in self._forms(root):
            if form in self._roots:
                self._roots.remove(form)

    @contextmanager
    def protect(self, root: str | os.PathLike[str]) -> Iterator[None]:
        """Treat ``root`` as a real state root for the duration (guard tests)."""
        self.add_root(root)
        try:
            yield
        finally:
            self.remove_root(root)

    def protected_root(self, path: str) -> str | None:
        path = _normalise(path)
        for root in self._roots:
            if path == root or path.startswith(root + "/"):
                return root
        return None

    def contained_root(self, path: str) -> str | None:
        """A protected root strictly beneath ``path`` (``path`` is its ancestor)."""
        prefix = _normalise(path).rstrip("/") + "/"
        for root in self._roots:
            if root.startswith(prefix):
                return root
        return None

    def _write_target(self, event: str, args: tuple[Any, ...]) -> list[str]:
        if event == "os.symlink":
            return [p for p in _symlink_ends(args) if p]
        if event == "open":
            flags = args[2] if len(args) > 2 and isinstance(args[2], int) else 0
            mode = args[1] if len(args) > 1 and isinstance(args[1], str) else ""
            if not (flags & _WRITE_FLAGS) and not any(c in mode for c in "wax+"):
                return []
            indexes: tuple[int, ...] = (0,)
        else:
            indexes = _MUTATED_ARGS.get(event, ())
        paths = [p for p in (_as_path(args[i]) for i in indexes if i < len(args)) if p]
        if event == "sqlite3.connect":
            return [target for p in paths for target in _sqlite_file_targets(p)]
        return paths

    def check(self, event: str, args: tuple[Any, ...]) -> None:
        if event != "open" and event not in _MUTATED_ARGS:
            return
        if getattr(self._busy, "on", False):
            return
        self._busy.on = True
        try:
            for path in self._write_target(event, args):
                if self.protected_root(path):
                    self._deny(event, path)
            for index in _TREE_ENDS.get(event, ()):
                source = _as_path(args[index]) if index < len(args) else None
                if source and self.contained_root(source):
                    self._deny(event, source)
            if event == "os.symlink":
                # A new alias to an ANCESTOR of a root reaches the root through it.
                target = _symlink_ends(args)[1]
                if target and self.contained_root(target):
                    self._deny(event, target)
        finally:
            self._busy.on = False

    def _deny(self, event: str, path: str) -> None:
        self.violations.append(Violation(self.phase, event, path, _callers()))
        raise PermissionError(
            errno.EACCES,
            "tests must not write the operator's real Verdict state "
            "(isolated HOME/VERDICT_HOME did not cover this path)",
            path,
        )

    def drain(self) -> list[Violation]:
        found, self.violations = self.violations, []
        return found


#: Resolved at import time: before any fixture touches the environment.
REAL_HOMES: tuple[str, ...] = tuple(str(h) for h in _home_directories())
GUARD = HomeWriteGuard(real_state_roots())
_hook_installed = False


def install(guard: HomeWriteGuard = GUARD) -> None:
    """Install the audit hook once per interpreter (hooks cannot be removed)."""
    global _hook_installed
    if _hook_installed:
        return
    _hook_installed = True
    check: Callable[[str, tuple[Any, ...]], None] = guard.check
    sys.addaudithook(check)


def render_violations(found: list[Violation]) -> str:
    lines = "\n".join(v.render() for v in found)
    return (
        f"{len(found)} write(s) to the operator's real Verdict state were refused "
        "(nothing was written). A test must use tmp_path / the per-test HOME, "
        "never a path that resolves to the real home:\n"
        f"{lines}\n"
        f"protected roots: {', '.join(GUARD.roots)}"
    )


# ---------------------------------------------------------------------------
# pytest wiring (registered by ``pytest_configure``; conftest imports it)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _real_home_write_guard(request: pytest.FixtureRequest) -> Iterator[None]:
    """Fail the test, by name, if anything it ran tried to write real state."""
    GUARD.phase = request.node.nodeid
    yield
    GUARD.phase = OUTSIDE_TEST
    found = GUARD.drain()
    if found:
        pytest.fail(render_violations(found), pytrace=False)


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Fail the run for refused writes made outside any test (import time)."""
    found = GUARD.drain()
    if not found:
        return
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    message = render_violations(found)
    if reporter is not None:
        reporter.write_line(message, red=True)
    else:
        print(message, file=sys.stderr)
    session.exitstatus = int(pytest.ExitCode.TESTS_FAILED)


def pytest_configure(config: pytest.Config) -> None:
    install()
    plugins = config.pluginmanager
    module = sys.modules[__name__]
    # Already registered when loaded with ``-p tests.helpers.home_guard``.
    if not plugins.is_registered(module):
        plugins.register(module, "verdict-home-write-guard")
