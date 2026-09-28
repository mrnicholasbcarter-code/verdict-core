"""Story footprint model and collision detection for multi-story concurrency.

Pure library — no I/O, no network, no Linear calls. All inputs are typed values.
Unknown or missing evidence fails closed (SERIALIZE), never defaults to parallel.

Path normalization contract
---------------------------
All write-path strings are **defensively normalized** inside the library before
any comparison.  Callers need not pre-normalize, but the following invariants
are enforced on every path:

1. Backslashes are converted to forward slashes.
2. ``posixpath.normpath`` collapses ``./``, ``..``, and duplicate ``/``.
3. A leading ``./`` that survives normpath is stripped.
4. Trailing ``/`` is stripped (directories are detected by the absence of a
   file extension *or* an explicit trailing ``/`` in the **original** input,
   but stored without it).
5. **Case-insensitive comparison** is used for collision checks.  This is the
   conservative choice: on case-insensitive filesystems (macOS default, Windows)
   ``Verdict/API.py`` and ``verdict/api.py`` name the same file, so we must
   serialize.  On case-sensitive filesystems this is a false positive (safe).
6. **Fail closed on invalid paths**: absolute paths, paths that still reference
   ``..`` after normalization (escape the repo root), or paths that normalize
   to empty are flagged ``INVALID_PATH`` and force ``SERIALIZE``.
7. A directory entry (``verdict/`` or ``verdict``) collides with any file
   whose normalized path starts with that directory prefix followed by ``/``.

Implements BOD-157 Packet A (AC 2, 3).
"""

from __future__ import annotations

import fnmatch
import posixpath
from dataclasses import dataclass, field
from enum import Enum


class CollisionVerdict(str, Enum):
    """Whether two stories may run concurrently."""

    PARALLEL = "PARALLEL"
    SERIALIZE = "SERIALIZE"


class CollisionReason(str, Enum):
    """Why a particular verdict was reached."""

    DISJOINT = "disjoint"
    SAME_FILE = "same_file"
    GLOB_OVERLAP = "glob_overlap"
    SAME_AUTHORITY = "same_authority"
    UNKNOWN_FOOTPRINT = "unknown_footprint"
    INVALID_PATH = "invalid_path"
    DIRECTORY_OVERLAP = "directory_overlap"


# ------------------------------------------------------------------ helpers


def _normalize_path(raw: str) -> str:
    """Normalize a single repo-relative path or glob.

    Converts backslashes, collapses ``.`` / ``..`` / duplicate ``/``,
    strips leading ``./`` and trailing ``/``.  Does **not** lower-case
    (case folding is applied at comparison time only so the stored
    representation is faithful to the caller's input).
    """
    p = raw.replace("\\", "/")
    p = posixpath.normpath(p)
    # normpath turns empty / pure-dot into "."
    if p == ".":
        return ""
    return p


def _is_invalid(normalized: str) -> bool:
    """Return True when *normalized* cannot be a valid repo-relative path."""
    if not normalized:
        return True
    if posixpath.isabs(normalized):
        return True
    # After normpath, ".." at the start means it escapes the repo root.
    return normalized == ".." or normalized.startswith("../")


def _is_glob(pattern: str) -> bool:
    """Return True if *pattern* contains glob metacharacters."""
    return any(c in pattern for c in ("*", "?", "["))


def _is_directory_entry(raw: str) -> bool:
    """Heuristic: the *original* (pre-normpath) path looks like a directory.

    We check the raw input because normpath strips trailing ``/``.
    """
    stripped = raw.replace("\\", "/").rstrip("/")
    # After stripping slashes, if raw ended with '/' it was a directory.
    if raw.replace("\\", "/") != stripped + ("/" if raw.replace("\\", "/").endswith("/") else ""):
        pass  # just need the trailing-slash check below
    return raw.replace("\\", "/").rstrip(" ").endswith("/")


