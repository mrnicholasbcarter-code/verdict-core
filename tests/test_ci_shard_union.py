"""Unit tests for scripts/ci_shard_union.py comparison logic.

Tests the pure ``check_union`` function without invoking pytest itself.
"""

from __future__ import annotations

from scripts.ci_shard_union import check_union

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sets(*groups: set[str]) -> list[set[str]]:
    return list(groups)


# ---------------------------------------------------------------------------
# Happy-path: perfect 2-shard split
# ---------------------------------------------------------------------------


def test_perfect_split_has_no_issues() -> None:
    full = {"a", "b", "c", "d"}
    shards = _sets({"a", "b"}, {"c", "d"})
    missing, extra, dups = check_union(full, shards)
    assert missing == set()
    assert extra == set()
    assert dups == set()


def test_single_shard_equal_to_full() -> None:
    full = {"x", "y"}
    shards = _sets({"x", "y"})
    missing, extra, dups = check_union(full, shards)
    assert missing == set()
    assert extra == set()
    assert dups == set()


def test_empty_full_and_empty_shards() -> None:
    missing, extra, dups = check_union(set(), _sets(set()))
    assert missing == set()
    assert extra == set()
    assert dups == set()


# ---------------------------------------------------------------------------
# Missing tests
# ---------------------------------------------------------------------------


def test_detects_test_missing_from_all_shards() -> None:
    full = {"a", "b", "c"}
    shards = _sets({"a"}, {"b"})  # "c" not assigned
    missing, extra, dups = check_union(full, shards)
    assert missing == {"c"}
    assert extra == set()
    assert dups == set()


# ---------------------------------------------------------------------------
# Extra tests (in shard but not full)
# ---------------------------------------------------------------------------


def test_detects_extra_test_in_shard() -> None:
    full = {"a", "b"}
    shards = _sets({"a", "z"}, {"b"})  # "z" not in full
    missing, extra, dups = check_union(full, shards)
    assert missing == set()
    assert extra == {"z"}
    assert dups == set()


# ---------------------------------------------------------------------------
# Duplicate tests
# ---------------------------------------------------------------------------


def test_detects_duplicate_across_two_shards() -> None:
    full = {"a", "b", "c"}
    shards = _sets({"a", "b"}, {"b", "c"})  # "b" in both
    missing, extra, dups = check_union(full, shards)
    assert missing == set()
    assert extra == set()
    assert dups == {"b"}


def test_detects_duplicate_across_three_shards() -> None:
    full = {"a", "b", "c"}
    shards = _sets({"a"}, {"b", "a"}, {"c", "a"})  # "a" appears three times
    missing, extra, dups = check_union(full, shards)
    assert missing == set()
    assert extra == set()
    assert dups == {"a"}


# ---------------------------------------------------------------------------
# Combined issues
# ---------------------------------------------------------------------------


def test_reports_all_three_issues_simultaneously() -> None:
    full = {"a", "b", "c"}
    shards = _sets({"a", "b"}, {"b", "z"})  # missing "c", extra "z", dup "b"
    missing, extra, dups = check_union(full, shards)
    assert missing == {"c"}
    assert extra == {"z"}
    assert dups == {"b"}


# ---------------------------------------------------------------------------
# No shards
# ---------------------------------------------------------------------------


def test_no_shards_means_everything_missing() -> None:
    full = {"a", "b"}
    missing, extra, dups = check_union(full, [])
    assert missing == {"a", "b"}
    assert extra == set()
    assert dups == set()
