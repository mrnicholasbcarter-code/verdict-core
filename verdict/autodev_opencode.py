"""OpenCode as the ``verdict autodev`` model-tier executor, in an isolated attempt tree (OC2a, #1301).

``verdict autodev`` repairs code in the user's own repository. An agent that edits in place can
stage, commit and half-edit that tree, so this module never lets the worker touch it. Per attempt:

1. :func:`seed_attempt_tree` makes an unshared git clone of the repository in a new private
   directory and overlays the user's dirty state by copying files (design D2, D3). It records a
   manifest (M0) of every non-directory path: kind, executable bit and SHA-256.
2. The worker (an :class:`OpenCodeExecutor` run) edits only that clone.
3. :func:`attempt_delta` compares the clone with M0 by file content and runs no git command in it
   (D4); a worker-planted ``core.fsmonitor`` or hook therefore never runs. Every changed path must
   be owned by the unit, a regular file or a deletion.
4. :func:`transfer` copies only the validated owned files into the user's tree, journalling every
   mutation before it makes it (D6, D7); :func:`restore` undoes a transfer from that journal.

``verdict.autodev_run`` stays the judge: it attributes the change, runs the unit's command in the
user's tree and calls ``restore()`` on every exit that is not a verified success.

Everything that reads, writes or removes a path the worker could have changed is descriptor-relative
and follows no link, so swapping a directory for a symlink cannot redirect a read, a write or a
removal: the walk and the reads open the attempt tree once and accept only the directory that was
seeded, and the attempt directory is held by descriptor from the moment it is made
(:class:`AttemptDirectory`) because the worker can rename it.  POSIX only.
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import hashlib
import json
import os
import secrets
import shlex
import shutil
import stat
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from verdict.patch_executor import PatchAttempt, TokenUsage
from verdict.work_unit import WorkUnit

if TYPE_CHECKING:
    from verdict.orchestration.contracts import WorkerTerminal

#: Seed limits (design Open point 3): files in the attempt tree and total bytes read from it.
MAX_SEED_FILES = 50_000
MAX_SEED_BYTES = 512 * 1024 * 1024
#: The largest single file the executor transfers, in either direction.
MAX_TRANSFER_FILE_BYTES = 1024 * 1024
#: One OpenCode attempt. OpenCode retries an upstream 429 or 503 for 85 to 100 s before it prints the
#: status (#1338), so a timeout below 120 s would lose it; the signed assignment lives ``ceil(timeout)
#: + 60`` s and is refused above 1,800 s (#1339), which caps the timeout at 1,740 s.
ATTEMPT_TIMEOUT_SECONDS = 600.0
MIN_ATTEMPT_TIMEOUT_SECONDS = 120.0
MAX_ATTEMPT_TIMEOUT_SECONDS = 1740.0

#: Hooks are a test seam: a mapping from a phase name to a callable run at that point.
Hooks = Mapping[str, Callable[..., None]]

_DIR_FLAGS = (
    getattr(os, "O_RDONLY", 0)
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0)
)
_FILE_FLAGS = (
    getattr(os, "O_RDONLY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_NONBLOCK", 0)
    | getattr(os, "O_CLOEXEC", 0)
)
_TEMP_PREFIX = ".verdict-transfer-"
_READ_CHUNK = 1024 * 1024


class AttemptRefusedError(Exception):
    """The attempt is ``rejected``: Verdict refused it and the user's repository is untouched."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class AttemptFailedError(Exception):
    """The attempt is ``error``: it could not be completed (a seed, a worker or a transfer failure)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class _ConflictError(Exception):
    """The user's tree no longer matches the seed (or a held directory moved)."""


class _RacedError(Exception):
    """A path changed kind between the metadata read and the open."""


class _OverLimitError(Exception):
    def __init__(self, size: int) -> None:
        super().__init__(f"{size} bytes")
        self.size = size


def _hook(hooks: Hooks | None, name: str, *args: Any) -> None:
    if hooks is not None and name in hooks:
        hooks[name](*args)


def _missing_operations() -> list[str]:
    missing = [
        fn.__name__
        for fn in (
            os.open,
            os.stat,
            os.mkdir,
            os.rename,
            os.unlink,
            os.rmdir,
            os.readlink,
            os.symlink,
        )
        if fn not in os.supports_dir_fd
    ]
    if os.scandir not in os.supports_fd:
        missing.append("scandir")
    for flag in ("O_DIRECTORY", "O_NOFOLLOW", "O_NONBLOCK"):
        if not hasattr(os, flag):
            missing.append(flag)
    return sorted(set(missing))


#: Decided once at import, so a test that replaces one ``os`` function cannot change the answer.
_MISSING_OPERATIONS = _missing_operations()


def require_descriptor_operations() -> None:
    """Refuse to run where the descriptor-relative operations this module needs are missing."""
    if _MISSING_OPERATIONS:
        raise AttemptFailedError(
            "this host lacks the descriptor-relative file operations the isolated executor "
            f"needs: {', '.join(_MISSING_OPERATIONS)}"
        )


# --------------------------------------------------------------------------------------------
# entries, manifests, paths
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Entry:
    """One non-directory path: kind, executable bit and content digest (the comparison key)."""

    kind: str  # "file" | "symlink" | "other" | "dir" (a directory is only ever a *state*)
    executable: bool
    digest: str  # sha256 hex of the bytes (file) or of the link target (symlink)
    mode: int = field(default=0, compare=False)  # permission bits, kept to restore a file exactly
    size: int = field(default=0, compare=False)


@dataclass
class AttemptTree:
    """A seeded attempt tree, its manifest M0 and its device and inode.

    The worker can rename the directories above the tree and put a link where one was, so a read
    opens the tree by path once and accepts only the directory with this identity.  ``root_identity``
    is the device and inode of the user's repository directory the seed opened to read the user's
    files: the transfer and restore reopen the repository by pathname and act only on that directory.
    """

    path: Path
    manifest: dict[str, Entry]
    identity: tuple[int, int]
    root_identity: tuple[int, int]


@dataclass(frozen=True)
class DeltaItem:
    path: str
    kind: str  # "write" | "delete"
    data: bytes = b""
    executable: bool = False
    digest: str = ""


@dataclass(frozen=True)
class Delta:
    items: tuple[DeltaItem, ...]

    @property
    def paths(self) -> tuple[str, ...]:
        return tuple(item.path for item in self.items)


def validate_relpath(path: str) -> list[str]:
    """The components of ``path``, or :class:`AttemptFailedError` for one that is unsafe.

    A component that is empty, ``.``, ``..`` or ``.git``, an absolute path and an embedded NUL are
    refused: a path such as ``.git/hooks/pre-commit`` must never be written by a transfer.
    """
    if not path or path.startswith("/") or "\0" in path:
        raise AttemptFailedError(f"unsafe path {path!r}")
    parts = path.split("/")
    for part in parts:
        if part in ("", ".", "..", ".git"):
            raise AttemptFailedError(f"unsafe path component {part!r} in {path!r}")
    return parts


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _open_dir_at(parent_fd: int, name: str) -> int:
    return os.open(name, _DIR_FLAGS, dir_fd=parent_fd)


def _hash_fd(fd: int, *, keep: bool, limit: int | None) -> tuple[str, int, bytes]:
    """Read ``fd`` to the end once: (sha256, size, bytes when ``keep``).  ``_OverLimitError`` past ``limit``."""
    digest = hashlib.sha256()
    chunks: list[bytes] = []
    size = 0
    while True:
        chunk = os.read(fd, _READ_CHUNK)
        if not chunk:
            break
        size += len(chunk)
        if limit is not None and size > limit:
            raise _OverLimitError(size)
        digest.update(chunk)
        if keep:
            chunks.append(chunk)
    return digest.hexdigest(), size, b"".join(chunks)


def _state_at(
    parent_fd: int,
    name: str,
    *,
    keep: bool = False,
    limit: int | None = None,
    hooks: Hooks | None = None,
) -> tuple[Entry | None, bytes]:
    """The state of ``name`` under ``parent_fd``, never following a link and never opening a
    non-regular file.  ``(None, b"")`` when it is absent.  ``_RacedError`` if its kind changes under us.
    """
    try:
        st = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None, b""
    if stat.S_ISDIR(st.st_mode):
        return Entry("dir", False, ""), b""
    if stat.S_ISLNK(st.st_mode):
        try:
            target = os.readlink(name, dir_fd=parent_fd)
        except OSError as exc:
            raise _RacedError from exc
        return Entry("symlink", False, _sha256(os.fsencode(target))), b""
    if not stat.S_ISREG(st.st_mode):
        return Entry("other", False, ""), b""
    _hook(hooks, "after_metadata", name)
    try:
        fd = os.open(name, _FILE_FLAGS, dir_fd=parent_fd)
    except OSError as exc:
        raise _RacedError from exc
    try:
        fst = os.fstat(fd)
        if not stat.S_ISREG(fst.st_mode) or (fst.st_dev, fst.st_ino) != (st.st_dev, st.st_ino):
            raise _RacedError
        digest, size, data = _hash_fd(fd, keep=keep, limit=limit)
    finally:
        os.close(fd)
    entry = Entry(
        "file", bool(fst.st_mode & stat.S_IXUSR), digest, mode=stat.S_IMODE(fst.st_mode), size=size
    )
    return entry, data


