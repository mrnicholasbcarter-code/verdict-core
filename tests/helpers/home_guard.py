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
chain, nor against child processes (they inherit the isolated env), links that
already existed, or C-level I/O that bypasses the audited entry points. For
those, run the suite with the real state directories masked by empty mounts, or
under a throwaway ``HOME`` with ``tests/helpers/home_snapshot.py``.

A name is read against the directory it is relative to. A relative name given
with a ``dir_fd`` (``os.mkdir``, ``os.remove``, ``os.rename``, ``shutil.rmtree``
and the like) is read against the directory the descriptor names
(``/proc/self/fd``, Linux), never the working directory; where that cannot be
read it is not checked. ``os.open(..., dir_fd=)`` is the exception: its audit
event does not carry the descriptor, so its relative name is read against the
working directory. A ``sqlite3.connect`` argument that names no file (``:memory:``
and the empty private temporary database) is never a write target. The audit event
does not say ``uri=``, so a ``file:`` string is read both ways: as a URI (decoded;
a last ``mode`` of ``ro`` or ``memory`` touches no file) and as a literal filename
in the working directory, which SQLite does create.

The guard loads from ``tests/conftest.py``, so it does not exist in a run that
skips that conftest (``--noconftest``); load it there with
``-p tests.helpers.home_guard``. Under pytest-xdist a worker's own
exit status never reaches the controller, so each worker hands its refused
writes to the controller through ``workeroutput`` when it finishes, and the
controller fails the run for them (a refused write during session teardown, or
after the last per-test drain). A worker that finishes without reporting, or
whose report cannot be read, also fails the run: unknown is never clean.

The real roots are resolved ONCE, when this module is imported (conftest import,
before any fixture monkeypatches the environment). They are the home-relative
state directories, Verdict's own part of each exported XDG base, and every
``HOST_STATE_ENV`` variable whose exported value names a file or directory, so a
path captured before the isolation fixtures cleared the variable (an outbox
directory outside the home roots, a runs directory) stays refused. Variables
whose value is not a location (URLs, switches, a binary name, ``:memory:``) are
skipped by the explicit ``NOT_PROTECTED_ENV`` table and the value rules in
``_exported_locations``. A location that would cover the system temp dir, this
repository or the working directory (``/``, ``/tmp``, ``.``) cannot be protected
without refusing every ordinary write, so the run stops at start with a usage
error naming the variable (``STARTUP_PROBLEMS``) instead of dropping it.

