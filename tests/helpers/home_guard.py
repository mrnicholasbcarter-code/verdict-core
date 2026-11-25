"""No test can write the operator's real Verdict state (issue #1306).

Two layers, both loaded from ``tests/conftest.py``:

1. Base protection (``tests/conftest.py``): every test gets its own temp ``HOME`` and
   every state-path env var is cleared (``HOST_STATE_ENV``), so each path
   ``verdict.paths`` derives lands under that temp home. This is what keeps tests off
   the real state.
2. Tripwire (this module): a ``sys.addaudithook`` hook, installed at session start,
   refuses any write, create, rename, delete or SQLite open that targets the real
   state roots, BEFORE the syscall runs, and records it; three ``os`` functions whose
   audit events cannot carry a directory descriptor are wrapped for the session. The
   test (or the session, for code outside a test) then fails naming the file and the
   verdict/test frames that asked for it. It adds no isolation of its own: it notices,
   and where it can stops, a write that got past layer 1.

Why an audit hook and not a before/after fingerprint of ``~/.verdict``: the
installed services write there every few minutes, so a fingerprint blames tests
for other processes' writes and only reports after the damage. The hook sees
only calls made by THIS pytest process, so every hit is a test leak by
construction, and the write never happens.

Guarantees and limits (the one place they are stated)
=====================================================

Guaranteed. In the process that runs the tests (the pytest process, or an xdist
worker), from ``pytest_configure`` on, a call to any of the entry points below whose
arguments name a protected root, a path beneath it, or (for tree moves, links and
symlinks) an ancestor of it, spelled however (relative, ``..``, ``//``, ``file:``
URIs), is refused with ``PermissionError`` before the system call runs, is recorded,
and fails the test that made it even when the code under test swallows the error.
Outside a test it fails the session. Entry points, each pinned by
``tests/test_home_isolation.py``:

* opens for writing: ``open``, ``io.open``, ``pathlib`` and ``os.open`` with a write
  flag (``O_WRONLY``, ``O_RDWR``, ``O_APPEND``, ``O_CREAT``, ``O_TRUNC``, ``O_EXCL``), or
  a ``w``, ``a``, ``x`` or ``+`` mode;
* ``os.rename`` (and ``os.replace``), ``os.remove`` (and ``os.unlink``), ``os.rmdir``,
  ``os.mkdir``, ``os.truncate``, ``os.symlink``, ``os.link``, ``os.chmod``,
  ``os.chown``, ``os.utime``, and ``os.mkfifo`` and ``os.mknod`` (wrapped; see below);
* ``shutil.copyfile``, ``copymode``, ``copystat``, ``copytree`` (the destination),
  ``move`` and ``rmtree``;
* ``sqlite3.connect`` of a file (``:memory:`` and the empty name are not files).

A name is read against the directory it is relative to. A relative name given with
a ``dir_fd`` is read against the directory the descriptor names (``/proc/self/fd``),
never the working directory. ``DIR_FD_COVERAGE`` says, for every ``os`` function that
takes a ``dir_fd``, whether its audit event carries the descriptor (then the hook reads
it) or the function is wrapped; the test pins the table against the running interpreter.
``os.open``, ``os.mkfifo`` and ``os.mknod`` are wrapped for the session
(``install_descriptor_wrappers``, installed and removed with the plugin, and members of
``os.supports_dir_fd`` while installed): the ``open`` event carries no descriptor, and
the other two raise no audit event at all. A wrapper hands its arguments to the original
unchanged, so results, exceptions and errnos are the original's (compared in two
interpreters, with and without the guard, by ``dir_fd_probe``). A descriptor that is no
open directory (closed, or a file) is left to the operating system, which fails the call
with EBADF or ENOTDIR and changes nothing. An open directory descriptor whose path cannot
be read is refused and recorded, not skipped. A session on a platform where descriptors
cannot be resolved does not start (``descriptor_resolution_problem``).

NOT guaranteed (out of reach of a Python-level guard):

* Child processes (``subprocess``, ``multiprocessing``, ``os.system``, services): they
  inherit the isolated environment of layer 1, but the guard does not run in them.
* C code that raises no audit event: extension modules, ``ctypes``, and SQLite writing a
  file named in SQL (``ATTACH DATABASE``, ``VACUUM INTO``) after a connection that was
  not itself refused. Extended attributes (``os.setxattr``, ``os.removexattr``) are not
  checked.
* A descriptor opened before the guard was installed or inherited, and anything done
  through an open descriptor: ``os.write``, ``os.pwrite``, ``os.ftruncate``,
  ``os.sendfile``, ``os.fchmod``, ``os.fchown``, ``os.utime`` on a descriptor,
  ``open(fd, 'w')``, ``mmap`` of an already open file.
* Aliases. Names are read lexically; symlinks are not resolved. A symlink, bind mount or
  hard link that already existed and leads into a root from a name outside it is not seen
  (the guard refuses to CREATE a link or symlink into protected state, or one to an
  ancestor of it), and neither is a test that deliberately builds an alias chain.
* References to the unwrapped functions: ``from os import open`` or ``posix.open``
  captured before the wrappers were installed, and a builtin ``open(name, opener=...)``,
  whose own ``open`` event carries a relative name that is read against the working
  directory (the opener's ``os.open`` call is wrapped and read correctly).
* The check and the call are not one atomic step: another thread can close or replace a
  descriptor, or move a directory, between them.
* A run that skips ``tests/conftest.py`` (``--noconftest``) has no guard; load it there
  with ``-p tests.helpers.home_guard``.
* Linux only. Descriptors are resolved through ``/proc/self/fd``; elsewhere the session
  refuses to start rather than run unguarded.
* While the wrappers are installed ``os.open``, ``os.mkfifo`` and ``os.mknod`` are
  Python functions (with ``__wrapped__``), not the builtins: calls behave the same,
  identity does not (for example ``pickle`` of ``os.open``).
* Audit event shapes differ between Python versions. ``tests/test_home_isolation.py``
  checks the shape of every event this module relies on against the interpreter that
  runs it (CI's interpreter matrix is in ``.github/workflows/ci.yml``).

For what is not guaranteed, run the suite with the real state directories masked by
empty mounts, or under a throwaway ``HOME`` with ``tests/helpers/home_snapshot.py``.

Roots, wiring and reporting
===========================

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

A ``sqlite3.connect`` argument that names no file (``:memory:`` and the empty private
temporary database) is never a write target. The audit event does not say ``uri=``, so
a ``file:`` string is read both ways: as a URI (decoded; a last ``mode`` of ``ro`` or
``memory`` touches no file) and as a literal filename in the working directory, which
SQLite does create.

Under pytest-xdist a worker's own exit status never reaches the controller, so each
worker hands its refused writes to the controller through ``workeroutput`` when it
finishes, and the controller fails the run for them (a refused write during session
teardown, or after the last per-test drain). A worker that finishes without reporting,
or whose report cannot be read, also fails the run: unknown is never clean.

Escape hatch: there is none. A test that believes it needs the real home does
not; point ``HOME``/``VERDICT_HOME`` at ``tmp_path`` instead.
"""

