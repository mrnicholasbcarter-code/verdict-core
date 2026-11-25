"""Child-process probes for ``tests/test_home_isolation.py`` (not a test module).

A Python audit hook cannot be removed once installed, so "with and without the
guard" has to be two interpreters. Each mode prints one JSON document:

``results [--guard]``
    Run a fixed list of descriptor-relative calls in a temp directory and record, for
    each, what it returned or which exception (type, errno, message) it raised. With
    ``--guard`` the home write guard and its descriptor wrappers are installed first.
    The parent compares the two documents: a guard that changes what legitimate code
    sees fails the comparison.

``events``
    Record the audit events one call per ``os.supports_dir_fd`` member raises, so the
    parent can pin the shape of each event on the interpreter that runs the tests.

``lifecycle <workdir>``
    Run pytest twice in this process with the guard plugin, and report whether the
    descriptor wrappers were in place during each run and gone after it.
"""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import stat
import sys
import tempfile
from collections.abc import Callable
from typing import Any

#: Names the guard wraps (``DESCRIPTOR_CALLS``), read here without importing the guard.
WRAPPED_NAMES = ("open", "mkfifo", "mknod")

_O_PATH = getattr(os, "O_PATH", 0)
_CREATE = os.O_WRONLY | os.O_CREAT | os.O_EXCL
_APPEND = os.O_WRONLY | os.O_APPEND


class _Index:
    """An integer-like object: ``os`` takes it wherever it takes an int (``__index__``)."""

    def __init__(self, value: int) -> None:
        self.value = value

    def __index__(self) -> int:
        return self.value


class _LyingInt(int):
    """An int whose ``__index__`` disagrees with its value: the call uses the value."""

    def __index__(self) -> int:
        return 0


class _IndexRaises:
    """Looks integer-like and is not: ``__index__`` raises ``TypeError``."""

    def __index__(self) -> int:
        raise TypeError("not an integer after all")


def _opened(name: Any, flags: int, *args: Any, **kwargs: Any) -> Callable[[], object]:
    def call() -> object:
        os.close(os.open(name, flags, *args, **kwargs))
        return "fd"

    return call