Escape hatch: there is none. A test that believes it needs the real home does
not; point ``HOME``/``VERDICT_HOME`` at ``tmp_path`` instead.
"""

from __future__ import annotations

import errno
import os
import re
import sys
import sysconfig
import tempfile
import threading
import urllib.parse
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager, suppress
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
#:
#: Membership is also what protects the exported location from writes: the guard
#: reads every name here, except those in ``NOT_PROTECTED_ENV`` and the XDG bases
#: (``_XDG_STATE_SUBDIRS``), as a file or directory that must never be written
#: (``protected_state_env``). Add a name here and its export is protected; add it
#: to ``NOT_PROTECTED_ENV`` only if its value is not a location, with the reason.
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
    # The OpenCode executor copies this through its child-env allowlist before it
    # overrides it with the per-attempt config directory; an operator export must
    # never be what a test sees.
    "OPENCODE_CONFIG_DIR",
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
    # Selectors of the operator's live installs (not state): an export would point a
    # test at the real opencode binary or the live ``verdict serve``.
    "VERDICT_OPENCODE_BIN",
    "VERDICT_SERVE_URL",
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

#: ``HOST_STATE_ENV`` members the guard does NOT protect as a location, each with
#: the reason. Every other member is protected from its exported value.
NOT_PROTECTED_ENV: dict[str, str] = {
    "VERDICT_RELAY_ADMISSION_CACHE": "a boolean switch (true/false), not a location",
    "VERDICT_DECISION_SIGNALS_MODE": "a mode name (OFF/SHADOW/...), not a location",
    "VERDICT_GATEWAY": "a URL",
    "VERDICT_SHARED_MEMORY_URL": "a URL",
    "VERDICT_SERVE_URL": "a URL",
    "VERDICT_SHARED_MEMORY_TOKEN": "a secret, not a location",
    "VERDICT_ACTIVE_CONTROLLER_ROUTE": "a route name, not a location",
    "VERDICT_OPENCODE_BIN": "a binary name resolved on PATH (or an executable tests run, not state)",
    "VERDICT_CONTEXT_ROOTS": "comma-separated directory names relative to the workspace",
    "VERDICT_MCP_CONTEXT_ROOT": (
        "a read-only source-tree selector; protecting it would protect the operator's checkout"
    ),
    "VERDICT_WORKSPACE_ROOT": (
        "a read-only source-tree selector; protecting it would protect the operator's checkout"
    ),
}

#: ``HOST_STATE_ENV`` members that hold several locations, separated by ``os.pathsep``.
_PATH_LIST_ENV: frozenset[str] = frozenset({"VERDICT_OUTCOME_SHADOW_ROOTS"})

#: ``verdict.outcome_shadow`` appends its sink beside the FIRST shadow root, which is
#: a write to a path the exported value does not name (pinned by
#: ``tests/test_home_isolation.py::test_the_shadow_sink_name_matches_what_verdict_writes``).
OUTCOME_SHADOW_SINK = "outcome_shadow.jsonl"

_URL = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")

#: This checkout: tests write caches and bytecode here, so no root may cover it.
_REPO_ROOT = Path(__file__).resolve().parents[2]


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


def protected_state_env() -> tuple[str, ...]:
    """The ``HOST_STATE_ENV`` names whose exported value is protected as a location."""
    return tuple(
        name
        for name in HOST_STATE_ENV
        if name not in NOT_PROTECTED_ENV and name not in _XDG_STATE_SUBDIRS
    )


def _exported_locations(name: str, value: str) -> list[str]:
    """The locations an exported value names; empty when it names none.

    Not a location: nothing, ``:memory:`` (SQLite) or a URL. A relative value is
    resolved against the working directory when the guard is built, as the code
    that reads it would at that moment.
    """
    value = value.strip()
    if not value or value == ":memory:" or _URL.match(value):
        return []
    parts = [value]
    if name in _PATH_LIST_ENV:
        parts = [chunk.strip() for chunk in value.split(os.pathsep)]
    found = [os.path.expanduser(part) for part in parts if part]
    if name == "VERDICT_OUTCOME_SHADOW_ROOTS" and found:
        first = _normalise(found[0]).rstrip("/")
        beside = {os.path.dirname(first), os.path.dirname(os.path.realpath(first))}
        found.extend(sorted(os.path.join(base, OUTCOME_SHADOW_SINK) for base in beside))
    return found


def _state_root_sources(environ: Mapping[str, str] | None = None) -> list[tuple[str, str]]:
    """(label, root) for every real state location: the label names where it came from."""
    source: Mapping[str, str] = dict(os.environ) if environ is None else environ
    found: list[tuple[str, str]] = []
    for home in _home_directories(source):
        found.extend((f"HOME={home}", str(home / rel)) for rel in STATE_RELATIVE)
    for name, subdirs in _XDG_STATE_SUBDIRS.items():
        base = (source.get(name) or "").strip()
        if base:
            found.extend(
                (f"{name}={base}", os.path.join(os.path.expanduser(base), sub)) for sub in subdirs
            )
    for name in protected_state_env():
        found.extend(
            (f"{name}={source.get(name, '')}", root)
            for root in _exported_locations(name, source.get(name) or "")
        )
    return found


def real_state_roots(environ: Mapping[str, str] | None = None) -> list[str]:
    """The operator's real state locations as this process was started.

    Both ``$HOME`` and the passwd home count: a fixture that re-points ``HOME``
    does not change where a ``pwd``-based lookup lands. Every exported
    ``protected_state_env`` location counts too, wherever it points.
    """
    return [root for _, root in _state_root_sources(environ)]


def _write_locations_of_this_run(source: Mapping[str, str]) -> list[str]:
    """Places a test run must always be able to write: temp dirs, the repo, the cwd.

    Built without ``tempfile.gettempdir()``, which caches its answer before the
    fixtures have run.
    """
    temp = [source.get(key) or "" for key in ("TMPDIR", "TEMP", "TMP")]
    places = ["/tmp", "/var/tmp", tempfile.tempdir or "", *temp, str(_REPO_ROOT)]
    with suppress(OSError):  # a deleted cwd is not a place to write
        places.append(os.getcwd())
    return [form for place in places if place for form in HomeWriteGuard._forms(place)]


def unsafe_state_roots(environ: Mapping[str, str] | None = None) -> list[str]:
    """One line per protected root that would cover a place the run must write.

    A root equal to, or an ancestor of, the temp dir, the repository or the working
    directory (``/``, ``/tmp``, ``.``) would turn every ordinary write into a
    "real state" violation, so it is a usage error at start, never silently skipped.
    """
    source: Mapping[str, str] = dict(os.environ) if environ is None else environ
    places = _write_locations_of_this_run(source)
    problems: list[str] = []
    for label, root in _state_root_sources(source):
        forms = HomeWriteGuard._forms(root)
        covers = not forms or any(p == f or p.startswith(f + "/") for f in forms for p in places)
        if covers:  # no forms: the root is "/"
            problems.append(
                f"{label}: {root or '/'} is, or contains, the system temp dir, this repository "
                "or the working directory"
            )
    return problems


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

#: audit event -> {index of a path argument: index of the ``dir_fd`` it is relative to}.
#: The event carries ``-1`` (or ``None``) for a ``dir_fd`` that was not given.
#: ``open`` is absent: its audit event has no descriptor argument.
_DIR_FD_ARGS: dict[str, dict[int, int]] = {
    "os.rename": {0: 2, 1: 3},
    "os.link": {0: 2, 1: 3},
    "os.symlink": {1: 2},  # the link; its target is relative to the link's directory
    "os.remove": {0: 1},
    "os.rmdir": {0: 1},
    "os.mkdir": {0: 2},
    "os.chmod": {0: 2},
    "os.chown": {0: 3},
    "os.utime": {0: 3},
    "shutil.rmtree": {0: 1},
}

#: Names SQLite opens without touching a file, whatever ``uri=`` says: ``:memory:``
#: and the empty name (a private temporary database, deleted on close).
_SQLITE_NO_FILE_NAMES = frozenset({"", ":memory:"})

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

    ``:memory:`` and the empty name open no file under any setting, so they have
    no target; every other name that does not start with ``file:`` is a path.
    Only those two exact strings are exempt: ``./:memory:``, ``x:memory:`` and
    ``:memory:?cache=shared`` are ordinary file names.

    The audit event carries the raw string and not ``uri=``, so a ``file:``
    string is read both ways. As a URI it is decoded (``file:/p?mode=rwc``,
    ``file:///p``, percent escapes). It touches no file when its LAST ``mode``
    is ``ro`` (a read-only open creates nothing, and nothing is written) or
    ``memory`` (SQLite applies the last ``mode``: ``mode=memory&mode=rwc`` does
    create the file), or when the decoded path is ``:memory:`` or empty. As a
    plain filename it is a literal name in the cwd (``file:state.db?mode=ro`` and
    ``file::memory:`` are writable files there, and SQLite does create them), but
    a literal can only be created when its directory exists, so ``file:/abs/p``
    (needs a directory named ``file:``) is not a literal target.
    """
    if value in _SQLITE_NO_FILE_NAMES:
        return []
    if not value.startswith("file:"):
        return [value]
    targets = [value] if os.path.isdir(os.path.dirname(_normalise(value))) else []
    split = urllib.parse.urlsplit(value)
    modes = urllib.parse.parse_qs(split.query).get("mode", [])
    if modes and modes[-1] in ("ro", "memory"):
        return targets
    if split.netloc in ("", "localhost"):
        decoded = urllib.parse.unquote(split.path)
        if decoded not in _SQLITE_NO_FILE_NAMES:
            targets.append(decoded)
    return targets


