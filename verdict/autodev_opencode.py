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

Everything that reads a tree the worker could have written is descriptor-relative and follows no
link, so swapping a directory for a symlink cannot redirect a read or a write. POSIX only.
"""

from __future__ import annotations

import contextlib
import errno
import hashlib
import os
import secrets
import shutil
import stat
import subprocess
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from verdict.work_unit import WorkUnit

#: Seed limits (design Open point 3): files in the attempt tree and total bytes read from it.
MAX_SEED_FILES = 50_000
MAX_SEED_BYTES = 512 * 1024 * 1024
#: The largest single file the executor transfers, in either direction.
MAX_TRANSFER_FILE_BYTES = 1024 * 1024

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
        for fn in (os.open, os.stat, os.mkdir, os.rename, os.unlink, os.rmdir, os.readlink)
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
    """A seeded attempt tree and its manifest M0."""

    path: Path
    manifest: dict[str, Entry]


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
                entry, _ = _state_at(dir_fd, name, hooks=hooks)
            except _RacedError as exc:
                raise AttemptFailedError("the attempt tree changed during validation") from exc
            if entry is None or entry.kind != "file":
                raise AttemptFailedError("the attempt tree changed during validation")
            totals[1] += entry.size
            if totals[1] > MAX_SEED_BYTES:
                raise limit_error(f"the tree holds more than {MAX_SEED_BYTES} bytes (limit)")
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
    root: Path, *, hooks: Hooks | None = None, limit_error: Callable[[str], Exception]
) -> dict[str, Entry]:
    """Every non-directory path under ``root`` (outside the top-level ``.git``), by descriptor."""
    require_descriptor_operations()
    try:
        root_fd = os.open(str(root), _DIR_FLAGS)
    except OSError as exc:
        raise AttemptFailedError(f"the attempt tree cannot be read ({type(exc).__name__})") from exc
    try:
        manifest: dict[str, Entry] = {}
        _walk_dir(root_fd, "", manifest, [0, 0], top=True, hooks=hooks, limit_error=limit_error)
        return manifest
    finally:
        os.close(root_fd)


# --------------------------------------------------------------------------------------------
# 2.1 seed
# --------------------------------------------------------------------------------------------


def _read_user_file(root_fd: int, rel: str) -> tuple[str, Any]:
    """The user's file at ``rel`` for the seed: ``("absent", None)``, ``("file", (bytes, mode))``,
    ``("symlink", target)`` or ``("dir"|"other", None)``.  Directories are opened without
    following links, so a symlinked parent is "absent" for a seed (git never lists below one).
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
            entry, data = _state_at(cur, parts[-1], keep=True, limit=MAX_SEED_BYTES)
        except (_RacedError, _OverLimitError) as exc:
            raise AttemptFailedError(f"{rel} changed or is too large while seeding") from exc
        if entry is None or entry.kind != "file":
            return "absent", None
        return "file", (data, entry.mode)
    finally:
        for fd in opened:
            os.close(fd)


def _remove_in_tree(tree: Path, rel: str) -> None:
    target = tree.joinpath(*rel.split("/"))
    try:
        st = target.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISDIR(st.st_mode):
        shutil.rmtree(target, ignore_errors=True)
    else:
        target.unlink()


def _copy_into_tree(tree: Path, rel: str, kind: str, payload: Any) -> None:
    parts = rel.split("/")
    cur = tree
    for name in parts[:-1]:
        cur = cur / name
        try:
            st = cur.lstat()
        except FileNotFoundError:
            cur.mkdir()
            continue
        if not stat.S_ISDIR(st.st_mode):
            cur.unlink()
            cur.mkdir()
    target = tree.joinpath(*parts)
    with contextlib.suppress(FileNotFoundError):
        if stat.S_ISDIR(target.lstat().st_mode):
            shutil.rmtree(target)
        else:
            target.unlink()
    if kind == "symlink":
        os.symlink(payload, target)
        return
    data, mode = payload
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view) :]
        os.fchmod(fd, mode & 0o7777 or 0o644)
    finally:
        os.close(fd)


def _make_removable(path: str) -> None:
    with contextlib.suppress(OSError):
        os.chmod(path, 0o700)
        with os.scandir(path) as scanner:
            for item in scanner:
                if item.is_dir(follow_symlinks=False):
                    _make_removable(item.path)