# --------------------------------------------------------------------------------------------
# git
# --------------------------------------------------------------------------------------------

Runner = Callable[..., Any]


def _git_env(*, literal: bool) -> dict[str, str]:
    env = dict(os.environ)
    env.update(
        {
            "GIT_OPTIONAL_LOCKS": "0",  # a read must not refresh the index it reads
            "GIT_TERMINAL_PROMPT": "0",
            "LC_ALL": "C",
        }
    )
    if literal:
        env["GIT_LITERAL_PATHSPECS"] = "1"
    else:
        env.pop("GIT_LITERAL_PATHSPECS", None)  # git check-ignore refuses the literal magic
    return env


def run_git(
    repo: Path,
    args: Sequence[str],
    *,
    stdin: bytes | None = None,
    runner: Runner = subprocess.run,
    isolated: bool = False,
    literal: bool = True,
) -> Any:
    """Run ``git`` in ``repo`` and return the completed process (bytes output, never raises).

    ``isolated`` adds ``-c core.hooksPath=/dev/null`` for a git process that works in an attempt
    tree; Verdict never runs git in such a tree after the worker started (D4).
    """
    argv = ["git"]
    if isolated:
        argv += ["-c", "core.hooksPath=/dev/null"]
    argv += list(args)
    return runner(
        argv,
        cwd=str(repo),
        input=stdin,
        capture_output=True,
        env=_git_env(literal=literal),
        check=False,
    )


@dataclass(frozen=True)
class StatusRecord:
    code: str
    path: str
    source: str | None = None


def parse_status_z(raw: bytes) -> list[StatusRecord]:
    """Parse ``git status --porcelain=v1 -z``: a rename or copy is two records, destination first."""
    parts = raw.split(b"\0")
    records: list[StatusRecord] = []
    index = 0
    while index < len(parts):
        part = parts[index]
        index += 1
        if not part:
            continue
        code = part[:2].decode("ascii", "replace")
        path = os.fsdecode(part[3:])
        source: str | None = None
        if ("R" in code or "C" in code) and index < len(parts):
            source = os.fsdecode(parts[index])
            index += 1
        records.append(StatusRecord(code, path, source))
    return records


def _failure_text(done: Any) -> str:
    err = getattr(done, "stderr", b"") or b""
    text = err.decode("utf-8", "replace") if isinstance(err, bytes) else str(err)
    return text.strip()[-300:] or f"exit status {getattr(done, 'returncode', '?')}"


# --------------------------------------------------------------------------------------------
# the manifest walk
# --------------------------------------------------------------------------------------------


def _walk_dir(
    dir_fd: int,
    prefix: str,
    manifest: dict[str, Entry],
    totals: list[int],
    *,
    top: bool,
    hooks: Hooks | None,
    limit_error: Callable[[str], Exception],
) -> None:
    with os.scandir(dir_fd) as scanner:
        entries = sorted(scanner, key=lambda item: item.name)
    for item in entries:
        name = item.name
        if top and name == ".git":
            continue
        rel = prefix + name
        try:
            st = item.stat(follow_symlinks=False)
        except FileNotFoundError as exc:
            raise AttemptFailedError("the attempt tree changed during validation") from exc
        if stat.S_ISDIR(st.st_mode):
            try:
                child = _open_dir_at(dir_fd, name)
            except OSError as exc:
                raise AttemptFailedError("the attempt tree changed during validation") from exc
            try:
                cst = os.fstat(child)
                if (cst.st_dev, cst.st_ino) != (st.st_dev, st.st_ino):
                    raise AttemptFailedError("the attempt tree changed during validation")
                _walk_dir(
                    child,
                    rel + "/",
                    manifest,
                    totals,
                    top=False,
                    hooks=hooks,
                    limit_error=limit_error,
                )
            finally:
                os.close(child)
            continue
        totals[0] += 1
        if totals[0] > MAX_SEED_FILES:
            raise limit_error(f"the tree holds more than {MAX_SEED_FILES} files (limit)")
        if stat.S_ISREG(st.st_mode):
            _hook(hooks, "before_open", rel)
            try:
                # The read stops at what is left of the tree's byte budget: a worker-made file that
                # is huge, or still growing, is never hashed to its end.
                entry, _ = _state_at(dir_fd, name, limit=MAX_SEED_BYTES - totals[1], hooks=hooks)
            except _RacedError as exc:
                raise AttemptFailedError("the attempt tree changed during validation") from exc
            except _OverLimitError as exc:
                raise limit_error(
                    f"the tree holds more than {MAX_SEED_BYTES} bytes (limit)"
                ) from exc
            if entry is None or entry.kind != "file":
                raise AttemptFailedError("the attempt tree changed during validation")
            totals[1] += entry.size
            manifest[rel] = entry
        elif stat.S_ISLNK(st.st_mode):
            try:
                manifest[rel] = Entry(
                    "symlink", False, _sha256(os.fsencode(os.readlink(name, dir_fd=dir_fd)))
                )
            except OSError as exc:
                raise AttemptFailedError("the attempt tree changed during validation") from exc
        else:
            manifest[rel] = Entry("other", False, "")


def walk_manifest(
    root: Path | int, *, hooks: Hooks | None = None, limit_error: Callable[[str], Exception]
) -> dict[str, Entry]:
    """Every non-directory path under ``root`` (outside the top-level ``.git``), by descriptor.

    ``root`` is a directory path, opened here without following a link in its last component, or a
    directory descriptor the caller holds and keeps open (the attempt tree is walked that way, so the
    walk and the reads that follow it use one open of the tree).
    """
    require_descriptor_operations()
    manifest: dict[str, Entry] = {}
    if isinstance(root, int):
        _walk_dir(root, "", manifest, [0, 0], top=True, hooks=hooks, limit_error=limit_error)
        return manifest
    try:
        root_fd = os.open(str(root), _DIR_FLAGS)
    except OSError as exc:
        raise AttemptFailedError(f"the attempt tree cannot be read ({type(exc).__name__})") from exc
    try:
        _walk_dir(root_fd, "", manifest, [0, 0], top=True, hooks=hooks, limit_error=limit_error)
    finally:
        os.close(root_fd)
    return manifest


# --------------------------------------------------------------------------------------------
# 2.1 seed
# --------------------------------------------------------------------------------------------


def _read_user_file(root_fd: int, rel: str, limit: int) -> tuple[str, Any]:
    """The user's file at ``rel`` for the seed: ``("absent", None)``, ``("file", (bytes, mode))``,
    ``("symlink", target)`` or ``("dir"|"other", None)``.  Directories are opened without
    following links, so a symlinked parent is "absent" for a seed (git never lists below one).

    A file is read at most to ``limit`` bytes, what is left of the seed's one byte budget: the seed
    keeps every file it reads in memory until the clone is written, so the budget is shared.
    """
    parts = rel.split("/")
    cur = root_fd
    opened: list[int] = []
    try:
        for name in parts[:-1]:
            try:
                cur = _open_dir_at(cur, name)
            except OSError:
                return "absent", None
            opened.append(cur)
        try:
            st = os.stat(parts[-1], dir_fd=cur, follow_symlinks=False)
        except FileNotFoundError:
            return "absent", None
        if stat.S_ISLNK(st.st_mode):
            return "symlink", os.readlink(parts[-1], dir_fd=cur)
        if stat.S_ISDIR(st.st_mode):
            return "dir", None
        if not stat.S_ISREG(st.st_mode):
            return "other", None
        try:
            entry, data = _state_at(cur, parts[-1], keep=True, limit=limit)
        except _RacedError as exc:
            raise AttemptFailedError(f"{rel} changed while seeding") from exc
        except _OverLimitError as exc:
            raise AttemptFailedError(
                f"seed refused: the files to copy hold more than {MAX_SEED_BYTES} bytes (limit)"
            ) from exc
        if entry is None or entry.kind != "file":
            return "absent", None
        return "file", (data, entry.mode)
    finally:
        for fd in opened:
            os.close(fd)


def _open_removable_dir(parent_fd: int, name: str) -> int | None:
    """Open the directory ``name`` under ``parent_fd`` without following a link; ``None`` when it is
    absent, not a real directory, or cannot be made readable.

    A directory the worker made unreadable is ours (the same user), so it is given back owner access
    by name relative to ``parent_fd`` and with no link followed, where this host can do that; where it
    cannot, the directory is left.
    """
    try:
        return _open_dir_at(parent_fd, name)
    except PermissionError:
        try:
            os.chmod(name, 0o700, dir_fd=parent_fd, follow_symlinks=False)
            return _open_dir_at(parent_fd, name)
        except (OSError, NotImplementedError, ValueError):
            return None
    except OSError:
        return None  # absent, a link (ELOOP) or not a directory (ENOTDIR)