def _fd_directory(fd: int) -> str | None:
    """The directory an open descriptor names (Linux ``/proc/self/fd``); None if unknown."""
    try:
        target = os.readlink(f"/proc/self/fd/{fd}")
    except OSError:  # not open, or no /proc
        return None
    return target if target.startswith("/") else None


def _event_path(event: str, args: tuple[Any, ...], index: int) -> str | None:
    """Path argument ``index`` of an audit event, absolute when a ``dir_fd`` anchors it.

    A relative name given with a ``dir_fd`` is relative to that directory, not to
    the working directory, so reading it against the cwd would refuse harmless
    calls (``shutil.rmtree`` of a tree elsewhere, with the cwd inside a root) and
    allow others. The event carries the descriptor (``-1`` or None when it was not
    given). When its directory cannot be read the name is not checkable: None.
    """
    path = _as_path(args[index]) if index < len(args) else None
    if not path or path.startswith("/"):
        return path
    fd_index = _DIR_FD_ARGS.get(event, {}).get(index)
    dir_fd = args[fd_index] if fd_index is not None and fd_index < len(args) else None
    if not isinstance(dir_fd, int) or isinstance(dir_fd, bool) or dir_fd < 0:
        return path
    base = _fd_directory(dir_fd)
    return os.path.join(base, path) if base else None


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

    A relative target resolves against the link's directory, not the cwd. The
    link is read against its ``dir_fd`` when it has one (``_event_path``); a
    relative target of a link whose directory is unknown is not checkable.
    """
    target = _as_path(args[0]) if args else None
    link = _event_path("os.symlink", args, 1)
    if target and not target.startswith("/"):
        target = os.path.join(os.path.dirname(_normalise(link)), target) if link else None
    return link, target


class HomeWriteGuard:
    """Refuses and records writes under ``roots``.

    ``check`` is the audit-hook body. It never writes. It reads only to place a
    name (a ``stat`` for a ``file:`` SQLite name, a ``readlink`` of
    ``/proc/self/fd/N`` for a ``dir_fd``); neither raises an audit event, and the
    ``_busy`` flag stops it recursing into itself.
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
        paths = [p for p in (_event_path(event, args, i) for i in indexes) if p]
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
                source = _event_path(event, args, index)
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
#: Exported state locations that would swallow ordinary writes (``unsafe_state_roots``).
#: Never silently dropped: ``pytest_configure`` refuses to start the run for them.
STARTUP_PROBLEMS: tuple[str, ...] = tuple(unsafe_state_roots())
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


