"""Partial-work salvage and restoration for DAG node retry.

When a node attempt fails after producing owned-file changes, this module
preserves those files in a durable salvage directory under the run.  On the
next retry (same node, possibly different route), the preserved files are
restored into the fresh worktree before the worker executes, giving it a
head-start on partial work from the previous attempt.

Safety rules:
- Only files listed in the node's ``owned_files`` are eligible for salvage.
- Files whose content matches credential patterns are rejected.
- Corrupted or missing salvage directories produce an empty restore (no-change safe).
- All evidence emitted is content-free: file names, counts, sizes only.
"""

from __future__ import annotations

import os
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from verdict.security import redact_text

# Upper bound on individual salvaged file size (1 MiB).  Anything larger is
# likely a generated artefact (binary, vendored bundle) not worth replaying.
_MAX_FILE_BYTES = 1_048_576

# Maximum total salvage size across all files (4 MiB).
_MAX_TOTAL_BYTES = 4_194_304
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


@dataclass(frozen=True)
class SalvageResult:
    """Content-free evidence of a salvage operation."""

    saved: tuple[str, ...]  # repo-relative paths that were preserved
    skipped: tuple[str, ...]  # paths skipped (too large, credential, unowned)
    total_bytes: int = 0
    error: str = ""  # non-empty only on unexpected failure


@dataclass(frozen=True)
class RestoreResult:
    """Content-free evidence of a restore operation."""

    restored: tuple[str, ...]  # repo-relative paths written into the worktree
    skipped: tuple[str, ...]  # paths that could not be restored
    total_bytes: int = 0
    error: str = ""


def _safe_relative_path(relative: str) -> str | None:
    """Return a normalized POSIX relative path, or None when unsafe."""
    if not relative or "\x00" in relative:
        return None
    stripped = relative.rstrip("/")
    if not stripped or stripped.startswith("/"):
        return None
    parts = stripped.split("/")
    if any(part in ("", ".", "..") for part in parts):
        return None
    candidate = PurePosixPath(stripped)
    if candidate.is_absolute() or any(part in ("", ".", "..") for part in candidate.parts):
        return None
    return candidate.as_posix()


def _safe_owned_set(owned_files: tuple[str, ...]) -> frozenset[str]:
    return frozenset(
        safe for owned in owned_files if (safe := _safe_relative_path(owned)) is not None
    )


def _is_owned(relative: str, owned_files: frozenset[str]) -> bool:
    """Check whether a repo-relative path falls within owned_files."""
    safe_relative = _safe_relative_path(relative)
    if safe_relative is None:
        return False

    # Exact match
    if safe_relative in owned_files:
        return True
    # Directory ownership: if owned_files contains "dir" (normalized from
    # "dir/" if supplied), files under that directory are owned.
    parts = safe_relative.split("/")
    for i in range(1, len(parts)):
        prefix = "/".join(parts[:i])
        if prefix in owned_files:
            return True
    return False


def _is_dir_no_follow(path: Path) -> bool:
    try:
        mode = path.lstat().st_mode
    except OSError:
        return False
    return stat.S_ISDIR(mode)


def _regular_file_size_no_follow(path: Path) -> int | None:
    try:
        mode_size = path.lstat()
    except OSError:
        return None
    if not stat.S_ISREG(mode_size.st_mode):
        return None
    return mode_size.st_size


def _resolved_within(path: Path, root: Path, *, strict: bool) -> bool:
    try:
        resolved_root = root.resolve(strict=True)
        resolved_path = path.resolve(strict=strict)
        resolved_path.relative_to(resolved_root)
    except (OSError, ValueError):
        return False
    return True


def _has_symlink_component(root: Path, relative: str) -> bool:
    current = root
    for part in PurePosixPath(relative).parts:
        current = current / part
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            continue
        except OSError:
            return True
        if stat.S_ISLNK(mode):
            return True
    return False