def _empty_directory(dir_fd: int, *, strict: bool) -> None:
    """Remove everything under the directory ``dir_fd``, descending by descriptor and following no link.

    ``strict`` raises the first ``OSError``; otherwise an entry that cannot be removed is left.
    """
    with contextlib.suppress(OSError):
        os.fchmod(dir_fd, 0o700)  # a 0o555 directory cannot lose entries until its owner says so
    try:
        with os.scandir(dir_fd) as scanner:
            names = [item.name for item in scanner]
    except OSError:
        if strict:
            raise
        return
    for name in names:
        try:
            _remove_entry(dir_fd, name, strict=strict)
        except OSError:
            if strict:
                raise


def _remove_entry(parent_fd: int, name: str, *, strict: bool) -> None:
    """Remove ``name`` under ``parent_fd``: a file or link is unlinked, never followed; a directory is
    emptied (see :func:`_empty_directory`) and removed.  Absent is fine.  ``strict`` as above.
    """
    try:
        os.unlink(name, dir_fd=parent_fd)
        return
    except FileNotFoundError:
        return
    except OSError as exc:  # a directory is EISDIR on Linux and EPERM elsewhere
        failure = exc
    child = _open_removable_dir(parent_fd, name)
    if child is None:
        if strict:
            raise failure
        return
    try:
        _empty_directory(child, strict=strict)
    finally:
        os.close(child)
    try:
        os.rmdir(name, dir_fd=parent_fd)
    except FileNotFoundError:
        pass
    except OSError:
        if strict:
            raise


def _remove_in_tree(tree_fd: int, rel: str) -> None:
    """Remove ``rel`` from the clone open at ``tree_fd``; every ancestor is opened without following a
    link, so a committed symlink in the clone can never route the removal outside it.

    An ancestor that is absent, a link or a file means nothing below it is part of the clone's tree:
    there is nothing to remove, and the overlay replaces that ancestor if the user's tree has a
    directory there.
    """
    parts = rel.split("/")
    opened: list[int] = []
    cur = tree_fd
    try:
        for name in parts[:-1]:
            try:
                cur = _open_dir_at(cur, name)
            except OSError:
                return
            opened.append(cur)
        _remove_entry(cur, parts[-1], strict=True)
    finally:
        for fd in opened:
            os.close(fd)


def _copy_into_tree(tree_fd: int, rel: str, kind: str, payload: Any) -> None:
    """Write the user's ``rel`` into the clone open at ``tree_fd``, relative to descriptors held from
    the root and following no link; a non-directory in the way of an ancestor is replaced."""
    parts = rel.split("/")
    opened: list[int] = []
    cur = tree_fd
    try:
        for name in parts[:-1]:
            try:
                st = os.stat(name, dir_fd=cur, follow_symlinks=False)
            except FileNotFoundError:
                os.mkdir(name, 0o777, dir_fd=cur)
            else:
                if not stat.S_ISDIR(st.st_mode):
                    _remove_entry(cur, name, strict=True)
                    os.mkdir(name, 0o777, dir_fd=cur)
            cur = _open_dir_at(cur, name)
            opened.append(cur)
        final = parts[-1]
        _remove_entry(cur, final, strict=True)
        if kind == "symlink":
            os.symlink(payload, final, dir_fd=cur)
            return
        data, mode = payload
        fd = os.open(
            final,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
            0o600,
            dir_fd=cur,
        )
        try:
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view) :]
            os.fchmod(fd, mode & 0o7777 or 0o644)
        finally:
            os.close(fd)
    finally:
        for fd in opened:
            os.close(fd)


def _close_quietly(fd: int) -> None:
    """Close ``fd`` in cleanup that runs while another exception propagates (or on every exit): a close
    that fails is not worth replacing that exception, and the caller's other closes still run."""
    with contextlib.suppress(OSError):
        os.close(fd)


def remove_tree(path: Path) -> None:
    """Remove a directory Verdict made before any worker could reach it (a failed seed).  Never raises
    an ``Exception``; see :class:`AttemptDirectory` for the one a worker has run in."""
    try:
        parent_fd = os.open(str(path.parent), _DIR_FLAGS)
    except OSError:
        return
    try:
        with contextlib.suppress(Exception):  # best effort; a very deep tree raises RecursionError
            _remove_entry(parent_fd, path.name, strict=False)
    finally:
        _close_quietly(parent_fd)


class AttemptDirectory:
    """The private directory one attempt lives in, held by descriptor from the moment it is made.

    The worker runs inside it as the same user, so it can rename this directory or leave a link where
    it was.  Cleanup therefore never walks the pathname: it empties the directory through the
    descriptor opened before the worker started, and removes the name only when the entry there is
    still the directory it made (its device and inode).  A directory the worker moved away is emptied
    and left in place.
    """

    def __init__(self, temp_root: Path | None) -> None:
        self.path = Path(
            os.path.abspath(tempfile.mkdtemp(prefix="verdict-autodev-oc-", dir=temp_root))
        )
        self._root_fd = -1
        self._fd = -1
        try:
            # The temporary directory is the operator's, not the worker's: it may itself be a link.
            self._root_fd = os.open(
                str(self.path.parent),
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0),
            )
            self._fd = _open_dir_at(self._root_fd, self.path.name)
            made = os.fstat(self._fd)
            self._identity = (made.st_dev, made.st_ino)
        except BaseException:
            for fd in (self._fd, self._root_fd):
                if fd >= 0:
                    _close_quietly(fd)
            self._fd = self._root_fd = -1
            with contextlib.suppress(OSError):
                os.rmdir(self.path)
            raise

    def remove(self) -> None:
        """Empty the directory and remove its name; idempotent, and never raises an ``Exception``."""
        fd, root_fd = self._fd, self._root_fd
        if fd < 0:
            return
        self._fd = self._root_fd = -1
        try:
            with contextlib.suppress(
                Exception
            ):  # best effort; a very deep tree raises RecursionError
                _empty_directory(fd, strict=False)
                try:
                    at_name = os.stat(self.path.name, dir_fd=root_fd, follow_symlinks=False)
                except OSError:
                    return  # moved away: only the emptied directory remains
                same = (at_name.st_dev, at_name.st_ino) == self._identity
                if stat.S_ISDIR(at_name.st_mode) and same:
                    with contextlib.suppress(OSError):
                        os.rmdir(self.path.name, dir_fd=root_fd)
        finally:
            _close_quietly(fd)
            _close_quietly(root_fd)


def seed_attempt_tree(repo: Path, parent_dir: Path, owned_files: Iterable[str]) -> AttemptTree:
    """Make the attempt tree ``parent_dir/tree`` from ``repo``'s current state (D2, D3).

    An unshared ``--no-local`` clone with its remote removed, then the user's modified, deleted,
    staged, renamed and untracked files copied over it (never git objects), then every owned file
    copied from the user's working tree whatever git says (its checkout bytes can differ).  The
    manifest M0 is taken last.  ``AttemptFailedError`` names why the seed was refused; the tree is
    removed first.
    """
    require_descriptor_operations()
    repo = Path(repo)
    owned = tuple(owned_files)
    for rel in owned:
        validate_relpath(rel)
    tree = Path(parent_dir) / "tree"
    try:
        return _seed(repo, tree, owned)
    except BaseException:
        remove_tree(tree)
        raise


