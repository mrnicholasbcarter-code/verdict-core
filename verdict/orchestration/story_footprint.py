"""Story footprint model and collision detection for multi-story concurrency.

Pure library — no I/O, no network, no Linear calls. All inputs are typed values.
Unknown or missing evidence fails closed (SERIALIZE), never defaults to parallel.

Implements BOD-157 Packet A (AC 2, 3).
"""

from __future__ import annotations

import fnmatch
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


@dataclass(frozen=True)
class StoryFootprintV1:
    """Immutable description of a story's file-system and authority impact.

    Parameters
    ----------
    story_id:
        Unique identifier for the story (e.g. Linear issue id).
    write_paths:
        Concrete repo-relative paths **or** glob patterns the story will touch.
        An empty set means the footprint is unknown.
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


def _is_glob(pattern: str) -> bool:
    """Return True if *pattern* contains glob metacharacters."""
    return any(c in pattern for c in ("*", "?", "["))


def _paths_overlap(paths_a: frozenset[str], paths_b: frozenset[str]) -> CollisionReason | None:
    """Check whether two write-path sets overlap (literal or via globs).

    Returns the most specific reason code, or ``None`` when disjoint.
    """
    # Fast literal intersection first.
    if paths_a & paths_b:
        return CollisionReason.SAME_FILE

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


def collide(a: StoryFootprintV1, b: StoryFootprintV1) -> tuple[CollisionVerdict, CollisionReason]:
    """Decide whether stories *a* and *b* may execute concurrently.

    Returns ``(verdict, reason)``. The function is commutative:
    ``collide(a, b) == collide(b, a)``.

    Fail-closed rule: if either footprint is unknown (no write paths declared),
    the verdict is always SERIALIZE.
    """
    # Fail closed on unknown footprint.
    if not a.is_known() or not b.is_known():
        return CollisionVerdict.SERIALIZE, CollisionReason.UNKNOWN_FOOTPRINT

    # Authority overlap is checked before path overlap — it is a broader signal.
    auth_reason = _authorities_overlap(a.authorities, b.authorities)
    if auth_reason is not None:
        return CollisionVerdict.SERIALIZE, auth_reason

    path_reason = _paths_overlap(a.write_paths, b.write_paths)
    if path_reason is not None:
        return CollisionVerdict.SERIALIZE, path_reason

    return CollisionVerdict.PARALLEL, CollisionReason.DISJOINT