def _ensure_safe_parent(root: Path, relative: str) -> bool:
    if not _resolved_within(root / relative, root, strict=False):
        return False
    if _has_symlink_component(root, relative):
        return False

    parent = PurePosixPath(relative).parent
    current = root
    for part in parent.parts:
        if part == ".":
            continue
        current = current / part
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            try:
                current.mkdir()
                mode = current.lstat().st_mode
            except OSError:
                return False
        except OSError:
            return False
        if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
            return False
        if not _resolved_within(current, root, strict=True):
            return False
    return True


def _read_text_no_follow(path: Path) -> str:
    fd = os.open(path, os.O_RDONLY | _NOFOLLOW)
    try:
        with os.fdopen(fd, "rb") as handle:
            file_stat = os.fstat(handle.fileno())
            if not stat.S_ISREG(file_stat.st_mode):
                raise OSError("not_regular_file")
            if file_stat.st_size > _MAX_FILE_BYTES:
                raise OSError("file_too_large")
            return handle.read().decode("utf-8", errors="replace")
    except Exception:
        raise


def _has_credential_content(path: Path) -> bool:
    """Return True when content is credential-bearing or unreadable."""
    try:
        raw = _read_text_no_follow(path)
    except OSError:
        return True
    redacted = redact_text(raw)
    return redacted != raw


def _copy_regular_no_follow(src: Path, dst: Path) -> None:
    src_fd = os.open(src, os.O_RDONLY | _NOFOLLOW)
    try:
        with os.fdopen(src_fd, "rb") as source:
            source_stat = os.fstat(source.fileno())
            if not stat.S_ISREG(source_stat.st_mode):
                raise OSError("not_regular_file")
            if source_stat.st_size > _MAX_FILE_BYTES:
                raise OSError("file_too_large")

            dst_fd = os.open(
                dst, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | _NOFOLLOW, source_stat.st_mode & 0o777
            )
            with os.fdopen(dst_fd, "wb") as target:
                shutil.copyfileobj(source, target)
    except Exception:
        raise


def _iter_regular_relative_files(root: Path) -> tuple[list[str], list[str]]:
    candidates: list[str] = []
    skipped: list[str] = []
    seen: set[str] = set()

    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(dirnames)
        for dirname in list(dirnames):
            child = Path(dirpath) / dirname
            rel = child.relative_to(root).as_posix()
            safe_rel = _safe_relative_path(rel)
            if safe_rel is None or _has_symlink_component(root, safe_rel):
                skipped.append(rel)
                dirnames.remove(dirname)

        for filename in sorted(filenames):
            child = Path(dirpath) / filename
            rel = child.relative_to(root).as_posix()
            safe_rel = _safe_relative_path(rel)
            if safe_rel is None or _has_symlink_component(root, safe_rel):
                skipped.append(rel)
                continue
            if _regular_file_size_no_follow(child) is None:
                skipped.append(rel)
                continue
            if safe_rel not in seen:
                candidates.append(safe_rel)
                seen.add(safe_rel)

    return candidates, skipped