def _operations(d: str, dfd: int, ffd: int, cfd: int) -> list[tuple[str, Callable[[], object]]]:
    """(label, call): ``dfd`` a directory, ``ffd`` a file, ``cfd`` a closed descriptor."""
    here = pathlib.Path

    def path(name: str) -> str:
        return os.path.join(d, name)

    def rmtree_at() -> object:
        if sys.version_info < (3, 11):
            return "unsupported"
        os.makedirs(path("tree/inner"))
        shutil.rmtree("tree", dir_fd=dfd)
        return os.path.exists(path("tree"))

    def rmtree_plain() -> object:
        os.makedirs(path("tree2/inner"))
        shutil.rmtree(path("tree2"))
        return os.path.exists(path("tree2"))

    def fwalk() -> object:
        return sorted(name for _, _, files, _ in os.fwalk(d) for name in files)

    def scandir_fd() -> object:
        with os.scandir(dfd) as entries:
            return sorted(entry.name for entry in entries)

    def mkstemp() -> object:
        fd, name = tempfile.mkstemp(dir=d)
        os.close(fd)
        os.remove(name)
        return "mkstemp"

    def temporary_directory() -> object:
        with tempfile.TemporaryDirectory(dir=d) as inner:
            pathlib.Path(inner, "x").write_text("x")
        return os.path.exists(inner)

    def builtin_open_with_opener() -> object:
        with open("opened", "w", opener=lambda p, fl: os.open(p, fl, 0o600, dir_fd=dfd)) as handle:
            return handle.write("x")

    def pathlib_write() -> object:
        return pathlib.Path(path("pathlib-write")).write_text("x")

    return [
        # os.open: writing forms
        ("open create exclusive", _opened("new1", _CREATE, 0o600, dir_fd=dfd)),
        ("open create exclusive again", _opened("new1", _CREATE, 0o600, dir_fd=dfd)),
        ("open truncate", _opened("existing", os.O_WRONLY | os.O_TRUNC, dir_fd=dfd)),
        ("open read-write", _opened("existing", os.O_RDWR, dir_fd=dfd)),
        ("open append", _opened("existing", os.O_WRONLY | os.O_APPEND, dir_fd=dfd)),
        (
            "open create keyword mode",
            _opened("new2", os.O_WRONLY | os.O_CREAT, mode=0o600, dir_fd=dfd),
        ),
        (
            "open all keywords",
            lambda: os.close(os.open(path="new3", flags=_CREATE, mode=0o600, dir_fd=dfd)),
        ),
        ("open bytes name", _opened(b"new4", _CREATE, 0o600, dir_fd=dfd)),
        ("open path-like name", _opened(here("new5"), _CREATE, 0o600, dir_fd=dfd)),
        (
            "open write on a symlink, no follow",
            _opened("sym", os.O_WRONLY | os.O_NOFOLLOW, dir_fd=dfd),
        ),
        ("open write, name missing", _opened("missing", os.O_WRONLY, dir_fd=dfd)),
        ("open write, closed descriptor", _opened("existing", os.O_WRONLY, dir_fd=cfd)),
        (
            "open create, closed descriptor",
            _opened("n", os.O_WRONLY | os.O_CREAT, 0o600, dir_fd=cfd),
        ),
        ("open write, file descriptor", _opened("existing", os.O_WRONLY, dir_fd=ffd)),
        ("open write, descriptor -1", _opened("existing", os.O_WRONLY, dir_fd=-1)),
        (
            "open write, absolute name, closed descriptor",
            _opened(path("existing"), os.O_WRONLY | os.O_APPEND, dir_fd=cfd),
        ),
        (
            "open write, absolute name, descriptor given",
            _opened(path("existing"), os.O_WRONLY | os.O_APPEND, dir_fd=dfd),
        ),
        ("open write, no descriptor", _opened(path("plain"), os.O_WRONLY | os.O_CREAT, 0o600)),
        (
            "open write, descriptor None",
            _opened(path("plain2"), os.O_WRONLY | os.O_CREAT, 0o600, dir_fd=None),
        ),
        # os.open: forms that read
        ("open read", _opened("existing", os.O_RDONLY, dir_fd=dfd)),
        (
            "open read keywords",
            lambda: os.close(os.open(path="existing", flags=os.O_RDONLY, dir_fd=dfd)),
        ),
        ("open directory", _opened(".", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=dfd)),
        ("open O_PATH", _opened("existing", _O_PATH | os.O_NOFOLLOW, dir_fd=dfd)),
        ("open read a symlink, no follow", _opened("sym", os.O_RDONLY | os.O_NOFOLLOW, dir_fd=dfd)),
        ("open read, name missing", _opened("missing", os.O_RDONLY, dir_fd=dfd)),
        ("open read, closed descriptor", _opened("existing", os.O_RDONLY, dir_fd=cfd)),
        ("open read, file descriptor", _opened("existing", os.O_RDONLY, dir_fd=ffd)),
        # os.open: arguments the OS call itself rejects
        ("open no arguments", lambda: os.open()),
        ("open no flags", lambda: os.open("existing")),
        ("open flags a string", lambda: os.open("existing", "x")),
        ("open flags a float", _opened("existing", 1.5, dir_fd=dfd)),
        ("open name a float", lambda: os.open(1.5, os.O_RDONLY)),
        ("open NUL in the name", _opened("a\0b", os.O_WRONLY | os.O_CREAT, dir_fd=dfd)),
        ("open descriptor a string", _opened("existing", os.O_WRONLY, dir_fd="x")),
        ("open descriptor a float", _opened("existing", os.O_WRONLY, dir_fd=1.5)),
        ("open too many positionals", lambda: os.open("existing", os.O_WRONLY, 0o600, dfd)),
        ("open unknown keyword", _opened("existing", os.O_WRONLY, nonsense=1)),
        # os.open: integer-like arguments (``__index__``), which the call converts itself
        ("open create, flags integer-like", _opened("ix1", _Index(_CREATE), 0o600, dir_fd=dfd)),
        ("open create again, flags integer-like", _opened("ix1", _Index(_CREATE), dir_fd=dfd)),
        (
            "open truncate, flags integer-like",
            _opened("existing", _Index(os.O_WRONLY | os.O_TRUNC), dir_fd=dfd),
        ),
        ("open append, flags integer-like", _opened("existing", _Index(_APPEND), dir_fd=dfd)),
        (
            "open create, flags an int that lies",
            _opened("ix2", _LyingInt(_CREATE), 0o600, dir_fd=dfd),
        ),
        ("open create, mode integer-like", _opened("ix3", _CREATE, _Index(0o600), dir_fd=dfd)),
        (
            "open create, descriptor integer-like",
            _opened("ix4", _CREATE, 0o600, dir_fd=_Index(dfd)),
        ),
        (
            "open create, everything integer-like, keywords",
            lambda: os.close(
                os.open(path="ix5", flags=_Index(_CREATE), mode=_Index(0o600), dir_fd=_Index(dfd))
            ),
        ),
        ("open read, flags integer-like", _opened("existing", _Index(os.O_RDONLY), dir_fd=dfd)),
        (
            "open create, flags integer-like, closed descriptor",
            _opened("ix6", _Index(_CREATE), 0o600, dir_fd=cfd),
        ),
        (
            "open create, flags integer-like, descriptor -1",
            _opened("ix7", _Index(_CREATE), 0o600, dir_fd=_Index(-1)),
        ),
        (
            "open create, flags integer-like, absolute name",
            _opened(path("ix8"), _Index(_CREATE), 0o600, dir_fd=_Index(cfd)),
        ),
        ("open write, flags an object", _opened("existing", object(), dir_fd=dfd)),
        ("open write, flags a string", _opened("existing", "w", dir_fd=dfd)),
        ("open write, flags None", _opened("existing", None, dir_fd=dfd)),
        ("open write, flags raise TypeError", _opened("existing", _IndexRaises(), dir_fd=dfd)),
        ("open create, mode an object", _opened("ix9", _CREATE, object(), dir_fd=dfd)),
        ("open create, descriptor an object", _opened("ix10", _CREATE, 0o600, dir_fd=object())),
        # os.mkfifo / os.mknod: no audit event at all
        ("mkfifo descriptor", lambda: os.mkfifo("fifo1", dir_fd=dfd)),
        ("mkfifo descriptor again", lambda: os.mkfifo("fifo1", dir_fd=dfd)),
        ("mkfifo positional mode", lambda: os.mkfifo("fifo2", 0o600, dir_fd=dfd)),
        ("mkfifo keywords", lambda: os.mkfifo(path="fifo3", mode=0o600, dir_fd=dfd)),
        ("mkfifo bytes name", lambda: os.mkfifo(b"fifo4", dir_fd=dfd)),
        ("mkfifo plain path", lambda: os.mkfifo(path("fifo5"))),
        ("mkfifo absolute name with a descriptor", lambda: os.mkfifo(path("fifo6"), dir_fd=cfd)),
        ("mkfifo closed descriptor", lambda: os.mkfifo("fifo7", dir_fd=cfd)),
        ("mkfifo file descriptor", lambda: os.mkfifo("fifo8", dir_fd=ffd)),
        ("mkfifo descriptor -1", lambda: os.mkfifo("fifo9", dir_fd=-1)),
        ("mkfifo no arguments", lambda: os.mkfifo()),
        ("mkfifo mode integer-like", lambda: os.mkfifo("fifo10", _Index(0o600), dir_fd=dfd)),
        ("mkfifo descriptor integer-like", lambda: os.mkfifo("fifo11", 0o600, dir_fd=_Index(dfd))),
        ("mkfifo mode an object", lambda: os.mkfifo("fifo12", object(), dir_fd=dfd)),
        ("mkfifo mode a string", lambda: os.mkfifo("fifo13", "x", dir_fd=dfd)),
        ("mkfifo mode a string, no descriptor", lambda: os.mkfifo(path("fifo14"), "x")),
        ("mkfifo descriptor an object", lambda: os.mkfifo("fifo15", dir_fd=object())),
        ("mknod descriptor", lambda: os.mknod("node1", dir_fd=dfd)),
        ("mknod descriptor again", lambda: os.mknod("node1", dir_fd=dfd)),
        ("mknod regular file mode", lambda: os.mknod("node2", stat.S_IFREG | 0o600, dir_fd=dfd)),
        ("mknod positional device", lambda: os.mknod("node3", stat.S_IFREG | 0o600, 0, dir_fd=dfd)),
        ("mknod plain path", lambda: os.mknod(path("node4"))),
        (
            "mknod mode integer-like",
            lambda: os.mknod("node7", _Index(stat.S_IFREG | 0o600), dir_fd=dfd),
        ),
        (
            "mknod device integer-like",
            lambda: os.mknod("node8", stat.S_IFREG | 0o600, _Index(0), dir_fd=dfd),
        ),
        (
            "mknod descriptor integer-like",
            lambda: os.mknod("node9", stat.S_IFREG | 0o600, 0, dir_fd=_Index(dfd)),
        ),
        ("mknod mode a string", lambda: os.mknod("node10", "x", dir_fd=dfd)),
        ("mknod device a string", lambda: os.mknod("node11", stat.S_IFREG, "x", dir_fd=dfd)),
        ("mknod device an object", lambda: os.mknod("node12", stat.S_IFREG, object(), dir_fd=dfd)),
        ("mknod closed descriptor", lambda: os.mknod("node5", dir_fd=cfd)),
        ("mknod file descriptor", lambda: os.mknod("node6", dir_fd=ffd)),
        # calls whose audit events carry the descriptor
        ("mkdir", lambda: os.mkdir("made", dir_fd=dfd)),
        ("mkdir again", lambda: os.mkdir("made", dir_fd=dfd)),
        ("mkdir closed descriptor", lambda: os.mkdir("made2", dir_fd=cfd)),
        ("mkdir file descriptor", lambda: os.mkdir("made3", dir_fd=ffd)),
        ("symlink", lambda: os.symlink("existing", "alias", dir_fd=dfd)),
        ("link", lambda: os.link("existing", "linked", src_dir_fd=dfd, dst_dir_fd=dfd)),
        ("rename", lambda: os.rename("linked", "renamed", src_dir_fd=dfd, dst_dir_fd=dfd)),
        ("replace", lambda: os.replace("renamed", "replaced", src_dir_fd=dfd, dst_dir_fd=dfd)),
        (
            "rename closed descriptor",
            lambda: os.rename("replaced", "x", src_dir_fd=cfd, dst_dir_fd=dfd),
        ),
        ("chmod", lambda: os.chmod("existing", 0o640, dir_fd=dfd)),
        ("utime", lambda: os.utime("existing", (1, 1), dir_fd=dfd)),
        ("stat", lambda: type(os.stat("existing", dir_fd=dfd)).__name__),
        ("readlink", lambda: os.readlink("sym", dir_fd=dfd)),
        ("access", lambda: os.access("existing", os.W_OK, dir_fd=dfd)),
        ("unlink", lambda: os.unlink("replaced", dir_fd=dfd)),
        ("unlink closed descriptor", lambda: os.unlink("existing", dir_fd=cfd)),
        ("rmdir", lambda: os.rmdir("made", dir_fd=dfd)),
        ("rmdir missing", lambda: os.rmdir("made", dir_fd=dfd)),
        ("rmtree with a descriptor", rmtree_at),
        ("rmtree of a path", rmtree_plain),
        ("fwalk", fwalk),
        ("scandir of a descriptor", scandir_fd),
        ("mkstemp", mkstemp),
        ("TemporaryDirectory", temporary_directory),
        ("builtin open with an opener", builtin_open_with_opener),
        ("pathlib write_text", pathlib_write),
        # what code that inspects ``os`` sees
        ("os.open is in supports_dir_fd", lambda: os.open in os.supports_dir_fd),
        ("os.mkfifo is in supports_dir_fd", lambda: os.mkfifo in os.supports_dir_fd),
        ("os.mknod is in supports_dir_fd", lambda: os.mknod in os.supports_dir_fd),
        (
            "shutil's fd test still holds",
            lambda: {os.open, os.stat, os.unlink, os.rmdir} <= os.supports_dir_fd,
        ),
        ("shutil._use_fd_functions", lambda: shutil._use_fd_functions),
        ("names", lambda: [os.open.__name__, os.mkfifo.__name__, os.mknod.__name__]),
        ("modules", lambda: [os.open.__module__, os.mkfifo.__module__, os.mknod.__module__]),
        ("callable", lambda: all(callable(f) for f in (os.open, os.mkfifo, os.mknod))),
    ]


