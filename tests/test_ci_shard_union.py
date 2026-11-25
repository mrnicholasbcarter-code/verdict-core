"""Unit tests for scripts/ci_shard_union.py comparison logic.

Tests the pure ``check_union`` function without invoking pytest itself.
"""

from __future__ import annotations

import json
import os
import random
import subprocess
import sys
from pathlib import Path

import pytest

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


# ---------------------------------------------------------------------------
# --emit-ids: the selected node ids as JSON (used by the proof legs and aggregate)
# ---------------------------------------------------------------------------

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "ci_shard_union.py"

TOY_TESTS = """
import pytest

@pytest.mark.parametrize("value", [1, 2])
def test_param(value):
    assert value

def test_plain():
    assert True

@pytest.mark.timing
def test_timed():
    assert True
"""


def _toy_tree(tmp_path: Path) -> Path:
    tests = tmp_path / "toy" / "tests"
    tests.mkdir(parents=True)
    (tests / "test_toy.py").write_text(TOY_TESTS, encoding="utf-8")
    return tmp_path / "toy"


def _clean_env(**extra: str) -> dict[str, str]:
    env = {
        k: v for k, v in os.environ.items() if not k.startswith(("PYTEST_", "COV_", "COVERAGE_"))
    }
    env.update(extra)
    return env


def _emit(
    toy: Path, out: Path, *pytest_args: str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--emit-ids", str(out), "--", *pytest_args],
        cwd=toy,
        env=env if env is not None else _clean_env(),
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )


ALL_TOY_IDS = [
    "tests/test_toy.py::test_param[1]",
    "tests/test_toy.py::test_param[2]",
    "tests/test_toy.py::test_plain",
    "tests/test_toy.py::test_timed",
]


def test_emit_ids_writes_the_selected_ids_as_json(tmp_path: Path) -> None:
    toy = _toy_tree(tmp_path)
    out = tmp_path / "nested" / "ids.json"
    done = _emit(toy, out, "tests/", "-q")
    assert done.returncode == 0, done.stderr
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["ids"] == ALL_TOY_IDS
    assert payload["count"] == len(ALL_TOY_IDS)
    assert payload["schema_version"] == 1


def test_emit_ids_follows_the_marker_selection(tmp_path: Path) -> None:
    toy = _toy_tree(tmp_path)
    out = tmp_path / "ids.json"
    assert _emit(toy, out, "tests/", "-m", "timing").returncode == 0
    assert json.loads(out.read_text(encoding="utf-8"))["ids"] == [ALL_TOY_IDS[3]]
    assert _emit(toy, out, "tests/", "-m", "not timing").returncode == 0
    assert json.loads(out.read_text(encoding="utf-8"))["ids"] == ALL_TOY_IDS[:3]


@pytest.mark.parametrize(
    "addopts",
    [
        pytest.param("-p xdist -n auto", id="xdist"),
        pytest.param("-k test_plain", id="selection-option"),
        pytest.param("--deselect tests/test_toy.py::test_plain", id="deselect"),
    ],
)
def test_emit_ids_ignores_pytest_addopts(tmp_path: Path, addopts: str) -> None:
    toy = _toy_tree(tmp_path)
    out = tmp_path / "ids.json"
    done = _emit(toy, out, "tests/", env=_clean_env(PYTEST_ADDOPTS=addopts))
    assert done.returncode == 0, done.stderr
    assert json.loads(out.read_text(encoding="utf-8"))["ids"] == ALL_TOY_IDS


def test_emit_ids_with_the_leg_arguments_partitions_the_selection(tmp_path: Path) -> None:
    toy = _toy_tree(tmp_path)
    groups: list[list[str]] = []
    for group in (1, 2):
        out = tmp_path / f"ids-{group}.json"
        done = _emit(
            toy,
            out,
            "tests/",
            "-q",
            "--tb=short",
            "-m",
            "not timing",
            "--splits",
            "2",
            "--group",
            str(group),
            "--splitting-algorithm",
            "least_duration",
        )
        assert done.returncode == 0, done.stderr
        groups.append(json.loads(out.read_text(encoding="utf-8"))["ids"])
    missing, extra, duplicates = check_union(set(ALL_TOY_IDS[:3]), [set(g) for g in groups])
    assert (missing, extra, duplicates) == (set(), set(), set())
    assert sum(len(g) for g in groups) == 3
    assert all(groups)