def salvage_owned_files(
    worktree: Path, owned_files: tuple[str, ...], salvage_dir: Path, *, base_sha: str = ""
) -> SalvageResult:
    """Preserve owned modified files from a failed attempt's worktree.

    Only files that (a) exist in the worktree, (b) are listed in owned_files,
    (c) are within size limits, and (d) contain no detectable credentials are
    saved.  The operation is all-or-nothing on error: a partial copy is cleaned
    up and an error result is returned.

    Returns a content-free ``SalvageResult``.
    """
    owned_set = _safe_owned_set(owned_files)
    saved: list[str] = []
    skipped: list[str] = []
    total_bytes = 0

    try:
        if not _is_dir_no_follow(worktree):
            return SalvageResult(saved=(), skipped=(), error="unsafe_worktree")

        candidates: list[str] = []
        seen: set[str] = set()
        for owned in owned_files:
            rel = _safe_relative_path(owned)
            if rel is None:
                skipped.append(owned)
                continue

            full = worktree / rel
            if _has_symlink_component(worktree, rel) or not _resolved_within(
                full, worktree, strict=False
            ):
                skipped.append(rel)
                continue

            if _regular_file_size_no_follow(full) is not None:
                if rel not in seen:
                    candidates.append(rel)
                    seen.add(rel)
            elif _is_dir_no_follow(full):
                child_candidates, child_skipped = _iter_regular_relative_files(full)
                for child_skipped_rel in child_skipped:
                    skipped.append(f"{rel}/{child_skipped_rel}")
                for child_rel in child_candidates:
                    repo_rel = f"{rel}/{child_rel}"
                    if repo_rel not in seen:
                        candidates.append(repo_rel)
                        seen.add(repo_rel)

        if not candidates:
            return SalvageResult(saved=(), skipped=tuple(skipped))

        salvage_dir.mkdir(parents=True, exist_ok=True)
        if not _is_dir_no_follow(salvage_dir):
            return SalvageResult(saved=(), skipped=tuple(skipped), error="unsafe_salvage_dir")

        for rel_path in candidates:
            src = worktree / rel_path
            if not _is_owned(rel_path, owned_set):
                skipped.append(rel_path)
                continue
            if _has_symlink_component(worktree, rel_path) or not _resolved_within(
                src, worktree, strict=True
            ):
                skipped.append(rel_path)
                continue

            size = _regular_file_size_no_follow(src)
            if size is None or size > _MAX_FILE_BYTES:
                skipped.append(rel_path)
                continue
            if total_bytes + size > _MAX_TOTAL_BYTES:
                skipped.append(rel_path)
                continue

            if _has_credential_content(src):
                skipped.append(rel_path)
                continue

            dst = salvage_dir / rel_path
            if not _ensure_safe_parent(salvage_dir, rel_path):
                skipped.append(rel_path)
                continue
            _copy_regular_no_follow(src, dst)
            saved.append(rel_path)
            total_bytes += size

        return SalvageResult(saved=tuple(saved), skipped=tuple(skipped), total_bytes=total_bytes)

    except Exception as exc:
        if salvage_dir.exists() and _is_dir_no_follow(salvage_dir):
            shutil.rmtree(salvage_dir, ignore_errors=True)
        return SalvageResult(saved=(), skipped=(), error=type(exc).__name__)


def restore_salvaged_files(
    salvage_dir: Path, worktree: Path, owned_files: tuple[str, ...]
) -> RestoreResult:
    """Restore previously salvaged files into a fresh worktree.

    Only files that (a) exist in the salvage directory, (b) are within the
    node's owned_files, and (c) pass credential checks are restored.
    A missing or empty salvage directory produces an empty result (no-change safe).

    Returns a content-free ``RestoreResult``.
    """
    if not salvage_dir.exists():
        return RestoreResult(restored=(), skipped=())
    if not _is_dir_no_follow(salvage_dir) or not _is_dir_no_follow(worktree):
        return RestoreResult(restored=(), skipped=(), error="unsafe_restore_root")

    owned_set = _safe_owned_set(owned_files)
    restored: list[str] = []
    skipped: list[str] = []
    total_bytes = 0

    try:
        candidates, rejected = _iter_regular_relative_files(salvage_dir)
        skipped.extend(rejected)

        for rel_path in candidates:
            src = salvage_dir / rel_path
            if not _is_owned(rel_path, owned_set):
                skipped.append(rel_path)
                continue
            if _has_symlink_component(salvage_dir, rel_path) or not _resolved_within(
                src, salvage_dir, strict=True
            ):
                skipped.append(rel_path)
                continue

            size = _regular_file_size_no_follow(src)
            if size is None or size > _MAX_FILE_BYTES:
                skipped.append(rel_path)
                continue
            if total_bytes + size > _MAX_TOTAL_BYTES:
                skipped.append(rel_path)
                continue

            if _has_credential_content(src):
                skipped.append(rel_path)
                continue

            dst = worktree / rel_path
            if not _ensure_safe_parent(worktree, rel_path):
                skipped.append(rel_path)
                continue
            _copy_regular_no_follow(src, dst)
            restored.append(rel_path)
            total_bytes += size

        return RestoreResult(
            restored=tuple(restored), skipped=tuple(skipped), total_bytes=total_bytes
        )

    except Exception as exc:
        return RestoreResult(restored=(), skipped=(), error=type(exc).__name__)