from __future__ import annotations

import errno
import functools
import operator
import os
import re
import stat
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

#: ``os.open`` adds this to the flags it reports in its ``open`` audit event.
_O_CLOEXEC = getattr(os, "O_CLOEXEC", 0)

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
#: ``open`` is absent: its audit event has no descriptor argument (see ``DESCRIPTOR_CALLS``).
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

#: Every ``os`` function that takes a ``dir_fd`` (``os.supports_dir_fd``) and how the guard covers it:
#: ``read-only`` changes nothing; ``audit event`` is covered by the named event, which carries the
#: descriptor (``_DIR_FD_ARGS``); ``wrapper`` is covered by the session wrapper of that name
#: (``DESCRIPTOR_CALLS``) because its audit event does not carry the descriptor or does not exist.
#: ``tests/test_home_isolation.py`` pins this table against the running interpreter: every member
#: of ``os.supports_dir_fd`` is classified, and each named event is raised with the descriptor at
#: the index ``_DIR_FD_ARGS`` says. (``os.replace`` raises ``os.rename``, ``os.unlink`` raises
#: ``os.remove``; ``lstat`` is in the set from Python 3.12.) Not in the set but taking a
#: ``dir_fd``: ``shutil.rmtree`` (3.11+; its event carries it and its inner calls are the
#: ``os.remove``/``os.rmdir`` rows) and ``os.fwalk`` (reads only). ``pathlib`` takes none.
DIR_FD_COVERAGE: dict[str, tuple[str, str]] = {
    "access": ("read-only", ""),
    "chmod": ("audit event", "os.chmod"),
    "chown": ("audit event", "os.chown"),
    "link": ("audit event", "os.link"),
    "lstat": ("read-only", ""),
    "mkdir": ("audit event", "os.mkdir"),
    "mkfifo": ("wrapper", "mkfifo"),
    "mknod": ("wrapper", "mknod"),
    "open": ("wrapper", "open"),
    "readlink": ("read-only", ""),
    "rename": ("audit event", "os.rename"),
    "rmdir": ("audit event", "os.rmdir"),
    "stat": ("read-only", ""),
    "symlink": ("audit event", "os.symlink"),
    "unlink": ("audit event", "os.remove"),
    "utime": ("audit event", "os.utime"),
}