def _seed(repo: Path, tree: Path, owned: tuple[str, ...]) -> AttemptTree:
    head = run_git(repo, ["rev-parse", "--verify", "-q", "HEAD^{commit}"])
    if head.returncode != 0:
        raise AttemptFailedError("the repository has no HEAD commit (commit something first)")
    listing = run_git(repo, ["ls-files", "-s", "-z"])
    if listing.returncode != 0:
        raise AttemptFailedError(f"git ls-files failed: {_failure_text(listing)}")
    for record in listing.stdout.split(b"\0"):
        if record.startswith(b"160000 "):
            name = os.fsdecode(record.split(b"\t", 1)[-1])
            raise AttemptFailedError(f"the repository contains a submodule ({name}); not supported")
    status = run_git(repo, ["status", "--porcelain=v1", "-z", "--untracked-files=all"])
    if status.returncode != 0:
        raise AttemptFailedError(f"git status failed: {_failure_text(status)}")
    records = parse_status_z(status.stdout)
    for record in records:
        if record.path.endswith("/"):
            raise AttemptFailedError(
                f"the repository contains a nested repository ({record.path}); not supported"
            )
        validate_relpath(record.path)
        if record.source is not None:
            validate_relpath(record.source)

    clone = run_git(
        tree.parent,
        [
            "clone",
            "--no-local",
            "--no-hardlinks",
            "-q",
            "-c",
            "core.hooksPath=/dev/null",
            str(repo),
            str(tree),
        ],
        isolated=True,
    )
    if clone.returncode != 0:
        raise AttemptFailedError(f"git clone of the repository failed: {_failure_text(clone)}")
    removed = run_git(tree, ["remote", "remove", "origin"], isolated=True)
    if removed.returncode != 0:
        raise AttemptFailedError(f"removing the clone's remote failed: {_failure_text(removed)}")

    root_fd = os.open(str(repo), _DIR_FLAGS)
    try:
        # The one directory the seed reads the user's files from; the transfer and restore act on it only.
        root_identity = _identities([root_fd])[0]
        removals: list[str] = []
        copies: list[str] = []
        for record in records:
            copies.append(record.path)
            if record.source is not None:
                removals.append(record.source)
        copies.extend(owned)
        loaded: dict[str, tuple[str, Any]] = {}
        budget = MAX_SEED_BYTES  # one byte budget for every file kept in memory, not one per file
        kept = 0
        for rel in dict.fromkeys(copies + removals):
            kind, payload = _read_user_file(root_fd, rel, budget)
            if kind in ("file", "symlink"):  # an absent path (a deletion) keeps nothing
                kept += 1
                if kept > MAX_SEED_FILES:
                    raise AttemptFailedError(
                        f"seed refused: more than {MAX_SEED_FILES} files to copy (limit)"
                    )
            if kind == "file":
                budget -= len(payload[0])
            loaded[rel] = (kind, payload)
        tree_fd = os.open(str(tree), _DIR_FLAGS)
        try:
            made = os.fstat(tree_fd)
            for rel in dict.fromkeys(removals + copies):
                kind, _payload = loaded[rel]
                if kind == "absent":
                    _seed_step(_remove_in_tree, tree_fd, rel)
            for rel in dict.fromkeys(copies):
                kind, payload = loaded[rel]
                if kind in ("file", "symlink"):
                    _seed_step(_copy_into_tree, tree_fd, rel, kind, payload)
                elif kind == "other":
                    raise AttemptFailedError(
                        f"{rel} is not a regular file or a symlink; not supported"
                    )
            manifest = walk_manifest(
                tree_fd, limit_error=lambda reason: AttemptFailedError(f"seed refused: {reason}")
            )
        finally:
            os.close(tree_fd)
    finally:
        os.close(root_fd)
    return AttemptTree(tree, manifest, (made.st_dev, made.st_ino), root_identity)


def _seed_step(step: Callable[..., None], tree_fd: int, rel: str, *args: Any) -> None:
    try:
        step(tree_fd, rel, *args)
    except OSError as exc:
        raise AttemptFailedError(f"seeding {rel} failed ({describe_failure(exc)})") from exc


# --------------------------------------------------------------------------------------------
# 2.2 delta
# --------------------------------------------------------------------------------------------


def _ignored_paths(repo: Path, paths: Sequence[str]) -> set[str]:
    """The subset of ``paths`` the user's own ignore rules match (read in the user's repository)."""
    if not paths:
        return set()
    payload = b"\0".join(os.fsencode(path) for path in paths) + b"\0"
    done = run_git(
        repo, ["check-ignore", "--no-index", "-z", "--stdin"], stdin=payload, literal=False
    )
    if done.returncode == 1:
        return set()
    if done.returncode != 0:
        raise AttemptFailedError(f"git check-ignore failed: {_failure_text(done)}")
    return {os.fsdecode(part) for part in done.stdout.split(b"\0") if part}


def _read_delta_file(root_fd: int, rel: str, hooks: Hooks | None) -> tuple[Entry, bytes]:
    """Read one delta file from the attempt tree: every ancestor opened ``O_NOFOLLOW`` from the root."""
    parts = rel.split("/")
    _hook(hooks, "before_open", rel)
    opened: list[int] = []
    cur = root_fd
    try:
        for name in parts[:-1]:
            try:
                cur = _open_dir_at(cur, name)
            except OSError as exc:
                raise AttemptFailedError("the attempt tree changed during validation") from exc
            opened.append(cur)
        try:
            entry, data = _state_at(
                cur, parts[-1], keep=True, limit=MAX_TRANSFER_FILE_BYTES, hooks=hooks
            )
        except _RacedError as exc:
            raise AttemptFailedError("the attempt tree changed during validation") from exc
        except _OverLimitError as exc:
            raise AttemptRefusedError(
                f"{rel} is {exc.size} bytes, over the {MAX_TRANSFER_FILE_BYTES}-byte transfer limit"
            ) from exc
        if entry is None or entry.kind != "file":
            raise AttemptFailedError("the attempt tree changed during validation")
        return entry, data
    finally:
        for fd in opened:
            os.close(fd)


def attempt_delta(
    tree: AttemptTree, repo: Path, unit: WorkUnit, *, hooks: Hooks | None = None
) -> Delta:
    """What the worker changed, decided from file contents only (D4), or a refusal.

    Raises :class:`AttemptRefusedError` (out of bounds, not a regular file, empty, over a limit) or
    :class:`AttemptFailedError` (the tree changed while it was being read).  No git command runs in
    the attempt tree.
    """
    m0 = tree.manifest
    require_descriptor_operations()
    # One descriptor for the walk and for every read: the directories above the tree are the
    # worker's to rename or replace by a link, so the tree is opened once and must be the directory
    # that was seeded.
    try:
        root_fd = os.open(str(tree.path), _DIR_FLAGS)
    except OSError as exc:
        raise AttemptFailedError(f"the attempt tree cannot be read ({type(exc).__name__})") from exc
    try:
        opened = os.fstat(root_fd)
        if (opened.st_dev, opened.st_ino) != tree.identity:
            raise AttemptFailedError("the attempt tree changed during validation")
        m1 = walk_manifest(
            root_fd,
            hooks=hooks,
            limit_error=lambda reason: AttemptRefusedError(
                f"the attempt tree is over the limit: {reason}"
            ),
        )
        changed = {path for path in set(m0) | set(m1) if m0.get(path) != m1.get(path)}
        # A new path the user's rules ignore is a byproduct (a cache, a build output). A path the
        # unit owns never is: dropping it would transfer half of a repair, or reject a repair as empty.
        fresh = sorted(path for path in changed if path not in m0 and not unit.owns(path))
        dropped = _ignored_paths(Path(repo), fresh)
        delta_paths = sorted(changed - dropped)
        escaped = unit.out_of_bounds(delta_paths)
        if escaped:
            raise AttemptRefusedError(f"changed files outside the unit boundary: {list(escaped)}")
        special = [
            f"{path} ({m1[path].kind})"
            for path in delta_paths
            if path in m1 and m1[path].kind != "file"
        ]
        if special:
            raise AttemptRefusedError(f"changed paths that are not a regular file: {special}")
        if not delta_paths:
            raise AttemptRefusedError("worker made no change to the owned files")

        items: list[DeltaItem] = []
        for path in delta_paths:
            if path not in m1:
                items.append(DeltaItem(path, "delete"))
                continue
            entry, data = _read_delta_file(root_fd, path, hooks)
            if entry != m1[path]:
                raise AttemptFailedError("the attempt tree changed during validation")
            items.append(DeltaItem(path, "write", data, entry.executable, entry.digest))
    finally:
        os.close(root_fd)
    return Delta(tuple(items))


# --------------------------------------------------------------------------------------------
# 2.3 transfer, 2.4 restore
# --------------------------------------------------------------------------------------------


@dataclass
class JournalEntry:
    """One mutation of the user's tree, written before it is made (D7)."""

    op: str  # "mkdir" | "write" | "delete"
    path: str
    prior: Entry | None = None
    prior_data: bytes = b""
    intended: Entry | None = None
    dir_identity: tuple[int, int] | None = None  # (st_dev, st_ino) of a directory we created
    #: (st_dev, st_ino) of every directory between the repository root and a written or deleted path
    #: (the root itself is ``root``, not here), top first: the directories the transfer wrote through,
    #: so restore can tell them from a replacement.
    parents: tuple[tuple[int, int], ...] | None = None
    #: (st_dev, st_ino) of the repository root the transfer wrote through, set when the entry is made,
    #: whatever its op: restore reopens the repository by pathname and acts only if that is this directory.
    root: tuple[int, int] | None = None


@dataclass
class RestoreResult:
    restored: list[str] = field(default_factory=list)
    not_restored: list[tuple[str, str]] = field(default_factory=list)
    touched: bool = False

    def summary(self) -> str:
        if not self.touched:
            return ""
        parts: list[str] = []
        if self.restored:
            parts.append("owned files restored: " + ", ".join(self.restored))
        elif not self.not_restored:
            parts.append("owned files already held their prior content")
        if self.not_restored:
            parts.append(
                "not restored: " + "; ".join(f"{path} ({why})" for path, why in self.not_restored)
            )
        return "; ".join(parts)


class TransferFailedError(AttemptFailedError):
    """A transfer failed after it began writing; the reason lists what could not be rolled back."""


