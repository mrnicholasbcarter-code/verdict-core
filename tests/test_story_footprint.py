"""Tests for verdict.orchestration.story_footprint — collision detection.

Covers BOD-157 Packet A acceptance criteria:
- disjoint footprints → PARALLEL
- same file → SERIALIZE
- overlapping globs → SERIALIZE
- same authority → SERIALIZE
- unknown / empty footprint → SERIALIZE (fail closed)
"""

from __future__ import annotations

import pytest

from verdict.orchestration.story_footprint import (
    CollisionReason,
    CollisionVerdict,
    StoryFootprintV1,
    collide,
)

# ---------------------------------------------------------------- helpers


def fp(
    sid: str, paths: frozenset[str] | None = None, authorities: frozenset[str] | None = None
) -> StoryFootprintV1:
    return StoryFootprintV1(
        story_id=sid, write_paths=paths or frozenset(), authorities=authorities or frozenset()
    )


# ---------------------------------------------------------------- is_known


class TestIsKnown:
    def test_empty_paths_is_unknown(self) -> None:
        assert not fp("s1").is_known()

    def test_with_paths_is_known(self) -> None:
        assert fp("s1", frozenset({"a.py"})).is_known()

    def test_authorities_only_still_unknown(self) -> None:
        """Authorities alone don't make a footprint known — write_paths required."""
        assert not fp("s1", authorities=frozenset({"migrations"})).is_known()


# ---------------------------------------------------------------- disjoint → PARALLEL


class TestDisjoint:
    def test_disjoint_files(self) -> None:
        a = fp("s1", frozenset({"verdict/router.py"}))
        b = fp("s2", frozenset({"verdict/proxy.py"}))
        verdict, reason = collide(a, b)
        assert verdict is CollisionVerdict.PARALLEL
        assert reason is CollisionReason.DISJOINT

    def test_disjoint_with_authorities(self) -> None:
        a = fp("s1", frozenset({"verdict/a.py"}), frozenset({"schemas/contracts"}))
        b = fp("s2", frozenset({"verdict/b.py"}), frozenset({"migrations"}))
        verdict, reason = collide(a, b)
        assert verdict is CollisionVerdict.PARALLEL
        assert reason is CollisionReason.DISJOINT

    def test_commutativity(self) -> None:
        a = fp("s1", frozenset({"x.py"}))
        b = fp("s2", frozenset({"y.py"}))
        assert collide(a, b) == collide(b, a)


# ---------------------------------------------------------------- same file → SERIALIZE


class TestSameFile:
    def test_exact_same_file(self) -> None:
        a = fp("s1", frozenset({"verdict/router.py", "verdict/api.py"}))
        b = fp("s2", frozenset({"verdict/router.py", "verdict/proxy.py"}))
        verdict, reason = collide(a, b)
        assert verdict is CollisionVerdict.SERIALIZE
        assert reason is CollisionReason.SAME_FILE

    def test_single_overlapping_file(self) -> None:
        a = fp("s1", frozenset({"verdict/contracts.py"}))
        b = fp("s2", frozenset({"verdict/contracts.py"}))
        verdict, reason = collide(a, b)
        assert verdict is CollisionVerdict.SERIALIZE
        assert reason is CollisionReason.SAME_FILE

    def test_same_file_commutativity(self) -> None:
        a = fp("s1", frozenset({"verdict/api.py", "verdict/router.py"}))
        b = fp("s2", frozenset({"verdict/router.py"}))
        assert collide(a, b) == collide(b, a)


# ---------------------------------------------------------------- glob overlap → SERIALIZE


class TestGlobOverlap:
    def test_glob_matches_concrete_path(self) -> None:
        a = fp("s1", frozenset({"verdict/orchestration/*.py"}))
        b = fp("s2", frozenset({"verdict/orchestration/run.py"}))
        verdict, reason = collide(a, b)
        assert verdict is CollisionVerdict.SERIALIZE
        assert reason is CollisionReason.GLOB_OVERLAP

    def test_concrete_matches_glob(self) -> None:
        """Reverse direction: concrete in a, glob in b."""
        a = fp("s1", frozenset({"verdict/orchestration/run.py"}))
        b = fp("s2", frozenset({"verdict/orchestration/*.py"}))
        verdict, reason = collide(a, b)
        assert verdict is CollisionVerdict.SERIALIZE
        assert reason is CollisionReason.GLOB_OVERLAP

    def test_identical_globs_same_file(self) -> None:
        """Identical glob strings are literal set intersection → SAME_FILE."""
        a = fp("s1", frozenset({"schemas/*.json"}))
        b = fp("s2", frozenset({"schemas/*.json"}))
        verdict, reason = collide(a, b)
        assert verdict is CollisionVerdict.SERIALIZE
        assert reason is CollisionReason.SAME_FILE

    def test_distinct_globs_overlap(self) -> None:
        """Different glob patterns that match each other → GLOB_OVERLAP."""
        a = fp("s1", frozenset({"schemas/*.json"}))
        b = fp("s2", frozenset({"schemas/contracts.json"}))
        verdict, reason = collide(a, b)
        assert verdict is CollisionVerdict.SERIALIZE
        assert reason is CollisionReason.GLOB_OVERLAP

    def test_glob_no_match(self) -> None:
        a = fp("s1", frozenset({"verdict/*.py"}))
        b = fp("s2", frozenset({"tests/test_api.py"}))
        verdict, reason = collide(a, b)
        assert verdict is CollisionVerdict.PARALLEL
        assert reason is CollisionReason.DISJOINT

    def test_glob_overlap_commutativity(self) -> None:
        a = fp("s1", frozenset({"verdict/orchestration/*.py"}))
        b = fp("s2", frozenset({"verdict/orchestration/run.py"}))
        assert collide(a, b) == collide(b, a)