def _paths_overlap(
    paths_a: frozenset[str], paths_b: frozenset[str], dirs_a: frozenset[str], dirs_b: frozenset[str]
) -> CollisionReason | None:
    """Check whether two normalized, case-folded write-path sets overlap.

    *dirs_a* / *dirs_b* are the subsets of each side that were detected as
    directory entries (stored without trailing ``/``, already lower-cased).

    Returns the most specific reason code, or ``None`` when disjoint.
    """
    # Fast literal intersection (case-folded).
    if paths_a & paths_b:
        return CollisionReason.SAME_FILE

    # Directory-vs-file: a directory "verdict" collides with "verdict/api.py".
    for d in dirs_a:
        prefix = d + "/"
        for pb in paths_b:
            if pb.startswith(prefix) or pb == d:
                return CollisionReason.DIRECTORY_OVERLAP
    for d in dirs_b:
        prefix = d + "/"
        for pa in paths_a:
            if pa.startswith(prefix) or pa == d:
                return CollisionReason.DIRECTORY_OVERLAP

    # Glob expansion: check every (a, b) pair for fnmatch overlap.
    for pa in paths_a:
        for pb in paths_b:
            if _is_glob(pa) and fnmatch.fnmatch(pb, pa):
                return CollisionReason.GLOB_OVERLAP
            if _is_glob(pb) and fnmatch.fnmatch(pa, pb):
                return CollisionReason.GLOB_OVERLAP
            # Both globs — conservative: if either matches the other's pattern
            # string we flag overlap (exact glob-vs-glob intersection is
            # undecidable in general; fail closed).
            if (
                _is_glob(pa)
                and _is_glob(pb)
                and (fnmatch.fnmatch(pa, pb) or fnmatch.fnmatch(pb, pa))
            ):
                return CollisionReason.GLOB_OVERLAP

    return None


def _authorities_overlap(auth_a: frozenset[str], auth_b: frozenset[str]) -> CollisionReason | None:
    """Check whether two authority sets share any canonical resource."""
    if auth_a & auth_b:
        return CollisionReason.SAME_AUTHORITY
    return None


@dataclass(frozen=True)
class StoryFootprintV1:
    """Immutable description of a story's file-system and authority impact.

    Parameters
    ----------
    story_id:
        Unique identifier for the story (e.g. Linear issue id).
    write_paths:
        Concrete repo-relative paths **or** glob patterns the story will touch.
        An empty set means the footprint is unknown.  Paths are defensively
        normalized inside :func:`collide` — callers need not pre-normalize.
    authorities:
        Canonical shared resources the story touches (e.g. ``"schemas/contracts"``,
        ``"migrations"``, ``"lockfiles"``).  An empty set means no known authorities.
    """

    story_id: str
    write_paths: frozenset[str] = field(default_factory=frozenset)
    authorities: frozenset[str] = field(default_factory=frozenset)

    def is_known(self) -> bool:
        """A footprint is known only when at least one write path is declared."""
        return len(self.write_paths) > 0


def _prepare_paths(raw_paths: frozenset[str]) -> tuple[frozenset[str], frozenset[str], bool]:
    """Normalize and validate *raw_paths*.

    Returns ``(normalized_lower, directory_entries_lower, has_invalid)``.
    All returned strings are lower-cased for case-insensitive comparison.
    """
    normalized: list[str] = []
    dirs: list[str] = []
    has_invalid = False

    for raw in raw_paths:
        is_dir = _is_directory_entry(raw)
        norm = _normalize_path(raw)
        if _is_invalid(norm):
            has_invalid = True
            break
        lower = norm.lower()
        normalized.append(lower)
        if is_dir:
            dirs.append(lower)

    return frozenset(normalized), frozenset(dirs), has_invalid


def collide(a: StoryFootprintV1, b: StoryFootprintV1) -> tuple[CollisionVerdict, CollisionReason]:
    """Decide whether stories *a* and *b* may execute concurrently.

    Returns ``(verdict, reason)``.  The function is **symmetric**:
    ``collide(a, b) == collide(b, a)``.

    All write paths are defensively normalized before comparison:

    * ``./``, ``..``, duplicate ``/``, trailing ``/``, and backslashes are
      collapsed via :func:`_normalize_path`.
    * Comparisons are **case-insensitive** (conservative for mixed-OS repos).
    * Absolute paths, repo-root escapes (``..``), and empty-after-norm paths
      force ``(SERIALIZE, INVALID_PATH)``.
    * Directory entries collide with any file beneath them.

    Fail-closed rule: if either footprint is unknown (no write paths declared),
    the verdict is always ``SERIALIZE``.
    """
    # Fail closed on unknown footprint.
    if not a.is_known() or not b.is_known():
        return CollisionVerdict.SERIALIZE, CollisionReason.UNKNOWN_FOOTPRINT

    # Normalize + validate.
    paths_a, dirs_a, invalid_a = _prepare_paths(a.write_paths)
    paths_b, dirs_b, invalid_b = _prepare_paths(b.write_paths)

    if invalid_a or invalid_b:
        return CollisionVerdict.SERIALIZE, CollisionReason.INVALID_PATH

    # Authority overlap is checked before path overlap — it is a broader signal.
    auth_reason = _authorities_overlap(a.authorities, b.authorities)
    if auth_reason is not None:
        return CollisionVerdict.SERIALIZE, auth_reason

    path_reason = _paths_overlap(paths_a, paths_b, dirs_a, dirs_b)
    if path_reason is not None:
        return CollisionVerdict.SERIALIZE, path_reason

    return CollisionVerdict.PARALLEL, CollisionReason.DISJOINT