@dataclass(frozen=True)
class DescriptorCall:
    """An ``os`` function the session wraps because its audit event cannot place a ``dir_fd`` name.

    ``os.open`` raises an ``open`` event without the descriptor, so its relative name would be
    read against the working directory. ``os.mkfifo`` and ``os.mknod`` raise no audit event at
    all, with a path or with a descriptor.
    """

    event: str  # the name a violation carries
    parameters: tuple[str, ...]  # positional parameters, in order (``dir_fd`` is keyword-only)
    audited: bool  # its call with a path (no descriptor) raises an event the hook checks
    flags: str | None  # the parameter whose write flags decide whether the call writes


DESCRIPTOR_CALLS: dict[str, DescriptorCall] = {
    "open": DescriptorCall("os.open", ("path", "flags", "mode"), audited=True, flags="flags"),
    "mkfifo": DescriptorCall("os.mkfifo", ("path", "mode"), audited=False, flags=None),
    "mknod": DescriptorCall("os.mknod", ("path", "mode", "device"), audited=False, flags=None),
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


#: Where Linux lists this process's open descriptors, and how an entry is read.
#: Module attributes so the guard's own tests can make the lookup fail.
_PROC_FD_DIR = "/proc/self/fd"
_readlink = os.readlink


class _UnresolvedDirFdError(Exception):
    """An open directory descriptor whose path could not be read: the guard cannot place the call."""

    def __init__(self, fd: int, name: str, reason: str) -> None:
        super().__init__(reason)
        self.fd, self.name, self.reason = fd, name, reason

    def describe(self) -> str:
        return f"<dir_fd {self.fd}: {self.reason}>/{self.name}"


def _fd_number(value: Any) -> int | None:
    """The descriptor a ``dir_fd`` argument names; None when it names none (not given, ``-1``)."""
    if value is None:
        return None
    try:
        number = operator.index(value)
    except TypeError:  # the call itself rejects it
        return None
    return number if number >= 0 else None


def _fd_directory(fd: int, name: str) -> str | None:
    """The directory an open descriptor names (Linux ``/proc/self/fd``).

    None when ``fd`` is not an open directory (closed, or a file, socket, pipe): the
    operating system then fails the call itself, with EBADF or ENOTDIR, and changes
    nothing, so there is nothing to place. An open DIRECTORY whose name cannot be read
    raises ``_UnresolvedDirFdError``: the call can land anywhere, so the caller refuses it.
    """
    try:
        mode = os.fstat(fd).st_mode
    except OSError:
        return None
    if not stat.S_ISDIR(mode):
        return None
    link = f"{_PROC_FD_DIR}/{fd}"
    try:
        target = _readlink(link)
    except OSError as exc:
        raise _UnresolvedDirFdError(
            fd, name, f"{link} cannot be read ({exc.strerror or exc})"
        ) from None
    if not target.startswith("/"):
        raise _UnresolvedDirFdError(fd, name, f"{link} names {target!r}, not a path")
    return target


def _anchored(name: str, fd: int) -> str | None:
    """``name`` (relative to the directory ``fd`` names) as an absolute path; None: the OS fails."""
    base = _fd_directory(fd, name)
    return os.path.join(base, name) if base else None


def descriptor_resolution_problem() -> str | None:
    """Why this platform cannot name the directory a descriptor refers to; None when it can.

    Probed with a directory that always exists (this checkout), opened read-only, so the
    probe creates nothing and does not go through ``tempfile``'s cached temp dir.
    """
    probe = str(_REPO_ROOT)
    try:
        fd = os.open(probe, os.O_RDONLY | os.O_DIRECTORY)
    except OSError as exc:
        return f"{probe} cannot be opened to probe descriptor resolution ({exc})"
    try:
        resolved = _fd_directory(fd, probe)
    except _UnresolvedDirFdError as unresolved:
        return unresolved.reason
    finally:
        os.close(fd)
    expected = os.path.realpath(probe)
    if resolved != expected:
        return f"{_PROC_FD_DIR}/<fd> names {resolved!r} for a directory open at {expected!r}"
    return None


def _event_path(event: str, args: tuple[Any, ...], index: int) -> str | None:
    """Path argument ``index`` of an audit event, absolute when a ``dir_fd`` anchors it.

    A relative name given with a ``dir_fd`` is relative to that directory, not to
    the working directory, so reading it against the cwd would refuse harmless
    calls (``shutil.rmtree`` of a tree elsewhere, with the cwd inside a root) and
    allow others. The event carries the descriptor (``-1`` or None when it was not
    given). None: the descriptor is no open directory, so the OS fails the call itself.
    Raises ``_UnresolvedDirFdError`` for an open directory whose path cannot be read.
    """
    path = _as_path(args[index]) if index < len(args) else None
    if not path or path.startswith("/"):
        return path
    fd_index = _DIR_FD_ARGS.get(event, {}).get(index)
    fd = _fd_number(args[fd_index]) if fd_index is not None and fd_index < len(args) else None
    return path if fd is None else _anchored(path, fd)


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
    link is read against its ``dir_fd`` when it has one (``_event_path``); when
    that descriptor is no open directory the OS fails the call itself and the
    relative target has nothing to resolve against.
    """
    target = _as_path(args[0]) if args else None
    link = _event_path("os.symlink", args, 1)
    if target and not target.startswith("/"):
        target = os.path.join(os.path.dirname(_normalise(link)), target) if link else None
    return link, target


class HomeWriteGuard:
    """Refuses and records writes under ``roots``.

    ``check`` is the audit-hook body; ``vet`` is the same decision for the three
    ``os`` functions the session wraps (``DESCRIPTOR_CALLS``). Neither writes. They
    read only to place a name (a ``stat`` for a ``file:`` SQLite name; an ``fstat``
    and a ``readlink`` of ``/proc/self/fd/N`` for a ``dir_fd``); none of those
    raises an audit event, and the ``_busy`` flag stops a check recursing into itself.
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

    def expect_open(self, expected: tuple[str, int] | None) -> None:
        """The ``open`` event the next wrapped ``os.open`` raises was already read (or None)."""
        self._busy.expected_open = expected

    def _already_read(self, args: tuple[Any, ...]) -> bool:
        """Is this ``open`` event the one a wrapper has just read against its descriptor?"""
        expected = getattr(self._busy, "expected_open", None)
        if expected is None:
            return False
        name = _as_path(args[0]) if args else None
        flags = args[2] if len(args) > 2 and isinstance(args[2], int) else None
        if flags is None or (name, flags | _O_CLOEXEC) != expected:
            return False
        self._busy.expected_open = None
        return True

    def vet(
        self, spec: DescriptorCall, args: tuple[Any, ...], kwargs: dict[str, Any]
    ) -> tuple[str, int] | None:
        """Refuse a wrapped call that would change protected state (``PermissionError``).

        A relative name given with a ``dir_fd`` is read against the directory the
        descriptor names. A descriptor that is no open directory is left to the OS
        (EBADF, ENOTDIR); an open directory whose path cannot be read is refused.

        Returns, for a call whose own ``open`` audit event must not be read again against
        the working directory, that event's (name, flags); otherwise None.
        """
        if getattr(self._busy, "on", False):
            return None
        given = dict(zip(spec.parameters, args, strict=False))
        given.update((k, v) for k, v in kwargs.items() if k in spec.parameters or k == "dir_fd")
        name = _as_path(given.get("path"))
        flags = given.get(spec.flags) if spec.flags else None
        if not name:
            return None
        if spec.flags and not (isinstance(flags, int) and flags & _WRITE_FLAGS):
            return None  # a read, or arguments the call itself rejects
        fd = _fd_number(given.get("dir_fd"))
        anchor = None if name.startswith("/") else fd  # an absolute name ignores the descriptor
        if spec.audited and anchor is None:
            return None  # its ``open`` event reads this name against the cwd, which is right
        self._busy.on = True
        try:
            target: str | None = name
            problem = ""
            if anchor is not None:
                try:
                    target = _anchored(name, anchor)
                except _UnresolvedDirFdError as unresolved:
                    target, problem = None, unresolved.describe()
            if problem:
                self._deny(spec.event, problem)
            if target and self.protected_root(target):
                self._deny(spec.event, target)
        finally:
            self._busy.on = False
        return (name, flags | _O_CLOEXEC) if spec.audited and isinstance(flags, int) else None

    def check(self, event: str, args: tuple[Any, ...]) -> None:
        if event != "open" and event not in _MUTATED_ARGS:
            return
        if getattr(self._busy, "on", False):
            return
        if event == "open" and self._already_read(args):
            return
        self._busy.on = True
        try:
            self._refuse_protected(event, args)
        except _UnresolvedDirFdError as unresolved:
            problem = unresolved.describe()
        else:
            problem = ""
        finally:
            self._busy.on = False
        if problem:
            self._deny(event, problem)

    def _refuse_protected(self, event: str, args: tuple[Any, ...]) -> None:
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

    def _deny(self, event: str, path: str) -> None:
        self.violations.append(Violation(self.phase, event, path, _callers()))
        raise PermissionError(
            errno.EACCES,
            "tests must not write the operator's real Verdict state "
            "(isolated HOME/VERDICT_HOME did not cover this path)",
            path,
        ) from None

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


#: name -> (the ``os`` function as it was, the wrapper) while the wrappers are installed.
_wrapped: dict[str, tuple[Callable[..., Any], Callable[..., Any]]] = {}


def _wrap(
    guard: HomeWriteGuard, spec: DescriptorCall, original: Callable[..., Any]
) -> Callable[..., Any]:
    """``original`` behind ``guard.vet``: same arguments, same result, same exceptions.

    The wrapper takes ``*args, **kwargs`` and hands them on untouched, so a call the
    guard does not understand (wrong arity, wrong types) fails inside ``original`` with
    its own error. ``vet`` only reads, and only raises the guard's ``PermissionError``.
    """

    @functools.wraps(original)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        if spec.audited and kwargs.get("dir_fd") is None:
            return original(*args, **kwargs)  # its ``open`` event is the hook's business alone
        expected = guard.vet(spec, args, kwargs)
        if expected is None:
            return original(*args, **kwargs)
        guard.expect_open(expected)
        try:
            return original(*args, **kwargs)
        finally:
            guard.expect_open(None)

    return wrapper


def install_descriptor_wrappers(guard: HomeWriteGuard | None = None) -> None:
    """Wrap ``os.open``, ``os.mkfifo`` and ``os.mknod`` (``DESCRIPTOR_CALLS``); idempotent.

    Each wrapper is also added to ``os.supports_dir_fd`` while it is installed: ``shutil``
    and other code decide whether to use descriptor-relative calls by testing membership of
    the ``os`` function in that set, and a wrapper that is not a member would switch
    ``shutil.rmtree`` to its slower path.
    """
    guard = GUARD if guard is None else guard
    for name, spec in DESCRIPTOR_CALLS.items():
        current = getattr(os, name)
        recorded = _wrapped.get(name)
        if recorded is not None and recorded[1] is current:
            continue
        if recorded is not None:  # something else replaced our wrapper: forget it
            os.supports_dir_fd.discard(recorded[1])
        wrapper = _wrap(guard, spec, current)
        _wrapped[name] = (current, wrapper)
        if current in os.supports_dir_fd:
            os.supports_dir_fd.add(wrapper)
        setattr(os, name, wrapper)


def remove_descriptor_wrappers() -> None:
    """Put back what ``install_descriptor_wrappers`` replaced (unless something else did since)."""
    for name, (original, wrapper) in list(_wrapped.items()):
        if getattr(os, name) is wrapper:
            setattr(os, name, original)
        os.supports_dir_fd.discard(wrapper)
        del _wrapped[name]


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
    unresolvable = descriptor_resolution_problem()
    if unresolvable:
        raise pytest.UsageError(
            "tests/helpers/home_guard.py cannot start: it places a descriptor-relative name "
            "(a call with dir_fd=) through /proc/self/fd, and that does not work here, so such "
            "a write could land in real state unseen. Run the tests on Linux with /proc "
            f"mounted. Reason: {unresolvable}"
        )
    install_descriptor_wrappers()
    plugins = config.pluginmanager
    module = sys.modules[__name__]
    # Already registered when loaded with ``-p tests.helpers.home_guard``.
    if not plugins.is_registered(module):
        plugins.register(module, "verdict-home-write-guard")


def pytest_unconfigure(config: pytest.Config) -> None:
    remove_descriptor_wrappers()