# ---------------------------------------------------------------- same authority → SERIALIZE


class TestSameAuthority:
    def test_shared_authority(self) -> None:
        a = fp("s1", frozenset({"verdict/a.py"}), frozenset({"schemas/contracts"}))
        b = fp("s2", frozenset({"verdict/b.py"}), frozenset({"schemas/contracts"}))
        verdict, reason = collide(a, b)
        assert verdict is CollisionVerdict.SERIALIZE
        assert reason is CollisionReason.SAME_AUTHORITY

    def test_multiple_authorities_one_overlap(self) -> None:
        a = fp("s1", frozenset({"x.py"}), frozenset({"migrations", "lockfiles"}))
        b = fp("s2", frozenset({"y.py"}), frozenset({"schemas/contracts", "lockfiles"}))
        verdict, reason = collide(a, b)
        assert verdict is CollisionVerdict.SERIALIZE
        assert reason is CollisionReason.SAME_AUTHORITY

    def test_authority_takes_precedence_over_disjoint_paths(self) -> None:
        """Even if paths are disjoint, shared authority → SERIALIZE."""
        a = fp("s1", frozenset({"verdict/a.py"}), frozenset({"migrations"}))
        b = fp("s2", frozenset({"verdict/b.py"}), frozenset({"migrations"}))
        verdict, reason = collide(a, b)
        assert verdict is CollisionVerdict.SERIALIZE
        assert reason is CollisionReason.SAME_AUTHORITY

    def test_authority_commutativity(self) -> None:
        a = fp("s1", frozenset({"a.py"}), frozenset({"lockfiles"}))
        b = fp("s2", frozenset({"b.py"}), frozenset({"lockfiles"}))
        assert collide(a, b) == collide(b, a)


# ---------------------------------------------------------------- unknown footprint → SERIALIZE


class TestUnknownFootprint:
    def test_empty_footprint_a(self) -> None:
        a = fp("s1")
        b = fp("s2", frozenset({"verdict/router.py"}))
        verdict, reason = collide(a, b)
        assert verdict is CollisionVerdict.SERIALIZE
        assert reason is CollisionReason.UNKNOWN_FOOTPRINT

    def test_empty_footprint_b(self) -> None:
        a = fp("s1", frozenset({"verdict/router.py"}))
        b = fp("s2")
        verdict, reason = collide(a, b)
        assert verdict is CollisionVerdict.SERIALIZE
        assert reason is CollisionReason.UNKNOWN_FOOTPRINT

    def test_both_empty(self) -> None:
        verdict, reason = collide(fp("s1"), fp("s2"))
        assert verdict is CollisionVerdict.SERIALIZE
        assert reason is CollisionReason.UNKNOWN_FOOTPRINT

    def test_authorities_but_no_paths_still_unknown(self) -> None:
        a = fp("s1", authorities=frozenset({"migrations"}))
        b = fp("s2", frozenset({"verdict/b.py"}))
        verdict, reason = collide(a, b)
        assert verdict is CollisionVerdict.SERIALIZE
        assert reason is CollisionReason.UNKNOWN_FOOTPRINT

    def test_unknown_commutativity(self) -> None:
        a = fp("s1")
        b = fp("s2", frozenset({"x.py"}))
        assert collide(a, b) == collide(b, a)


# ---------------------------------------------------------------- frozen dataclass guarantees


class TestFrozenDataclass:
    def test_immutable(self) -> None:
        f = fp("s1", frozenset({"a.py"}))
        with pytest.raises(AttributeError):
            f.story_id = "s2"  # type: ignore[misc]

    def test_hashable(self) -> None:
        a = fp("s1", frozenset({"a.py"}))
        b = fp("s1", frozenset({"a.py"}))
        assert hash(a) == hash(b)
        assert a == b
        assert len({a, b}) == 1

    def test_different_stories_not_equal(self) -> None:
        a = fp("s1", frozenset({"a.py"}))
        b = fp("s2", frozenset({"a.py"}))
        assert a != b


# ---------------------------------------------------------------- edge cases


class TestEdgeCases:
    def test_same_story_id_different_paths(self) -> None:
        """Same story_id doesn't short-circuit — collision is path/authority based."""
        a = fp("s1", frozenset({"a.py"}))
        b = fp("s1", frozenset({"b.py"}))
        verdict, reason = collide(a, b)
        assert verdict is CollisionVerdict.PARALLEL
        assert reason is CollisionReason.DISJOINT

    def test_authority_checked_before_paths(self) -> None:
        """When both authority and path overlap, authority reason takes precedence."""
        a = fp("s1", frozenset({"verdict/api.py"}), frozenset({"schemas/contracts"}))
        b = fp("s2", frozenset({"verdict/api.py"}), frozenset({"schemas/contracts"}))
        verdict, reason = collide(a, b)
        assert verdict is CollisionVerdict.SERIALIZE
        assert reason is CollisionReason.SAME_AUTHORITY

    def test_question_mark_glob(self) -> None:
        a = fp("s1", frozenset({"verdict/?.py"}))
        b = fp("s2", frozenset({"verdict/a.py"}))
        verdict, reason = collide(a, b)
        assert verdict is CollisionVerdict.SERIALIZE
        assert reason is CollisionReason.GLOB_OVERLAP

    def test_bracket_glob(self) -> None:
        a = fp("s1", frozenset({"verdict/[abc].py"}))
        b = fp("s2", frozenset({"verdict/b.py"}))
        verdict, reason = collide(a, b)
        assert verdict is CollisionVerdict.SERIALIZE
        assert reason is CollisionReason.GLOB_OVERLAP
