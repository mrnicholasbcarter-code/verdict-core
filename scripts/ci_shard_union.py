"""Verify pytest-split shard union equals the full test collection.

CLI usage (CI):
    python scripts/ci_shard_union.py [--splits 4] [--marker "not timing"] [--test-dir tests/]

Id mode (the proof legs and the proof aggregate, issue #1307):
    python scripts/ci_shard_union.py --emit-ids OUT.json -- <pytest arguments>

collects the node ids those pytest arguments select, without running a test, and
writes them to OUT.json as ``{"schema_version": 1, "count": N, "ids": [...]}`` in
collection order. ``PYTEST_ADDOPTS`` is ignored, so a workflow option cannot
change the selection. On a collection failure it exits 1 and leaves no file.

The script uses an in-process pytest plugin to collect test node IDs without
running any tests, then verifies two invariants:
  1. union(shards) == full_collection  (no test missing from every shard)
  2. shards are pairwise disjoint      (no test duplicated across shards)

The comparison logic (``check_union``) is a pure function so it can be unit-
tested without invoking pytest.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
from collections.abc import Iterator, Sequence
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Comparison logic (pure; unit-testable)
# ---------------------------------------------------------------------------


def check_union(full: set[str], shards: Sequence[set[str]]) -> tuple[set[str], set[str], set[str]]:
    """Return (missing, extra, duplicates) given a full set and shard sets.

    ``missing``    - nodeids in *full* but not in any shard.
    ``extra``      - nodeids in some shard but not in *full*.
    ``duplicates`` - nodeids that appear in more than one shard.
    """
    union: set[str] = set().union(*shards)
    missing = full - union
    extra = union - full

    duplicates: set[str] = set()
    seen: set[str] = set()
    for shard in shards:
        duplicates |= seen & shard
        seen |= shard

    return missing, extra, duplicates


# ---------------------------------------------------------------------------
# pytest collection via in-process plugin
# ---------------------------------------------------------------------------


class _NodeidCollector:
    """pytest plugin that records item nodeids after collection."""

    def __init__(self) -> None:
        self.nodeids: list[str] = []

    def pytest_collection_finish(self, session: pytest.Session) -> None:
        self.nodeids = [item.nodeid for item in session.items]


def collect_nodeids(args: list[str]) -> list[str]:
    """Collect test node IDs using an in-process pytest run (no subprocess).

    ``args`` is passed to ``pytest.main``; ``--collect-only`` and
    ``-p no:cacheprovider`` are appended automatically.
    """
    collector = _NodeidCollector()
    ret = pytest.main(
        [*args, "--collect-only", "-q", "-p", "no:cacheprovider"], plugins=[collector]
    )
    if ret not in (pytest.ExitCode.OK, pytest.ExitCode.NO_TESTS_COLLECTED):
        raise RuntimeError(f"pytest collection failed: exit {ret!r}")
    return collector.nodeids


@contextlib.contextmanager
def without_pytest_addopts() -> Iterator[None]:
    """Hide ``PYTEST_ADDOPTS`` for a block and restore the state found on entry."""
    saved = os.environ.pop("PYTEST_ADDOPTS", None)
    try:
        yield
    finally:
        if saved is None:
            os.environ.pop("PYTEST_ADDOPTS", None)
        else:
            os.environ["PYTEST_ADDOPTS"] = saved


def emit_ids(path: Path, pytest_args: Sequence[str]) -> int:
    """Write the ids ``pytest_args`` select to ``path``; return the process exit code."""
    path.unlink(missing_ok=True)
    try:
        with without_pytest_addopts():
            ids = collect_nodeids(list(pytest_args))
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"schema_version": 1, "count": len(ids), "ids": ids}
    path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    return 0


# ---------------------------------------------------------------------------
# Main check
# ---------------------------------------------------------------------------


def run_check(splits: int, marker: str, test_dir: str) -> bool:
    """Collect full set and each shard, then verify union and disjointness."""
    base: list[str] = [test_dir, "-m", marker]

    print("Collecting full test set ...")
    full = set(collect_nodeids(base))
    print(f"  {len(full)} tests")

    shards: list[set[str]] = []
    for group in range(1, splits + 1):
        print(f"Collecting shard {group}/{splits} ...")
        shard = set(collect_nodeids([*base, f"--splits={splits}", f"--group={group}"]))
        print(f"  {len(shard)} tests")
        shards.append(shard)

    missing, extra, duplicates = check_union(full, shards)
    ok = not (missing or extra or duplicates)

    if missing:
        print(f"MISSING - in full set but not in any shard ({len(missing)}):")
        for t in sorted(missing)[:40]:
            print(f"  < {t}")
    if extra:
        print(f"EXTRA - in shards but not in full set ({len(extra)}):")
        for t in sorted(extra)[:40]:
            print(f"  > {t}")
    if duplicates:
        print(f"DUPLICATES - appear in more than one shard ({len(duplicates)}):")
        for t in sorted(duplicates)[:40]:
            print(f"  ~ {t}")

    if ok:
        print(
            f"Shard union check PASSED: {len(full)} tests, {splits} shards, "
            f"union equals full set, shards are pairwise disjoint."
        )
    return ok


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--splits", type=int, default=4, help="number of shards (default 4)")
    parser.add_argument(
        "--marker", default="not timing", help='pytest -m marker (default "not timing")'
    )
    parser.add_argument("--test-dir", default="tests/", help="test directory (default tests/)")
    parser.add_argument(
        "--emit-ids",
        type=Path,
        metavar="OUT.json",
        help="write the ids that the pytest arguments after -- select, then stop",
    )
    parser.add_argument("pytest_args", nargs="*", help="pytest arguments (only with --emit-ids)")
    args = parser.parse_args(argv)
    if args.emit_ids is not None:
        return emit_ids(args.emit_ids, args.pytest_args or [args.test_dir])
    if args.pytest_args:
        parser.error("pytest arguments are only valid with --emit-ids")
    return 0 if run_check(args.splits, args.marker, args.test_dir) else 1


if __name__ == "__main__":
    sys.exit(main())