#: The key under which an xdist worker reports its refused writes (always
#: present, an empty list when clean) in ``config.workeroutput``.
WORKER_STATUS_KEY = "verdict_home_guard_violations"

#: What the controller collected from its workers (rendered lines).
_WORKER_FINDINGS = pytest.StashKey[list[str]]()


def worker_status(found: list[Violation]) -> list[dict[str, Any]]:
    """The wire form of a worker's violations (plain types only: execnet carries them)."""
    return [
        {"phase": v.phase, "event": v.event, "path": v.path, "where": list(v.where)} for v in found
    ]


def worker_findings(output: object, worker: str, errored: bool = False) -> list[str]:
    """What a finished xdist worker tells the controller; empty only when it reported clean.

    Anything the controller cannot positively read as "no refused writes" is a
    finding: no output, no status key, a status of the wrong shape.
    """
    if errored:
        return [f"worker {worker} went down before it reported its write-guard status"]
    if not isinstance(output, dict) or WORKER_STATUS_KEY not in output:
        return [f"worker {worker} finished without reporting its write-guard status"]
    status = output[WORKER_STATUS_KEY]
    if not isinstance(status, list):
        return [f"worker {worker} reported an unreadable write-guard status"]
    findings: list[str] = []
    for item in status:
        try:
            violation = Violation(
                str(item["phase"]),
                str(item["event"]),
                str(item["path"]),
                tuple(str(frame) for frame in item["where"]),
            )
        except (TypeError, KeyError):
            findings.append(f"worker {worker} reported an unreadable write-guard entry")
            continue
        findings.append(f"worker {worker}:\n{violation.render()}")
    return findings


@pytest.hookimpl(optionalhook=True)
def pytest_testnodedown(node: Any, error: object) -> None:
    """xdist controller: collect what a worker's guard saw (hook exists only with xdist)."""
    worker = str(getattr(getattr(node, "gateway", None), "id", "?"))
    output = getattr(node, "workeroutput", None)
    findings = worker_findings(output, worker, errored=error is not None)
    node.config.stash.setdefault(_WORKER_FINDINGS, []).extend(findings)


@pytest.hookimpl(trylast=True)
def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Fail the run for refused writes the per-test fixture did not report.

    In a worker (``config.workeroutput`` exists) the status is handed to the
    controller, which owns the run's exit status; elsewhere it fails the run here.
    """
    found = GUARD.drain()
    output = getattr(session.config, "workeroutput", None)
    if output is not None:
        output[WORKER_STATUS_KEY] = worker_status(found)
        return
    findings = session.config.stash.get(_WORKER_FINDINGS, [])
    if not found and not findings:
        return
    parts = [render_violations(found)] if found else []
    if findings:
        parts.append(
            f"{len(findings)} xdist worker write-guard finding(s) "
            "(nothing was written to real state):\n" + "\n".join(findings)
        )
    message = "\n".join(parts)
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    if reporter is not None:
        reporter.write_line(message, red=True)
    else:
        print(message, file=sys.stderr)
    session.exitstatus = int(pytest.ExitCode.TESTS_FAILED)


def pytest_configure(config: pytest.Config) -> None:
    install()
    if STARTUP_PROBLEMS:
        raise pytest.UsageError(
            "tests/helpers/home_guard.py cannot start: the exported state location(s) below "
            "would be protected so broadly that ordinary test writes would count as writes to "
            "real state. Unset each variable or point it at a dedicated directory, then rerun:\n  "
            + "\n  ".join(STARTUP_PROBLEMS)
        )
    plugins = config.pluginmanager
    module = sys.modules[__name__]
    # Already registered when loaded with ``-p tests.helpers.home_guard``.
    if not plugins.is_registered(module):
        plugins.register(module, "verdict-home-write-guard")