def remove_tree(path: Path) -> None:
    """Remove an attempt directory the worker may have made unreadable.  Never raises."""
    _make_removable(str(path))
    shutil.rmtree(path, ignore_errors=True)


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
        removals: list[str] = []
        copies: list[str] = []
        for record in records:
            copies.append(record.path)
            if record.source is not None:
                removals.append(record.source)
        copies.extend(owned)
        loaded: dict[str, tuple[str, Any]] = {}
        for rel in dict.fromkeys(copies + removals):
            loaded[rel] = _read_user_file(root_fd, rel)
        for rel in dict.fromkeys(removals + copies):
            kind, _payload = loaded[rel]
            if kind == "absent":
                _remove_in_tree(tree, rel)
        for rel in dict.fromkeys(copies):
            kind, payload = loaded[rel]
            if kind in ("file", "symlink"):
                _copy_into_tree(tree, rel, kind, payload)
            elif kind == "other":
                raise AttemptFailedError(f"{rel} is not a regular file or a symlink; not supported")
    finally:
        os.close(root_fd)

    manifest = walk_manifest(
        tree, limit_error=lambda reason: AttemptFailedError(f"seed refused: {reason}")
    )
    return AttemptTree(tree, manifest)


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
    m1 = walk_manifest(
        tree.path,
        hooks=hooks,
        limit_error=lambda reason: AttemptRefusedError(
            f"the attempt tree is over the limit: {reason}"
        ),
    )
    changed = {path for path in set(m0) | set(m1) if m0.get(path) != m1.get(path)}
    # A new path the user's rules ignore is a byproduct (a cache, a build output). A path the unit
    # owns never is: dropping it would transfer half of a repair, or reject a repair as empty.
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

    try:
        root_fd = os.open(str(tree.path), _DIR_FLAGS)
    except OSError as exc:
        raise AttemptFailedError("the attempt tree changed during validation") from exc
    try:
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
    parent_fd: int, name: str, data: bytes, mode: int, *, hooks: Hooks | None = None, path: str = ""
) -> None:
    """Write ``data`` to ``name`` under ``parent_fd`` through one unique temporary file.

    The temporary file is created ``O_EXCL | O_NOFOLLOW`` in the target directory, written, given
    ``mode`` and renamed over the target; on any failure before the rename it is unlinked by the
    same descriptor.  An unlink that fails is named on the exception (``leftover_temporary``) and
    never replaces it.
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
                os.close(fd)
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
            raise _ConflictError(f"{path}: a parent directory moved during the transfer") from exc
        held_st = os.fstat(fd)
        if not stat.S_ISDIR(at_name.st_mode) or (at_name.st_dev, at_name.st_ino) != (
            held_st.st_dev,
            held_st.st_ino,
        ):
            raise _ConflictError(f"{path}: a parent directory moved during the transfer")


def _write_item(
    root_fd: int,
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
                entry = JournalEntry("mkdir", "/".join(parts[: index + 1]))
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
            journal.append(JournalEntry("write", item.path, current, prior_data, intended))
            replace_file(cur, parts[-1], item.data, mode, hooks=hooks, path=item.path)
        else:
            journal.append(JournalEntry("delete", item.path, current, prior_data, None))
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
) -> None:
    """Copy a validated delta into the user's tree, all or nothing (D6).

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
        try:
            for item in delta.items:
                _check_read_only(root_fd, item, m0)
        except _ConflictError as exc:
            raise AttemptRefusedError(str(exc)) from exc
        _hook(hooks, "after_checks")
        try:
            for item in delta.items:
                _write_item(root_fd, item, m0, journal, hooks)
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
            if entry.op == "delete":
                result.not_restored.append((entry.path, "its directory is gone"))
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
                )
        except OSError as exc:
            result.not_restored.append((entry.path, describe_failure(exc)))
            return
        result.restored.append(entry.path)
    finally:
        for fd in fds:
            os.close(fd)


def _restore(root_fd: int, journal: list[JournalEntry], *, hooks: Hooks | None) -> RestoreResult:
    result = RestoreResult(touched=bool(journal))
    while journal:
        entry = journal.pop()
        parts = entry.path.split("/")
        if entry.op == "mkdir":
            _restore_directory(root_fd, entry, result, parts)
        else:
            _restore_file(root_fd, entry, result, parts, hooks)
    return result


def restore(journal: list[JournalEntry], repo: Path, hooks: Hooks | None = None) -> RestoreResult:
    """Undo the journalled mutations in reverse, never overwriting content the transfer did not write.

    For each entry a path holding the intended state is returned to its prior state; a path
    already holding the prior state is left and not reported (the mutation was never made);
    anything else is left alone and named.  A created directory is removed only when empty and
    still the directory the transfer created.  The journal is emptied; calling again is a no-op.
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
