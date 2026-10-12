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

import shutil
from dataclasses import dataclass
from pathlib import Path

from verdict.security import redact_text

# Upper bound on individual salvaged file size (1 MiB).  Anything larger is
# likely a generated artefact (binary, vendored bundle) not worth replaying.
_MAX_FILE_BYTES = 1_048_576

# Maximum total salvage size across all files (4 MiB).
_MAX_TOTAL_BYTES = 4_194_304


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


def _has_credential_content(path: Path) -> bool:
    """Return True if the file content contains detectable credentials."""
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    redacted = redact_text(raw)
    return redacted != raw


def _is_owned(relative: str, owned_files: frozenset[str]) -> bool:
    """Check whether a repo-relative path falls within owned_files."""
    # Exact match
    if relative in owned_files:
        return True
    # Directory ownership: if owned_files contains "dir/" or "dir", files
    # under that directory are owned.
    parts = relative.split("/")
    for i in range(1, len(parts)):
        prefix = "/".join(parts[:i])
        if prefix in owned_files or (prefix + "/") in owned_files:
            return True
    return False


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
    owned_set = frozenset(owned_files)
    saved: list[str] = []
    skipped: list[str] = []
    total_bytes = 0

    try:
        # Enumerate files that exist in owned paths
        candidates: list[str] = []
        for owned in owned_files:
            full = worktree / owned
            if full.is_file():
                candidates.append(owned)
            elif full.is_dir():
                for child in sorted(full.rglob("*")):
                    if child.is_file():
                        rel = str(child.relative_to(worktree))
                        candidates.append(rel)

        if not candidates:
            return SalvageResult(saved=(), skipped=())

        # Prepare salvage directory
        salvage_dir.mkdir(parents=True, exist_ok=True)

        for rel_path in candidates:
            src = worktree / rel_path
            if not src.is_file():
                skipped.append(rel_path)
                continue

            # Ownership check (defence in depth for rglob results)
            if not _is_owned(rel_path, owned_set):
                skipped.append(rel_path)
                continue

            # Size check
            try:
                size = src.stat().st_size
            except OSError:
                skipped.append(rel_path)
                continue
            if size > _MAX_FILE_BYTES:
                skipped.append(rel_path)
                continue
            if total_bytes + size > _MAX_TOTAL_BYTES:
                skipped.append(rel_path)
                continue

            # Credential check
            if _has_credential_content(src):
                skipped.append(rel_path)
                continue

            # Copy
            dst = salvage_dir / rel_path
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(src), str(dst))
            saved.append(rel_path)
            total_bytes += size

        return SalvageResult(saved=tuple(saved), skipped=tuple(skipped), total_bytes=total_bytes)

    except Exception as exc:
        # Clean up partial salvage on unexpected error
        if salvage_dir.exists():
            shutil.rmtree(salvage_dir, ignore_errors=True)
        return SalvageResult(saved=(), skipped=(), error=f"{type(exc).__name__}: {exc}")


def restore_salvaged_files(
    salvage_dir: Path, worktree: Path, owned_files: tuple[str, ...]
) -> RestoreResult:
    """Restore previously salvaged files into a fresh worktree.

    Only files that (a) exist in the salvage directory, (b) are within the
    node's owned_files, and (c) pass credential checks are restored.
    A missing or empty salvage directory produces an empty result (no-change safe).

    Returns a content-free ``RestoreResult``.
    """
    if not salvage_dir.exists() or not salvage_dir.is_dir():
        return RestoreResult(restored=(), skipped=())

    owned_set = frozenset(owned_files)
    restored: list[str] = []
    skipped: list[str] = []
    total_bytes = 0

    try:
        for src in sorted(salvage_dir.rglob("*")):
            if not src.is_file():
                continue

            rel_path = str(src.relative_to(salvage_dir))

            # Ownership re-check
            if not _is_owned(rel_path, owned_set):
                skipped.append(rel_path)
                continue

            # Credential re-check on restore
            if _has_credential_content(src):
                skipped.append(rel_path)
                continue

            # Size guard
            try:
                size = src.stat().st_size
            except OSError:
                skipped.append(rel_path)
                continue
            if size > _MAX_FILE_BYTES:
                skipped.append(rel_path)
                continue

            dst = worktree / rel_path
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(src), str(dst))
            restored.append(rel_path)
            total_bytes += size

        return RestoreResult(
            restored=tuple(restored), skipped=tuple(skipped), total_bytes=total_bytes
        )

    except Exception as exc:
        return RestoreResult(restored=(), skipped=(), error=f"{type(exc).__name__}: {exc}")