def test_emit_ids_fails_and_writes_nothing_when_collection_breaks(tmp_path: Path) -> None:
    toy = _toy_tree(tmp_path)
    (toy / "tests" / "test_broken.py").write_text("def test_x(:\n", encoding="utf-8")
    out = tmp_path / "ids.json"
    out.write_text("stale", encoding="utf-8")
    done = _emit(toy, out, "tests/")
    assert done.returncode == 1
    assert "collection failed" in done.stderr
    assert not out.exists()


def test_the_addopts_guard_restores_the_variable_on_every_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts.ci_shard_union import without_pytest_addopts

    monkeypatch.setenv("PYTEST_ADDOPTS", "-k outer")
    with without_pytest_addopts():
        assert "PYTEST_ADDOPTS" not in os.environ
        with without_pytest_addopts():
            assert "PYTEST_ADDOPTS" not in os.environ
        assert "PYTEST_ADDOPTS" not in os.environ
    assert os.environ["PYTEST_ADDOPTS"] == "-k outer"
    with pytest.raises(RuntimeError), without_pytest_addopts():
        raise RuntimeError("boom")
    assert os.environ["PYTEST_ADDOPTS"] == "-k outer"
    monkeypatch.delenv("PYTEST_ADDOPTS")
    with without_pytest_addopts():
        assert "PYTEST_ADDOPTS" not in os.environ
    assert "PYTEST_ADDOPTS" not in os.environ


def test_default_mode_keeps_its_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    import scripts.ci_shard_union as union

    calls: list[tuple[int, str, str]] = []

    def fake_run_check(splits: int, marker: str, test_dir: str) -> bool:
        calls.append((splits, marker, test_dir))
        return True

    monkeypatch.setattr(union, "run_check", fake_run_check)
    assert union.main([]) == 0
    assert union.main(["--splits", "2", "--marker", "timing", "--test-dir", "x/"]) == 0
    assert calls == [(4, "not timing", "tests/"), (2, "timing", "x/")]


class _Item:
    """The two things pytest-split's algorithms read from a test item."""

    def __init__(self, nodeid: str) -> None:
        self.nodeid = nodeid

    def __str__(self) -> str:
        return f"<Function {self.nodeid.split('::')[-1]}>"


@pytest.mark.parametrize("splits", [1, 2, 3, 4, 5])
@pytest.mark.parametrize("seed", [0, 1, 2, 3])
def test_least_duration_groups_are_disjoint_and_cover_the_input(splits: int, seed: int) -> None:
    from pytest_split.algorithms import Algorithms

    rng = random.Random(seed)
    count = rng.randint(0, 120)
    # Repeated test names across modules and repeated durations are the hard case.
    nodeids = [
        f"tests/test_m{rng.randint(0, 9)}.py::test_n{rng.randint(0, 15)}[{i}]" for i in range(count)
    ]
    items = [_Item(n) for n in nodeids]
    durations = {n: rng.choice([0.1, 0.5, 0.5, 2.0]) for n in nodeids if rng.random() < 0.8}
    groups = Algorithms.least_duration.value(splits, items, durations)
    selections = [{item.nodeid for item in group.selected} for group in groups]
    assert len(groups) == splits
    assert sum(len(group.selected) for group in groups) == count
    assert check_union(set(nodeids), selections) == (set(), set(), set())


# ---------------------------------------------------------------------------
# run_check: the check CI runs (collects the full set and each shard)
# ---------------------------------------------------------------------------


def _fake_collection(
    monkeypatch: pytest.MonkeyPatch, full: set[str], shards: list[set[str]]
) -> list[list[str]]:
    """Make ``collect_nodeids`` answer with ``full`` first, then each shard; return its calls."""
    import scripts.ci_shard_union as union

    answers = iter([full, *shards])
    calls: list[list[str]] = []

    def fake(args: list[str]) -> list[str]:
        calls.append(list(args))
        return sorted(next(answers))

    monkeypatch.setattr(union, "collect_nodeids", fake)
    return calls