def describe_failure(exc: BaseException) -> str:
    """``ExceptionType: message``, then a leftover temporary file if its removal also failed."""
    text = f"{type(exc).__name__}: {exc}"
    leftover = getattr(exc, "leftover_temporary", None)
    if leftover:
        text += f"; the temporary file {leftover} could not be removed either"
    return text


def replace_file(
    parent_fd: int,
    name: str,
    data: bytes,
    mode: int,
    *,
    hooks: Hooks | None = None,
    path: str = "",
    before_rename: Callable[[], None] | None = None,
) -> None:
    """Write ``data`` to ``name`` under ``parent_fd`` through one unique temporary file.

    The temporary file is created ``O_EXCL | O_NOFOLLOW`` in the target directory, written, given
    ``mode`` and renamed over the target; on any failure before the rename it is unlinked by the
    same descriptor.  An unlink that fails is named on the exception (``leftover_temporary``) and
    never replaces it.

    ``before_rename`` is the caller's last look at the target, made after the temporary file is
    written and immediately before the rename: it raises to refuse, and the temporary file is then
    removed like on any other failure.  It narrows the window in which a change to the target is
    overwritten to the time between that look and the rename; it cannot close it, because POSIX has
    no rename that compares the target's content.
    """
    temp = f"{_TEMP_PREFIX}{os.getpid()}-{secrets.token_hex(8)}"
    fd = os.open(
        temp,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
        0o600,
        dir_fd=parent_fd,
    )
    try:
        try:
            view = memoryview(data)
            while view:
                written = os.write(fd, view)
                view = view[written:]
            os.fchmod(fd, mode & 0o7777)
        finally:
            os.close(fd)
        _hook(hooks, "after_temp_write", path)
        if before_rename is not None:
            before_rename()
        os.rename(temp, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
    except BaseException as exc:
        try:
            os.unlink(temp, dir_fd=parent_fd)
        except FileNotFoundError:
            pass
        except OSError as unlink_error:
            with contextlib.suppress(Exception):
                exc.leftover_temporary = f"{temp} ({type(unlink_error).__name__})"  # type: ignore[attr-defined]
        raise


def _compare(path: str, current: Entry | None, expected: Entry | None) -> str | None:
    if current is not None and current.kind != "file":
        return f"{path} is not a regular file in the user's tree (it is a {current.kind})"
    if current == expected:
        return None
    if expected is None:
        return f"{path} appeared after the seed"
    if current is None:
        return f"{path} was removed after the seed"
    if current.digest != expected.digest:
        return f"{path} changed since the seed"
    return f"{path} changed since the seed (its executable bit)"


def _descend_existing(root_fd: int, dirs: Sequence[str], path: str) -> tuple[list[int], bool]:
    """Open each existing directory component without following links.

    ``(fds, complete)``; ``complete`` is false when a component does not exist.  A component that
    exists but is not a real directory is a :class:`_ConflictError`.  The caller closes ``fds``.
    """
    fds: list[int] = []
    cur = root_fd
    for name in dirs:
        try:
            cur = _open_dir_at(cur, name)
        except FileNotFoundError:
            return fds, False
        except OSError as exc:
            for fd in fds:
                _close_quietly(fd)
            raise _ConflictError(f"{path}: a parent directory is not a real directory") from exc
        fds.append(cur)
    return fds, True


def _check_read_only(root_fd: int, item: DeltaItem, m0: Mapping[str, Entry]) -> None:
    parts = item.path.split("/")
    fds, complete = _descend_existing(root_fd, parts[:-1], item.path)
    try:
        current: Entry | None = None
        if complete:
            cur = fds[-1] if fds else root_fd
            try:
                current, _ = _state_at(cur, parts[-1], limit=MAX_TRANSFER_FILE_BYTES)
            except _OverLimitError as exc:
                raise _ConflictError(
                    f"{item.path} is over the {MAX_TRANSFER_FILE_BYTES}-byte limit in the user's tree"
                ) from exc
            except _RacedError as exc:
                raise _ConflictError(f"{item.path} changed kind while it was being read") from exc
        why = _compare(item.path, current, m0.get(item.path))
        if why is not None:
            raise _ConflictError(why)
    finally:
        for fd in fds:
            os.close(fd)


def _verify_held(held: Sequence[tuple[int, str, int]], path: str) -> None:
    """Each held directory descriptor must still be the directory reachable at its name."""
    for fd, name, parent in held:
        try:
            at_name = os.stat(name, dir_fd=parent, follow_symlinks=False)
        except OSError as exc:
            raise _ConflictError(f"{path}: a parent directory moved while it was in use") from exc
        held_st = os.fstat(fd)
        if not stat.S_ISDIR(at_name.st_mode) or (at_name.st_dev, at_name.st_ino) != (
            held_st.st_dev,
            held_st.st_ino,
        ):
            raise _ConflictError(f"{path}: a parent directory moved while it was in use")


def _held_chain(
    root_fd: int, fds: Sequence[int], dirs: Sequence[str]
) -> list[tuple[int, str, int]]:
    """``(descriptor, name, parent descriptor)`` for directories opened one below the other from ``root_fd``."""
    chain: list[tuple[int, str, int]] = []
    parent = root_fd
    for fd, name in zip(fds, dirs, strict=True):
        chain.append((fd, name, parent))
        parent = fd
    return chain


def _identities(fds: Sequence[int]) -> tuple[tuple[int, int], ...]:
    """``(st_dev, st_ino)`` of each directory descriptor."""
    found = []
    for fd in fds:
        info = os.fstat(fd)
        found.append((info.st_dev, info.st_ino))
    return tuple(found)


def _last_look(
    held: Sequence[tuple[int, str, int]], parent_fd: int, name: str, path: str, limit: int
) -> Entry | None:
    """The last look before a commit: the held directories are still where they were, and the
    state of ``name`` now (``_ConflictError`` when it cannot be read within ``limit``)."""
    _verify_held(held, path)
    try:
        now, _ = _state_at(parent_fd, name, limit=limit)
    except _OverLimitError as exc:
        raise _ConflictError(f"{path} is over the transfer limit in the user's tree") from exc
    except _RacedError as exc:
        raise _ConflictError(f"{path} changed kind while it was being read") from exc
    return now


def _write_item(
    root_fd: int,
    root: tuple[int, int],
    item: DeltaItem,
    m0: Mapping[str, Entry],
    journal: list[JournalEntry],
    hooks: Hooks | None,
) -> None:
    parts = item.path.split("/")
    held: list[tuple[int, str, int]] = []
    cur = root_fd
    try:
        _hook(hooks, "before_item", item.path)
        for index, name in enumerate(parts[:-1]):
            try:
                fd = _open_dir_at(cur, name)
            except FileNotFoundError:
                if item.kind == "delete":
                    raise _ConflictError(f"{item.path} was removed after the seed") from None
                entry = JournalEntry("mkdir", "/".join(parts[: index + 1]), root=root)
                journal.append(entry)
                _hook(hooks, "before_mkdir", entry.path)
                try:
                    os.mkdir(name, 0o777, dir_fd=cur)
                except FileExistsError:
                    journal.pop()
                    try:
                        fd = _open_dir_at(cur, name)
                    except OSError as exc:
                        raise _ConflictError(f"{item.path}: a parent directory appeared") from exc
                else:
                    try:
                        fd = _open_dir_at(cur, name)
                    except OSError as exc:
                        raise _ConflictError(
                            f"{item.path}: a created directory was replaced"
                        ) from exc
                    made = os.fstat(fd)
                    entry.dir_identity = (made.st_dev, made.st_ino)
                _hook(hooks, "after_mkdir", entry.path)
            except OSError as exc:
                raise _ConflictError(
                    f"{item.path}: a parent directory is not a real directory"
                ) from exc
            held.append((fd, name, cur))
            cur = fd
        _hook(hooks, "before_write", item.path)
        _verify_held(held, item.path)
        try:
            current, prior_data = _state_at(
                cur, parts[-1], keep=True, limit=MAX_TRANSFER_FILE_BYTES
            )
        except _OverLimitError as exc:
            raise _ConflictError(
                f"{item.path} is over the transfer limit in the user's tree"
            ) from exc
        except _RacedError as exc:
            raise _ConflictError(f"{item.path} changed kind while it was being read") from exc
        why = _compare(item.path, current, m0.get(item.path))
        if why is not None:
            raise _ConflictError(why)
        if item.kind == "write":
            intended = Entry("file", item.executable, item.digest)
            if (
                current is not None
                and current.kind == "file"
                and current.executable == item.executable
            ):
                mode = current.mode  # keep the user's exact permission bits
            else:
                mode = 0o755 if item.executable else 0o644
            entry = JournalEntry(
                "write",
                item.path,
                current,
                prior_data,
                intended,
                parents=_identities([fd for fd, _name, _parent in held]),
                root=root,
            )
            journal.append(entry)

            def final_check() -> None:
                now = _last_look(held, cur, parts[-1], item.path, MAX_TRANSFER_FILE_BYTES)
                why = _compare(item.path, now, current)
                if why is not None:
                    raise _ConflictError(why)

            try:
                replace_file(
                    cur,
                    parts[-1],
                    item.data,
                    mode,
                    hooks=hooks,
                    path=item.path,
                    before_rename=final_check,
                )
            except _ConflictError:
                if journal and journal[-1] is entry:
                    journal.pop()  # refused before the rename: this path was never changed
                raise
        else:
            journal.append(
                JournalEntry(
                    "delete",
                    item.path,
                    current,
                    prior_data,
                    None,
                    parents=_identities([fd for fd, _name, _parent in held]),
                    root=root,
                )
            )
            os.unlink(parts[-1], dir_fd=cur)
        _hook(hooks, "after_item", item.path)
    finally:
        for fd, _name, _parent in held:
            os.close(fd)


def transfer(
    delta: Delta,
    repo: Path,
    m0: Mapping[str, Entry],
    journal: list[JournalEntry],
    hooks: Hooks | None = None,
    *,
    root_identity: tuple[int, int],
) -> None:
    """Copy a validated delta into the user's tree, all or nothing (D6).

    The repository is reopened by pathname, and ``root_identity`` is the device and inode of the
    directory the seed read (:attr:`AttemptTree.root_identity`): when the directory reopened is not
    that one (renamed away and replaced since the seed) the whole attempt is
    :class:`AttemptRefusedError` before the first read of a path, and nothing is written or journalled.

    A read-only phase compares the user's kind, mode and bytes with M0 for every path (a
    difference is :class:`AttemptRefusedError` and nothing was created).  The write phase journals each
    mutation before it makes it and re-checks at the moment of each write.  An ``Exception`` after
    the first mutation rolls back through :func:`restore` and is :class:`TransferFailedError`; a
    ``KeyboardInterrupt`` propagates with the journal intact for the caller's ``restore()``.
    """
    require_descriptor_operations()
    for item in delta.items:
        try:
            validate_relpath(item.path)
        except AttemptFailedError as exc:
            raise AttemptRefusedError(
                f"the delta path {item.path!r} is not safe: {exc.reason}"
            ) from exc
    try:
        root_fd = os.open(str(repo), _DIR_FLAGS)
    except OSError as exc:
        raise AttemptFailedError(f"the repository cannot be opened ({type(exc).__name__})") from exc
    try:
        if _identities([root_fd])[0] != root_identity:
            raise AttemptRefusedError(
                "the repository was replaced since the seed: the directory at its path is not "
                "the one the attempt was seeded from"
            )
        root = root_identity  # the directory every write below goes through: the one the seed read
        try:
            for item in delta.items:
                _check_read_only(root_fd, item, m0)
        except _ConflictError as exc:
            raise AttemptRefusedError(str(exc)) from exc
        _hook(hooks, "after_checks")
        try:
            for item in delta.items:
                _write_item(root_fd, root, item, m0, journal, hooks)
        except Exception as exc:
            if not journal:
                if isinstance(exc, _ConflictError):
                    raise AttemptRefusedError(str(exc)) from exc
                raise AttemptFailedError(describe_failure(exc)) from exc
            reason = str(exc) if isinstance(exc, _ConflictError) else describe_failure(exc)
            result = _restore(root_fd, journal, hooks=None)
            note = result.summary()
            if result.not_restored:
                raise TransferFailedError(f"{reason}; rolled back, but {note}") from exc
            raise TransferFailedError(f"{reason}; rolled back ({note})") from exc
    finally:
        os.close(root_fd)


def _restore_directory(
    root_fd: int, entry: JournalEntry, result: RestoreResult, parts: list[str]
) -> None:
    try:
        fds, complete = _descend_existing(root_fd, parts[:-1], entry.path)
    except _ConflictError as exc:
        result.not_restored.append((entry.path, str(exc).split(": ", 1)[-1]))
        return
    try:
        if not complete:
            return  # the parent is gone, so the directory is too
        pfd = fds[-1] if fds else root_fd
        name = parts[-1]
        try:
            st = os.stat(name, dir_fd=pfd, follow_symlinks=False)
        except FileNotFoundError:
            return
        if not stat.S_ISDIR(st.st_mode):
            result.not_restored.append((entry.path, "is no longer a directory"))
            return
        try:
            probe = _open_dir_at(pfd, name)
        except OSError:
            result.not_restored.append((entry.path, "is no longer a real directory"))
            return
        try:
            now = os.fstat(probe)
        finally:
            os.close(probe)
        if entry.dir_identity is not None and (now.st_dev, now.st_ino) != entry.dir_identity:
            result.not_restored.append((entry.path, "replaced by another directory"))
            return
        try:
            os.rmdir(name, dir_fd=pfd)
        except OSError as exc:
            why = "not empty" if exc.errno in (errno.ENOTEMPTY, errno.EEXIST) else str(exc.strerror)
            result.not_restored.append((entry.path, why))
            return
        result.restored.append(entry.path + "/")
    finally:
        for fd in fds:
            os.close(fd)


def _restore_file(
    root_fd: int, entry: JournalEntry, result: RestoreResult, parts: list[str], hooks: Hooks | None
) -> None:
    try:
        fds, complete = _descend_existing(root_fd, parts[:-1], entry.path)
    except _ConflictError as exc:
        result.not_restored.append((entry.path, str(exc).split(": ", 1)[-1]))
        return
    try:
        if not complete:
            # The directory the transfer wrote through is no longer there: the file is wherever that
            # directory went, and the path cannot be put back from here.
            result.not_restored.append((entry.path, "its directory is gone"))
            return
        # Before the state is read: a replacement directory can hold the very bytes this entry
        # expects, and would pass for the one the transfer wrote through.
        if entry.parents is None or len(entry.parents) != len(fds):
            result.not_restored.append((entry.path, "its parent directories were not recorded"))
            return
        if _identities(fds) != entry.parents:
            result.not_restored.append((entry.path, "a parent directory was replaced"))
            return
        pfd = fds[-1] if fds else root_fd
        name = parts[-1]
        try:
            current, _ = _state_at(pfd, name, limit=MAX_TRANSFER_FILE_BYTES * 2)
        except (_RacedError, _OverLimitError):
            result.not_restored.append((entry.path, "changed since the transfer"))
            return
        if entry.op == "write":
            if current == entry.prior:
                return  # the mutation was never made
            if current != entry.intended:
                result.not_restored.append((entry.path, "changed since the transfer"))
                return
        else:  # delete
            if current == entry.prior:
                return
            if current is not None:
                result.not_restored.append((entry.path, "changed since the transfer"))
                return
        held = _held_chain(root_fd, fds, parts[:-1])

        def final_check() -> None:
            now = _last_look(held, pfd, name, entry.path, MAX_TRANSFER_FILE_BYTES * 2)
            if now != current:
                raise _ConflictError("changed since the transfer")

        try:
            if entry.prior is None:
                os.unlink(name, dir_fd=pfd)
            else:
                replace_file(
                    pfd,
                    name,
                    entry.prior_data,
                    entry.prior.mode or 0o644,
                    hooks=hooks,
                    path=entry.path,
                    before_rename=final_check,
                )
        except _ConflictError as exc:
            result.not_restored.append((entry.path, str(exc).split(": ", 1)[-1]))
            return
        except OSError as exc:
            result.not_restored.append((entry.path, describe_failure(exc)))
            return
        result.restored.append(entry.path)
    finally:
        for fd in fds:
            os.close(fd)


def _root_refusal(root_fd: int, journal: Sequence[JournalEntry]) -> str | None:
    """Why restore must not touch ``root_fd``'s tree for ``journal``, or ``None`` when it is the
    directory every entry was written through."""
    here = _identities([root_fd])[0]
    if any(entry.root is None for entry in journal):
        return "the repository directory was not recorded"
    if any(entry.root != here for entry in journal):
        return "the repository was replaced: it is not the directory the transfer wrote through"
    return None


def _restore(root_fd: int, journal: list[JournalEntry], *, hooks: Hooks | None) -> RestoreResult:
    result = RestoreResult(touched=bool(journal))
    refusal = _root_refusal(root_fd, journal)
    if refusal is not None:
        # Nothing is read or written: every path is named with the one reason, and the journal is spent.
        result.not_restored.extend((entry.path, refusal) for entry in journal)
        journal.clear()
        return result
    while journal:
        # The entry leaves the journal only once it has been dealt with, so an interrupt in the
        # middle of one leaves it there for a second restore (which finds it done or does it).
        entry = journal[-1]
        parts = entry.path.split("/")
        try:
            if entry.op == "mkdir":
                _restore_directory(root_fd, entry, result, parts)
            else:
                _restore_file(root_fd, entry, result, parts, hooks)
        except Exception as exc:
            # A read or write failure on one path (a permission error on its state read, say) is that
            # path's result: the paths still in the journal are restored all the same.
            result.not_restored.append((entry.path, describe_failure(exc)))
        journal.pop()
    return result


def restore_transfer(
    journal: list[JournalEntry], repo: Path, hooks: Hooks | None = None
) -> RestoreResult:
    """Undo the journalled mutations in reverse, leaving alone content the transfer did not write.

    "Leaving alone" is what a last look before each write can see; a change that lands after it and
    before the rename is overwritten (the stated limit in the change's spec and design D6).

    For each entry a path holding the intended state is returned to its prior state; a path
    already holding the prior state is left and not reported (the mutation was never made);
    anything else is left alone and named, and so is a path whose parent directories are no longer
    the ones the transfer wrote through (compared by device and inode before the file is read).
    The repository is reopened by pathname, so its own device and inode are compared first with the
    one every entry recorded: when it is not that directory (renamed away and replaced), nothing is
    read or written and every journalled path is named with one reason.
    A created directory is removed only when empty and still the directory the transfer created.
    A read or write failure on one path is that path's ``not_restored`` entry and the loop goes
    on.  The journal is emptied, so calling again is a no-op; an interrupt (``KeyboardInterrupt``)
    leaves the entry in progress and those before it in the journal, and a second call continues
    from there.
    """
    if not journal:
        return RestoreResult()
    require_descriptor_operations()
    try:
        root_fd = os.open(str(repo), _DIR_FLAGS)
    except OSError as exc:
        pending = [entry.path for entry in journal]
        journal.clear()
        result = RestoreResult(touched=True)
        result.not_restored.extend(
            (path, f"repository unreadable ({type(exc).__name__})") for path in pending
        )
        return result
    try:
        return _restore(root_fd, journal, hooks=hooks)
    finally:
        os.close(root_fd)


# --------------------------------------------------------------------------------------------
# 3.1 prompt, 3.2 executor, 3.3 preflight
# --------------------------------------------------------------------------------------------


def build_opencode_prompt(unit: WorkUnit) -> str:
    """The task text the OpenCode executor puts in its instructions file for ``unit`` (D10).

    The objective, the owned files, the unit's verification command and the context pack
    (unchanged), and the rules: edit only the owned files, run the command, never commit or push.
    It reads nothing from the environment, so no credential can reach it.
    """
    owned = "\n".join(f"- {path}" for path in unit.owned_files)
    command = shlex.join(unit.verification_command)
    lines = [
        "Repair one bounded unit of work in the repository you are in (your current directory).",
        "",
        "Objective:",
        unit.objective,
        "",
        "You may edit, create or delete ONLY these files:",
        owned,
        "",
        "Every other file is out of bounds: do not edit, create or delete any other file.",
        "Do not run `git commit`, `git add`, `git push` or `git checkout`, and do not change git "
        "configuration; leave your changes in the working tree.",
        "",
        "When you are done, run this command from the repository root; it must exit 0:",
        f"    {command}",
    ]
    if unit.context.strip():
        lines += ["", "Context:", unit.context]
    return "\n".join(lines) + "\n"


class OpenCodeRunner(Protocol):
    """What the unit executor needs from ``OpenCodeExecutor``: ``run`` never raises."""

    async def run(
        self,
        prompt: str,
        *,
        route_id: str,
        cwd: Path,
        timeout_seconds: float,
        run_id: str | None = None,
        node_id: str | None = None,
    ) -> WorkerTerminal: ...


@dataclass(frozen=True)
class UnitExecutorConfig:
    """The slice of ``PatchExecutorConfig`` autodev reads: the route and the settings sent.

    Sampling fields are ``None``: OpenCode chooses them, so none is invented (spec: unknown values
    are recorded as unknown).
    """

    model: str
    base_url: str = ""
    temperature: float | None = None
    max_tokens: int | None = None


_REMEDY = "commit or stash it, or run without --worker-harness opencode"


def _status_word(code: str) -> str:
    if code == "??":
        return "is untracked"
    if "R" in code or "C" in code:
        return "is renamed or copied"
    if "D" in code:
        return "is deleted"
    if code[0] != " " and code[1] == " ":
        return "has a staged change"
    return "has uncommitted changes"


class OpenCodeUnitExecutor:
    """Run one work unit on OpenCode in an isolated attempt tree (D1, D8).

    ``execute_unit`` seeds a clone, runs the injected OpenCode executor there, takes the delta and
    transfers only validated owned-file changes.  ``restore()`` undoes the last transfer, and
    ``preflight`` refuses a unit whose owned files git cannot recover.  ``isolated`` is true: the
    gateway base-URL check is skipped for this executor and retries are refused (design D8).
    """

    isolated = True

    def __init__(
        self,
        repo: Path,
        runner: OpenCodeRunner,
        *,
        model: str,
        base_url: str = "",
        timeout_seconds: float = ATTEMPT_TIMEOUT_SECONDS,
        temp_root: Path | None = None,
        git_runner: Runner = subprocess.run,
        hooks: Hooks | None = None,
    ) -> None:
        if not (MIN_ATTEMPT_TIMEOUT_SECONDS <= timeout_seconds <= MAX_ATTEMPT_TIMEOUT_SECONDS):
            raise ValueError(
                f"the attempt timeout must be {MIN_ATTEMPT_TIMEOUT_SECONDS:g} to "
                f"{MAX_ATTEMPT_TIMEOUT_SECONDS:g} seconds"
            )
        self._repo = Path(repo)
        self._runner = runner
        self._timeout = float(timeout_seconds)
        self._temp_root = temp_root
        self._git = git_runner
        self._hooks = hooks
        self._run_id: str | None = None
        self._journal: list[JournalEntry] = []
        self.config = UnitExecutorConfig(model=model, base_url=base_url)
        self.response_headers: dict[str, str] = {}

    def begin_run(self, run_id: str) -> None:
        """Receive autodev's attempt-ledger run id, the id every OpenCode attempt is signed under."""
        self._run_id = run_id

    def restore(self) -> str:
        """Undo the last unit's transfer; ``""`` when nothing was transferred (D7)."""
        return restore_transfer(self._journal, self._repo, self._hooks).summary()

    # -- the attempt ---------------------------------------------------------------------

    def execute_unit(self, unit: WorkUnit) -> PatchAttempt:
        started = time.monotonic()
        self._journal.clear()  # this call's journal only: an earlier unit's files are verified

        def finish(
            outcome: str,
            reason: str = "",
            changed: tuple[str, ...] = (),
            usage: TokenUsage | None = None,
        ) -> PatchAttempt:
            return PatchAttempt(
                unit_id=unit.unit_id,
                model=self.config.model,
                outcome=outcome,
                reason=reason,
                changed_files=changed,
                usage=usage or TokenUsage(),
                latency_ms=int((time.monotonic() - started) * 1000),
                resolved_model=None,
            )

        if self._run_id is None:
            return finish("error", "the isolated executor was not given a run id (begin_run)")
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            return finish("error", "the isolated executor cannot run inside a running event loop")
        try:
            require_descriptor_operations()
        except AttemptFailedError as exc:
            return finish("error", exc.reason)

        attempt_dir = AttemptDirectory(self._temp_root)
        try:
            try:
                tree = seed_attempt_tree(self._repo, attempt_dir.path, unit.owned_files)
            except AttemptFailedError as exc:
                return finish("error", f"seed refused: {exc.reason}")
            terminal = asyncio.run(
                self._runner.run(
                    build_opencode_prompt(unit),
                    route_id=self.config.model,
                    cwd=tree.path,
                    timeout_seconds=self._timeout,
                    run_id=self._run_id,
                    node_id=unit.unit_id,
                )
            )
            usage = _usage(terminal)
            if not terminal.ok:
                detail = terminal.error or terminal.stop_reason or "no reason given"
                return finish("error", f"worker failed: {detail}", usage=usage)
            try:
                delta = attempt_delta(tree, self._repo, unit)
            except AttemptRefusedError as exc:
                return finish("rejected", exc.reason, usage=usage)
            except AttemptFailedError as exc:
                return finish("error", exc.reason, usage=usage)
            attempt_dir.remove()  # before the transfer, which is the last fallible step
            try:
                transfer(
                    delta,
                    self._repo,
                    tree.manifest,
                    self._journal,
                    self._hooks,
                    root_identity=tree.root_identity,
                )
            except AttemptRefusedError as exc:
                return finish("rejected", exc.reason, usage=usage)
            except AttemptFailedError as exc:
                return finish("error", exc.reason, usage=usage)
            return finish("applied", changed=delta.paths, usage=usage)
        finally:
            attempt_dir.remove()

    # -- the precondition ------------------------------------------------------------------

    def preflight(self, unit: WorkUnit) -> str | None:
        """A reason this unit must not run on OpenCode, or ``None`` (D5).

        An owned file must be tracked and unmodified, or absent: the transfer's undo information
        lives in memory, so after a crash git must hold the user's bytes.  Read-only; any failing
        read refuses the unit.
        """
        owned = list(unit.owned_files)

        def git(args: Sequence[str], **kwargs: Any) -> Any:
            return run_git(self._repo, args, runner=self._git, **kwargs)

        status = git(["status", "--porcelain=v1", "-z", "--untracked-files=all", "--", *owned])
        if status.returncode != 0:
            return f"cannot confirm the owned files are clean: git status failed ({_failure_text(status)})"
        for record in parse_status_z(status.stdout):
            for path in (record.path, record.source):
                if path in owned:
                    return f"owned file {path} {_status_word(record.code)}; {_REMEDY}"
        listing = git(["ls-files", "-v", "-z", "--", *owned])
        if listing.returncode != 0:
            return f"cannot confirm the owned files are clean: git ls-files failed ({_failure_text(listing)})"
        tags: dict[str, str] = {}
        for record_bytes in listing.stdout.split(b"\0"):
            if len(record_bytes) > 2:
                tags[os.fsdecode(record_bytes[2:])] = record_bytes[:1].decode("ascii", "replace")
        for path, tag in tags.items():
            if tag != "H":
                return (
                    f"owned file {path} is tagged {tag!r} by git ls-files -v (assume-unchanged or "
                    f"skip-worktree: git does not read its bytes); {_REMEDY}"
                )
        absent: list[str] = []
        for path in owned:
            exists = os.path.lexists(self._repo / path)
            if path not in tags and exists:
                return f"owned file {path} exists but is untracked or ignored; {_REMEDY}"
            if not exists and path not in tags:
                absent.append(path)
        if tags:
            attrs = git(["check-attr", "-z", "filter", "ident", "--", *sorted(tags)])
            if attrs.returncode != 0:
                return f"cannot confirm the owned files are clean: git check-attr failed ({_failure_text(attrs)})"
            parts = attrs.stdout.split(b"\0")
            for index in range(0, len(parts) - 2, 3):
                path, attr, value = (os.fsdecode(part) for part in parts[index : index + 3])
                if value not in ("unspecified", "unset"):
                    return (
                        f"owned file {path} has the git attribute {attr}={value} (a clean filter or "
                        f"ident keyword can hide an edit from git status); {_REMEDY}"
                    )
        if absent:
            payload = b"\0".join(os.fsencode(path) for path in absent) + b"\0"
            ignored = git(["check-ignore", "-z", "--stdin"], stdin=payload, literal=False)
            if ignored.returncode == 0:
                matched = sorted(part for part in ignored.stdout.split(b"\0") if part)
                path = os.fsdecode(matched[0]) if matched else absent[0]
                return (
                    f"owned file {path} does not exist and is matched by an ignore rule, so git "
                    "status would not show the file the unit creates; remove the ignore rule or "
                    "run without --worker-harness opencode"
                )
            if ignored.returncode != 1:
                return f"cannot confirm the owned files are not ignored: git check-ignore failed ({_failure_text(ignored)})"
        return None


def _usage(terminal: WorkerTerminal) -> TokenUsage:
    reported = terminal.usage
    if reported is None or (reported.input_tokens is None and reported.output_tokens is None):
        return TokenUsage()
    return TokenUsage(
        prompt_tokens=reported.input_tokens or 0,
        completion_tokens=reported.output_tokens or 0,
        reported=True,
    )


# --------------------------------------------------------------------------------------------
# 5.1 readiness and the executor the CLI builds
# --------------------------------------------------------------------------------------------

#: How long the readiness check waits for ``verdict serve`` and for ``opencode --version``.
SERVE_HEALTH_TIMEOUT_SECONDS = 5.0
BINARY_PROBE_TIMEOUT_SECONDS = 15.0
_SETUP_REMEDY = "run 'verdict setup --apply --allow host.serve' to create it"
_VERSION_ECHO_CHARS = 40


@dataclass(frozen=True)
class Readiness:
    """The result of the readiness check: ready, or the one check that failed and why."""

    ok: bool
    failed_check: str = ""  # "binary" | "serve" | "credentials"
    message: str = ""
    installed_version: str = ""
    pinned_version: str = ""

    @property
    def version_note(self) -> str:
        """A line naming both versions when the installed one is not the pinned one, else ``""``."""
        if self.ok and self.installed_version and self.installed_version != self.pinned_version:
            return (
                f"OpenCode {self.installed_version} is installed; this build is pinned to "
                f"{self.pinned_version}. Continuing; re-run the adapter's live check after an upgrade."
            )
        return ""


def _not_ready(check: str, why: str, remedy: str) -> Readiness:
    return Readiness(False, check, f"opencode {check} check failed: {why}; {remedy}")


def _installed_version(text: str) -> str:
    first = (text.strip().splitlines() or [""])[0].strip()
    first = "".join(ch for ch in first if ch.isprintable())
    if first[:1] in ("v", "V") and first[1:2].isdigit():
        first = first[1:]
    return first.split()[0][:_VERSION_ECHO_CHARS] if first else ""


def check_opencode_readiness(
    *,
    env: Mapping[str, str] | None = None,
    serve_token_provider: Callable[[], str | None] | None = None,
    key_provider: Callable[[], bytes] | None = None,
) -> Readiness:
    """Check, before any probe or model call, what an OpenCode unit needs (design D9).

    In order: the opencode binary resolves and runs ``--version``; ``verdict serve`` answers
    ``GET /health`` with 200 and ``"engine": "verdict"``; the serve token resolves and the worker
    signing key is readable (each read-only; no value is read into a message).  The first failure
    is returned with one message naming the check and the remedy.  A version other than the pinned
    one does not fail the check: it is carried in the result.
    """
    from verdict.http_safety import open_no_redirect
    from verdict.orchestration.executor_opencode import PINNED_VERSION, _opencode_binary, _probe_env
    from verdict.serve_service import ServeServiceError, resolve_serve_origin, resolve_serve_token
    from verdict.worker_assignment import load_assignment_key

    base_env = dict(os.environ if env is None else env)

    binary = _opencode_binary(base_env)
    resolved = shutil.which(binary) if os.sep not in binary else binary
    install = "install opencode or set VERDICT_OPENCODE_BIN to its path"
    if not resolved or not os.path.isfile(resolved) or not os.access(resolved, os.X_OK):
        return _not_ready("binary", f"not found or not executable at {binary!r}", install)
    try:
        done = subprocess.run(
            [resolved, "--version"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=BINARY_PROBE_TIMEOUT_SECONDS,
            env=_probe_env(base_env),
            check=False,
            start_new_session=True,
        )
    except subprocess.TimeoutExpired:
        return _not_ready("binary", "`--version` timed out", install)
    except OSError as exc:
        return _not_ready("binary", f"not runnable ({type(exc).__name__})", install)
    if done.returncode != 0:
        return _not_ready("binary", f"`--version` exited {done.returncode}", install)
    version = _installed_version(done.stdout or done.stderr or "")

    start = "start it with 'verdict serve' (or run 'verdict setup') and set VERDICT_SERVE_URL if it is elsewhere"
    try:
        origin = resolve_serve_origin(None, base_env)
    except ServeServiceError as exc:
        return _not_ready("serve", str(exc), "fix the address")
    try:
        request = urllib.request.Request(
            f"{origin}/health", headers={"Accept": "application/json"}, method="GET"
        )
        with open_no_redirect(request, timeout=SERVE_HEALTH_TIMEOUT_SECONDS) as response:
            status = response.status
            body = response.read(65536)
    except urllib.error.HTTPError as exc:
        return _not_ready("serve", f"{origin} answered HTTP {exc.code} to GET /health", start)
    except (urllib.error.URLError, OSError, TimeoutError, ValueError) as exc:
        return _not_ready("serve", f"{origin} is unreachable ({type(exc).__name__})", start)
    try:
        engine = json.loads(body).get("engine") if status == 200 else None
    except (ValueError, AttributeError):
        engine = None
    if status != 200 or engine != "verdict":
        return _not_ready(
            "serve",
            f"{origin} did not answer GET /health as Verdict serve (HTTP {status}, engine "
            f"{str(engine)[:40]!r})",
            start,
        )

    resolve_token = serve_token_provider or (
        lambda: (resolve_serve_token(base_env) or ("", ""))[0] or None
    )
    if not resolve_token():
        return _not_ready("credentials", "the serve token does not resolve", _SETUP_REMEDY)
    try:
        (key_provider or load_assignment_key)()
    except Exception:
        return _not_ready(
            "credentials", "the worker signing key is missing or unreadable", _SETUP_REMEDY
        )
    return Readiness(True, installed_version=version, pinned_version=PINNED_VERSION)


def build_opencode_unit_executor(repo: Path, *, model: str) -> OpenCodeUnitExecutor:
    """The one place the CLI builds an OpenCode unit executor (design D10).

    The OpenCode executor resolves its own credentials (the signing key and the serve token for
    its readiness check); this module never reads, stores or records them, so #1313 changes only
    this function.
    """
    from verdict.orchestration.executor_opencode import OpenCodeExecutor
    from verdict.serve_service import resolve_serve_origin

    return OpenCodeUnitExecutor(
        repo, OpenCodeExecutor(), model=model, base_url=resolve_serve_origin()
    )
