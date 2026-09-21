"""Descriptor-relative file access confined to a repository root.

Owned-file reads, attempt hashing and untracked replay all consume paths that
originate from model output or task specs. Any ``Path(root) / rel`` open can be
redirected by a symlink in a parent component even when the leaf itself is a
regular file, so every production read of a repository-relative path walks the
components with held directory descriptors and ``O_NOFOLLOW`` instead.

This is the single boundary for that walk; callers must not open
repository-relative paths themselves (BOD-133 residual).
"""

from __future__ import annotations

import errno
import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path, PurePosixPath

# A file larger than this cannot be a plausible source unit for a model prompt,
# an attempt hash, or a replayed work artifact; refuse rather than consume.
MAX_FILE_BYTES = 4 * 1024 * 1024

_O_PATH = getattr(os, "O_PATH", 0)


class UnsafeRepositoryPathError(ValueError):
    """A repository-relative path escapes the root or is not a regular file."""

    def __init__(self, relative_path: str, reason: str, *, symlink: bool = False) -> None:
        super().__init__(f"unsafe repository path {relative_path!r}: {reason}")
        self.relative_path = relative_path
        self.reason = reason
        self.symlink = symlink


def _split(relative_path: str) -> tuple[str, ...]:
    parts = tuple(PurePosixPath(relative_path).parts)
    if not parts or parts[0] == "/" or ".." in parts or any(p == "" for p in parts):
        raise UnsafeRepositoryPathError(relative_path, "must be a non-empty repo-relative path")
    return parts


def _open_dir(parent_fd: int, component: str, relative_path: str) -> int:
    try:
        return os.open(component, _O_PATH | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
    except OSError as exc:
        # Linux reports ENOTDIR (not ELOOP) when O_NOFOLLOW meets a symlinked
        # directory; either way the component cannot be a real directory.
        if exc.errno in (errno.ELOOP, errno.EMLINK):
            raise UnsafeRepositoryPathError(
                relative_path, f"symlink in path component {component!r}", symlink=True
            ) from exc
        if exc.errno == errno.ENOTDIR:
            # Distinguish a missing ancestor from a symlink or non-directory.
            # lstat never follows the component if it has itself become a link.
            try:
                os.stat(component, dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                raise FileNotFoundError(
                    errno.ENOENT, os.strerror(errno.ENOENT), relative_path
                ) from exc
            raise UnsafeRepositoryPathError(
                relative_path,
                f"symlink or non-directory in path component {component!r}",
                symlink=True,
            ) from exc
        raise UnsafeRepositoryPathError(
            relative_path, f"path component {component!r} is not a directory"
        ) from exc


def _open_leaf(parent_fd: int, leaf: str, relative_path: str, flags: int) -> int:
    try:
        fd = os.open(leaf, flags | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent_fd)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise UnsafeRepositoryPathError(
                relative_path, f"symlink at leaf {leaf!r}", symlink=True
            ) from exc
        raise
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise UnsafeRepositoryPathError(relative_path, f"leaf {leaf!r} is not a regular file")
    return fd


@contextmanager
def hold_repository_dirs(
    root: str | Path, relative_path: str
) -> Iterator[tuple[int, str, tuple[str, ...]]]:
    """Yield ``(parent_dir_fd, leaf, components)`` with every component held open.

    All descriptors stay open for the lifetime of the context so a concurrent
    swap of a validated component into a symlink cannot redirect the final
    open. Callers open the leaf against ``parent_dir_fd`` themselves so they
    can choose creation semantics.
    """
    parts = _split(relative_path)
    fds: list[int] = []
    try:
        fds.append(os.open(str(root), _O_PATH | os.O_DIRECTORY))
        for component in parts[:-1]:
            fds.append(_open_dir(fds[-1], component, relative_path))
        yield fds[-1], parts[-1], parts
    finally:
        for fd in reversed(fds):
            os.close(fd)


def open_repository_file(root: str | Path, relative_path: str, flags: int = os.O_RDONLY) -> int:
    """Return an fd for ``relative_path`` under ``root``, confined component-wise.

    Only regular files are returned; symlinks (leaf or parent), directories,
    FIFOs and device nodes are refused. Non-existence propagates as the normal
    ``FileNotFoundError`` so callers can keep treating absent files as ordinary
    omissions rather than security events.
    """
    with hold_repository_dirs(root, relative_path) as (parent_fd, leaf, _):
        return _open_leaf(parent_fd, leaf, relative_path, flags)


def read_repository_bytes(root: str | Path, relative_path: str) -> bytes:
    """Read a confined regular file as bytes, bounded by MAX_FILE_BYTES."""
    fd = open_repository_file(root, relative_path)
    try:
        chunks: list[bytes] = []
        total = 0
        while total < MAX_FILE_BYTES:
            chunk = os.read(fd, min(65536, MAX_FILE_BYTES - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        if total >= MAX_FILE_BYTES and os.read(fd, 1):
            raise UnsafeRepositoryPathError(relative_path, f"exceeds {MAX_FILE_BYTES} byte bound")
        return b"".join(chunks)
    finally:
        os.close(fd)


def read_repository_text(root: str | Path, relative_path: str) -> str:
    """Read a confined regular file as UTF-8 text."""
    return read_repository_bytes(root, relative_path).decode("utf-8")