def test_the_exact_set_passes_and_the_logged_counts_add_up(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import platform

    import scripts.ci_shard_union as union

    full = {f"t{i}" for i in range(7)}
    _fake_collection(monkeypatch, full, [{"t0", "t1", "t2"}, {"t3", "t4"}, {"t5", "t6"}])
    assert union.run_check(3, "not timing", "tests/") is True
    out = capsys.readouterr().out
    assert "Shard union check PASSED: 7 tests = 3 + 2 + 2 across 3 shards" in out
    assert f"Python {platform.python_version()}" in out
    for forbidden in ("MISSING", "EXTRA", "DUPLICATES", "FAILED"):
        assert forbidden not in out


@pytest.mark.parametrize(
    ("shards", "heading", "marked"),
    [
        pytest.param([{"a"}, {"b"}], "MISSING", "< c", id="missing"),
        pytest.param([{"a", "b"}, {"b", "c"}], "DUPLICATES", "~ b", id="duplicated"),
        pytest.param([{"a", "z"}, {"b", "c"}], "EXTRA", "> z", id="extra"),
    ],
)
def test_a_missing_duplicated_or_extra_id_fails_the_check_and_is_named(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    shards: list[set[str]],
    heading: str,
    marked: str,
) -> None:
    import scripts.ci_shard_union as union

    _fake_collection(monkeypatch, {"a", "b", "c"}, shards)
    assert union.run_check(2, "not timing", "tests/") is False
    out = capsys.readouterr().out
    assert heading in out
    assert marked in out
    assert "PASSED" not in out
    assert "Shard union check FAILED" in out


def test_all_three_problems_at_once_are_all_reported(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import scripts.ci_shard_union as union

    _fake_collection(monkeypatch, {"a", "b", "c"}, [{"a", "b"}, {"b", "z"}])
    assert union.run_check(2, "not timing", "tests/") is False
    out = capsys.readouterr().out
    assert "< c" in out and "> z" in out and "~ b" in out
    assert (
        "Shard union check FAILED: 3 tests, shards hold 4 (missing 1, extra 1, duplicated 1)" in out
    )


@pytest.mark.parametrize(
    ("shards", "code"),
    [
        pytest.param([{"a", "b"}, {"c"}], 0, id="exact"),
        pytest.param([{"a", "b"}, set()], 1, id="missing"),
        pytest.param([{"a", "b"}, {"b", "c"}], 1, id="duplicated"),
        pytest.param([{"a", "b"}, {"c", "z"}], 1, id="extra"),
    ],
)
def test_the_command_exits_non_zero_exactly_when_the_check_fails(
    monkeypatch: pytest.MonkeyPatch, shards: list[set[str]], code: int
) -> None:
    import scripts.ci_shard_union as union

    _fake_collection(monkeypatch, {"a", "b", "c"}, shards)
    assert union.main(["--splits", "2"]) == code


def test_the_shards_are_collected_with_the_marker_and_split_flags_of_the_full_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import scripts.ci_shard_union as union

    calls = _fake_collection(monkeypatch, {"a", "b", "c"}, [{"a"}, {"b"}, {"c"}])
    assert union.main(["--splits", "3", "--marker", "not slow", "--test-dir", "x/"]) == 0
    base = ["x/", "-m", "not slow"]
    assert calls == [
        base,
        [*base, "--splits=3", "--group=1"],
        [*base, "--splits=3", "--group=2"],
        [*base, "--splits=3", "--group=3"],
    ]


def test_the_command_checks_a_real_pytest_split_and_logs_counts_that_add_up(tmp_path: Path) -> None:
    import platform
    import re

    toy = _toy_tree(tmp_path)
    done = subprocess.run(
        [sys.executable, str(SCRIPT), "--splits", "2", "--marker", "not timing"],
        cwd=toy,
        env=_clean_env(),
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    found = re.search(r"PASSED: (\d+) tests = (\d+) \+ (\d+) across 2 shards", done.stdout)
    assert found, done.stdout
    total, first, second = map(int, found.groups())
    assert total == first + second == 3
    assert f"Python {platform.python_version()}" in done.stdout