def _outcome(call: Callable[[], object], scrub: str) -> list[Any]:
    try:
        value = call()
    except BaseException as exc:  # record whatever the call does, KeyboardInterrupt aside
        if isinstance(exc, KeyboardInterrupt):
            raise
        message = str(exc).replace(scrub, "<D>")
        return ["raised", type(exc).__name__, getattr(exc, "errno", None), message]
    if value is None or isinstance(value, (bool, int, str, list)):
        return ["returned", value]
    return ["returned", type(value).__name__]


def _listing(directory: str) -> list[list[Any]]:
    entries = []
    for entry in sorted(os.scandir(directory), key=lambda e: e.name):
        info = entry.stat(follow_symlinks=False)
        entries.append([entry.name, stat.S_IFMT(info.st_mode), info.st_size])
    return entries


def _activate_guard() -> None:
    from tests.helpers import home_guard

    home_guard.install()
    home_guard.install_descriptor_wrappers()


def results(guarded: bool) -> dict[str, Any]:
    home = os.environ["HOME"]
    protected = os.path.join(home, ".verdict")
    os.makedirs(protected, exist_ok=True)
    if guarded:
        _activate_guard()
    d = tempfile.mkdtemp(prefix="dir-fd-probe-")
    pathlib.Path(d, "existing").write_text("x")
    pathlib.Path(d, "file").write_text("f")
    os.symlink("existing", os.path.join(d, "sym"))
    dfd = os.open(d, os.O_RDONLY | os.O_DIRECTORY)
    ffd = os.open(os.path.join(d, "file"), os.O_RDONLY)
    cfd = os.open(d, os.O_RDONLY)
    os.close(cfd)
    outcomes = {label: _outcome(call, d) for label, call in _operations(d, dfd, ffd, cfd)}
    listing = _listing(d)
    # Is the guard really in this interpreter? A refused write to the protected home says so.
    engaged: dict[str, Any] = {}
    try:
        with open(os.path.join(protected, "engaged"), "w"):
            engaged["path write"] = "allowed"
    except PermissionError:
        engaged["path write"] = "refused"
    root_fd = os.open(protected, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.close(os.open("engaged-fd", os.O_WRONLY | os.O_CREAT, 0o600, dir_fd=root_fd))
        engaged["descriptor write"] = "allowed"
    except PermissionError:
        engaged["descriptor write"] = "refused"
    finally:
        os.close(root_fd)
    root_fd = os.open(protected, os.O_RDONLY | os.O_DIRECTORY)
    try:
        # the same write, with arguments that are integer-like and not ints
        os.close(os.open("engaged-ix", _Index(os.O_WRONLY | os.O_CREAT), 0o600, dir_fd=root_fd))
        engaged["integer-like flags write"] = "allowed"
    except PermissionError:
        engaged["integer-like flags write"] = "refused"
    finally:
        os.close(root_fd)
    engaged["wrapped"] = {name: hasattr(getattr(os, name), "__wrapped__") for name in WRAPPED_NAMES}
    return {"results": outcomes, "listing": listing, "engaged": engaged}


def events() -> dict[str, Any]:
    recorded: list[list[Any]] = []

    def hook(event: str, args: tuple[Any, ...]) -> None:
        if event.startswith(("os.", "shutil.", "open")):
            recorded.append(
                [event, [a if isinstance(a, (str, int, type(None))) else repr(a) for a in args]]
            )

    d = tempfile.mkdtemp(prefix="dir-fd-events-")
    for name in ("f", "g"):
        pathlib.Path(d, name).write_text("x")
    os.mkdir(os.path.join(d, "sub"))
    dfd = os.open(d, os.O_RDONLY | os.O_DIRECTORY)
    calls: dict[str, Callable[[], object]] = {
        "access": lambda: os.access("f", os.R_OK, dir_fd=dfd),
        "chmod": lambda: os.chmod("f", 0o600, dir_fd=dfd),
        "chown": lambda: os.chown("f", -1, -1, dir_fd=dfd),
        "link": lambda: os.link("f", "h", src_dir_fd=dfd, dst_dir_fd=dfd),
        "lstat": lambda: os.lstat("f", dir_fd=dfd),
        "mkdir": lambda: os.mkdir("m", dir_fd=dfd),
        "mkfifo": lambda: os.mkfifo("fifo", dir_fd=dfd),
        "mknod": lambda: os.mknod("node", dir_fd=dfd),
        "open": lambda: os.close(os.open("f", os.O_WRONLY | os.O_APPEND, dir_fd=dfd)),
        "readlink": lambda: os.readlink("s", dir_fd=dfd),
        "rename": lambda: os.rename("g", "g2", src_dir_fd=dfd, dst_dir_fd=dfd),
        "rmdir": lambda: os.rmdir("sub", dir_fd=dfd),
        "stat": lambda: os.stat("f", dir_fd=dfd),
        "symlink": lambda: os.symlink("f", "s", dir_fd=dfd),
        "unlink": lambda: os.unlink("h", dir_fd=dfd),
        "utime": lambda: os.utime("f", None, dir_fd=dfd),
    }
    sys.addaudithook(hook)
    seen: dict[str, list[list[Any]]] = {}
    order = [
        "symlink",
        "readlink",
        "link",
        "unlink",
        *sorted(set(calls) - {"symlink", "readlink", "link", "unlink"}),
    ]
    for name in order:
        if getattr(os, name, None) not in os.supports_dir_fd:
            continue
        recorded.clear()
        calls[name]()
        seen[name] = [list(item) for item in recorded]
    return {"dir_fd": dfd, "calls": seen, "members": sorted(f.__name__ for f in os.supports_dir_fd)}


_INSIDE = """
import os
import posix


def test_the_descriptor_wrappers_are_in_place():
    for name in ("open", "mkfifo", "mknod"):
        wrapper = getattr(os, name)
        assert wrapper.__wrapped__ is getattr(posix, name)
        assert wrapper in os.supports_dir_fd
    assert {os.open, os.stat, os.unlink, os.rmdir} <= os.supports_dir_fd
"""


def lifecycle(workdir: str) -> dict[str, Any]:
    import pytest

    pathlib.Path(workdir, "test_inside.py").write_text(_INSIDE)
    originals = {name: getattr(os, name) for name in WRAPPED_NAMES}
    members = set(os.supports_dir_fd)
    report: dict[str, Any] = {"runs": []}
    for _ in range(2):
        code = pytest.main(
            [
                "-q",
                "-p",
                "no:cacheprovider",
                "-o",
                "addopts=",
                "-p",
                "tests.helpers.home_guard",
                workdir,
            ]
        )
        report["runs"].append(
            {
                "exit": int(code),
                "restored": all(getattr(os, n) is originals[n] for n in WRAPPED_NAMES),
                "membership": set(os.supports_dir_fd) == members,
            }
        )
    return report


def main(argv: list[str]) -> None:
    mode = argv[0]
    if mode == "results":
        document = results(guarded="--guard" in argv[1:])
    elif mode == "events":
        document = events()
    elif mode == "lifecycle":
        document = lifecycle(argv[1])
    else:
        raise SystemExit(f"unknown mode {mode!r}")
    sys.stdout.write("\n" + json.dumps(document, sort_keys=True) + "\n")


if __name__ == "__main__":
    main(sys.argv[1:])
