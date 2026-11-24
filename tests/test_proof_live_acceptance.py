"""Tests for scripts/proof/live_acceptance.py (issue #1307, design D9).

The GitHub layer is one function (``run_gh``) and is replaced by a fake that serves
synthetic runs, jobs and artifacts. Reports, JUnit files and run data are synthetic,
so no test touches the network. Each subcommand must pass on good data and fail on
every wrong figure; the exit code is 0 (every assertion holds), 1 (one failed) or 2
(usage or fetch failure).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse
from xml.sax.saxutils import quoteattr

import pytest
import yaml

from scripts.proof import live_acceptance as live
from scripts.proof.live_acceptance import FetchError, diff_node_ids, main

UTC = timezone.utc
T0 = datetime(2026, 11, 25, 9, 0, 0, tzinfo=UTC)
SHARDS = 3
RESEARCH_MEDIAN = 531


def iso(seconds: float) -> str:
    return (T0 + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


def clean_git_env() -> dict[str, str]:
    """The environment for a throwaway repository.

    A git hook (the push gate runs these tests from one) exports GIT_DIR and friends. Left in
    place they override ``cwd=`` and ``git -C``, so ``git init`` and ``git commit`` would act
    on the real repository. Every GIT_* variable is removed, then the ones wanted are set.
    """
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(
        {
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.invalid",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.invalid",
        }
    )
    return env


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, env=clean_git_env(), capture_output=True, text=True, check=True
    ).stdout.strip()


# ---------------------------------------------------------------------------
# A contract and a fake GitHub
# ---------------------------------------------------------------------------

UNIT_FULL = ["pytest", "tests/", "-q", "--tb=short"]
UNIT_TIMING = [*UNIT_FULL, "-m", "timing", "-p", "no:xdist"]


def _contract_payload() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "name": "fixture-repo",
        "runner": "native",
        "modes": {
            "targeted": {"phases": ["format", "unit_targeted"]},
            "full": {
                "phases": ["format", "unit_targeted", "unit_full", "unit_timing", "adversarial"],
                "shards": SHARDS,
            },
        },
        "gates": [
            {"id": "format", "phase": "format", "classify": "format", "command": ["echo", "f"]},
            {
                "id": "unit_targeted",
                "phase": "unit_targeted",
                "classify": "test",
                "command": ["pytest", "{targets}", "-q", "--tb=short"],
                "targets_placeholder": "{targets}",
            },
            {
                "id": "unit_full",
                "phase": "unit_full",
                "classify": "test",
                "command": list(UNIT_FULL),
                "shard": "split",
            },
            {
                "id": "unit_timing",
                "phase": "unit_timing",
                "classify": "test",
                "command": list(UNIT_TIMING),
                "shard": "leader",
                "env_clear": ["PYTEST_ADDOPTS"],
            },
            {
                "id": "promptfoo",
                "phase": "adversarial",
                "classify": "adversarial",
                "command": ["echo", "p"],
                "when": "llm_boundary",
                "required": False,
            },
        ],
    }


@pytest.fixture
def contract_path(tmp_path: Path) -> Path:
    path = tmp_path / "contract.yaml"
    path.write_text(yaml.safe_dump(_contract_payload()), encoding="utf-8")
    return path


class FakeGitHub:
    """Stands in for ``run_gh``: serves runs, jobs and artifacts from dictionaries."""

    def __init__(self) -> None:
        self.runs: dict[str, list[dict[str, Any]]] = {}
        self.jobs: dict[int, list[dict[str, Any]]] = {}
        self.artifacts: dict[tuple[str, str], dict[str, str]] = {}
        self.calls: list[list[str]] = []
        self.fail_on: str | None = None

    def __call__(self, args: Sequence[str]) -> str:
        self.calls.append(list(args))
        if self.fail_on and self.fail_on in " ".join(args):
            raise FetchError(f"fake gh failure for {' '.join(args)}")
        if args[0] == "api":
            parsed = urlparse(args[1])
            query = parse_qs(parsed.query)
            page = int(query.get("page", ["1"])[0])
            runs_path = "repos/{owner}/{repo}/actions/runs"
            if parsed.path == runs_path:
                head = query["head_sha"][0]
                everything = self.runs.get(head, [])
                return json.dumps({"workflow_runs": everything[(page - 1) * 100 : page * 100]})
            match = re.fullmatch(r"repos/\{owner\}/\{repo\}/actions/runs/(\d+)/jobs", parsed.path)
            if match:
                jobs = self.jobs.get(int(match.group(1)), [])
                return json.dumps({"jobs": jobs[(page - 1) * 100 : page * 100]})
            raise FetchError(f"unexpected api path {args[1]}")
        if args[:2] == ["run", "download"]:
            run_id, name = args[2], args[args.index("-n") + 1]
            dest = Path(args[args.index("-D") + 1])
            files = self.artifacts.get((run_id, name))
            if files is None:
                raise FetchError(f"no artifact {name} on run {run_id}")
            for rel, text in files.items():
                (dest / rel).parent.mkdir(parents=True, exist_ok=True)
                (dest / rel).write_text(text, encoding="utf-8")
            return ""
        raise FetchError(f"unexpected gh call {list(args)}")


@pytest.fixture
def gh(monkeypatch: pytest.MonkeyPatch) -> FakeGitHub:
    fake = FakeGitHub()
    monkeypatch.setattr(live, "run_gh", fake)
    return fake


# ---------------------------------------------------------------------------
# merged-run and timing-leg: synthetic reports and JUnit files
# ---------------------------------------------------------------------------

TIMING_IDS = ["tests/test_t.py::test_a", "tests/test_t.py::test_b"]
LEG_IDS = [[f"tests/test_m{k}.py::test_x[{i}]" for i in range(4)] for k in range(1, SHARDS + 1)]
ALL_IDS = [i for leg in LEG_IDS for i in leg] + TIMING_IDS
PARENT_SUMMARY = f"===== {len(ALL_IDS) - 4} passed, 1 skipped, 2 xfailed in 42.00s ====="
PARENT_TOTAL = len(ALL_IDS) - 4 + 1 + 2  # one test fewer than the merged revision has


def _junit(cases: int, *, skipped: int = 0) -> str:
    rows = []
    for position in range(cases):
        attrs = f"classname={quoteattr('tests.t')} name={quoteattr(f'test_{position}')}"
        body = '<skipped type="pytest.skip" message="s"/>' if position < skipped else ""
        rows.append(f"<testcase {attrs}>{body}</testcase>")
    return f'<testsuites><testsuite name="pytest" tests="{cases}">{"".join(rows)}</testsuite></testsuites>'


def _leg_files(leg: int, *, unit_full: int, timing: int = 0, skipped: int = 0) -> dict[str, str]:
    files = {"junit-unit_full.xml": _junit(unit_full, skipped=skipped)}
    gates = [
        {"id": "unit_full", "status": "pass"},
        {"id": "unit_timing", "status": "pass" if leg == 1 else "skip"},
    ]
    if leg == 1:
        files["junit-unit_timing.xml"] = _junit(timing)
    files["proof-report-full.json"] = json.dumps(
        {"shard": {"index": leg, "count": SHARDS}, "gates": gates}
    )
    return files


def _merged_report(*, ok: bool = True, legs: int = SHARDS) -> str:
    return json.dumps({"ok": ok, "shards": [{"index": k} for k in range(1, legs + 1)]})


def _parent_report(summary: str = PARENT_SUMMARY) -> str:
    gates = [{"id": "unit_full", "status": "pass", "log_slice": f"...\n{summary}\n"}]
    return json.dumps({"ok": True, "gates": gates, "shard": None})


class Collected:
    """Replaces the collector: the full set at the checkout and the timing set."""

    def __init__(self, everything: list[str], timing: list[str]) -> None:
        self.everything, self.timing = everything, timing

    def __call__(self, args: Sequence[str], cwd: Path | None = None) -> list[str]:
        del cwd
        if "-m" in args and args[list(args).index("-m") + 1] == "timing":
            return list(self.timing)
        return list(self.everything)


def _seed_merged(
    gh: FakeGitHub, *, unit_full: list[int] | None = None, timing: int | None = None
) -> None:
    sizes = unit_full if unit_full is not None else [len(leg) for leg in LEG_IDS]
    gh.artifacts[("100", "proof-report-full")] = {"proof-report-full.json": _merged_report()}
    for leg in range(1, SHARDS + 1):
        gh.artifacts[("100", f"proof-shard-{leg}")] = _leg_files(
            leg, unit_full=sizes[leg - 1], timing=len(TIMING_IDS) if timing is None else timing
        )
    gh.artifacts[("90", "proof-report-full")] = {"proof-report-full.json": _parent_report()}


@pytest.fixture
def merged(
    gh: FakeGitHub, monkeypatch: pytest.MonkeyPatch, contract_path: Path
) -> tuple[FakeGitHub, Collected, list[str]]:
    _seed_merged(gh)
    collected = Collected(ALL_IDS, TIMING_IDS)
    monkeypatch.setattr(live, "collect_ids", collected)
    # The parent revision lacks one test the merged revision has: added 1, removed 0.
    revisions = {"parentrev": ALL_IDS[:-1], "mergedrev": ALL_IDS}
    monkeypatch.setattr(
        live, "collect_at_revision", lambda rev, repo, args=None: list(revisions[rev])
    )
    return gh, collected, ALL_IDS


def merged_args(contract_path: Path, **overrides: str) -> list[str]:
    values = {
        "--run": "100",
        "--parent-run": "90",
        "--parent-rev": "parentrev",
        "--merged-rev": "mergedrev",
        "--contract": str(contract_path),
    }
    values.update(overrides)
    return ["merged-run", *[x for pair in values.items() for x in pair]]


def test_merged_run_passes_on_good_data_and_prints_the_figures(
    merged: tuple[FakeGitHub, Collected, list[str]],
    contract_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = main(merged_args(contract_path))
    out = capsys.readouterr().out
    assert code == 0, out
    assert f"summed JUnit tests: {len(ALL_IDS)}" in out
    assert f"collected: {len(ALL_IDS)}" in out
    assert f"parent total: {PARENT_TOTAL}" in out
    assert "added: 1" in out and "removed: 0" in out
    assert "summed JUnit skipped: 0" in out
    assert "parent skipped: 1" in out and "parent xfailed: 2" in out


def test_merged_run_prints_the_summed_skips(
    merged: tuple[FakeGitHub, Collected, list[str]],
    contract_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    gh = merged[0]
    gh.artifacts[("100", "proof-shard-2")] = _leg_files(2, unit_full=len(LEG_IDS[1]), skipped=3)
    assert main(merged_args(contract_path)) == 0
    assert "summed JUnit skipped: 3" in capsys.readouterr().out


def _break(name: str) -> Callable[[FakeGitHub], None]:
    def report_not_ok(gh: FakeGitHub) -> None:
        gh.artifacts[("100", "proof-report-full")] = {
            "proof-report-full.json": _merged_report(ok=False)
        }

    def one_leg_missing(gh: FakeGitHub) -> None:
        gh.artifacts[("100", "proof-report-full")] = {
            "proof-report-full.json": _merged_report(legs=SHARDS - 1)
        }

    def one_leg_too_many(gh: FakeGitHub) -> None:
        gh.artifacts[("100", "proof-report-full")] = {
            "proof-report-full.json": _merged_report(legs=SHARDS + 1)
        }

    def junit_total_differs(gh: FakeGitHub) -> None:
        gh.artifacts[("100", "proof-shard-3")] = _leg_files(3, unit_full=len(LEG_IDS[2]) - 1)

    def timing_not_run(gh: FakeGitHub) -> None:
        gh.artifacts[("100", "proof-shard-1")] = _leg_files(1, unit_full=len(LEG_IDS[0]), timing=0)

    def parent_total_differs(gh: FakeGitHub) -> None:
        gh.artifacts[("90", "proof-report-full")] = {
            "proof-report-full.json": _parent_report("===== 5 passed in 1.00s =====")
        }

    def shard_index_wrong(gh: FakeGitHub) -> None:
        gh.artifacts[("100", "proof-report-full")] = {
            "proof-report-full.json": json.dumps(
                {"ok": True, "shards": [{"index": 1}, {"index": 1}, {"index": 3}]}
            )
        }

    cases = {
        "report not ok": report_not_ok,
        "one leg missing": one_leg_missing,
        "one leg too many": one_leg_too_many,
        "junit total differs from the collected count": junit_total_differs,
        "timing tests not run": timing_not_run,
        "parent total plus added differs": parent_total_differs,
        "leg indexes wrong": shard_index_wrong,
    }
    return cases[name]


@pytest.mark.parametrize(
    "name",
    [
        "report not ok",
        "one leg missing",
        "one leg too many",
        "junit total differs from the collected count",
        "timing tests not run",
        "parent total plus added differs",
        "leg indexes wrong",
    ],
)
def test_merged_run_fails_on_each_wrong_figure(
    merged: tuple[FakeGitHub, Collected, list[str]],
    contract_path: Path,
    capsys: pytest.CaptureFixture[str],
    name: str,
) -> None:
    _break(name)(merged[0])
    code = main(merged_args(contract_path))
    out = capsys.readouterr().out
    assert code == 1, out
    assert "FAIL" in out


def test_merged_run_fails_when_the_collected_count_differs_from_the_summed_junit(
    merged: tuple[FakeGitHub, Collected, list[str]],
    contract_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    merged[1].everything = [*ALL_IDS, "tests/test_extra.py::test_never_run"]
    code = main(merged_args(contract_path))
    assert code == 1
    assert "FAIL" in capsys.readouterr().out


def test_merged_run_counts_removed_tests_against_the_parent_total(
    merged: tuple[FakeGitHub, Collected, list[str]],
    contract_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    revisions = {"parentrev": [*ALL_IDS, "tests/test_gone.py::test_g"], "mergedrev": ALL_IDS}
    monkeypatch.setattr(
        live, "collect_at_revision", lambda rev, repo, args=None: list(revisions[rev])
    )
    merged[0].artifacts[("90", "proof-report-full")] = {
        "proof-report-full.json": _parent_report(f"===== {len(ALL_IDS) + 1} passed in 1.00s =====")
    }
    code = main(merged_args(contract_path))
    out = capsys.readouterr().out
    assert code == 0, out
    assert "removed: 1" in out


@pytest.mark.parametrize("fail_on", ["proof-report-full", "proof-shard-2", "90 "])
def test_merged_run_exits_two_when_an_artifact_cannot_be_fetched(
    merged: tuple[FakeGitHub, Collected, list[str]],
    contract_path: Path,
    capsys: pytest.CaptureFixture[str],
    fail_on: str,
) -> None:
    merged[0].fail_on = fail_on
    assert main(merged_args(contract_path)) == 2
    assert capsys.readouterr().err


def test_merged_run_exits_two_when_the_parent_summary_cannot_be_read(
    merged: tuple[FakeGitHub, Collected, list[str]],
    contract_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    merged[0].artifacts[("90", "proof-report-full")] = {
        "proof-report-full.json": _parent_report("no summary here")
    }
    assert main(merged_args(contract_path)) == 2
    assert "summary" in capsys.readouterr().err


def test_merged_run_exits_two_when_the_contract_declares_no_shards(
    merged: tuple[FakeGitHub, Collected, list[str]],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    payload = _contract_payload()
    del payload["modes"]["full"]["shards"]
    path = tmp_path / "noshards.yaml"
    path.write_text(yaml.safe_dump(payload), encoding="utf-8")
    assert main(merged_args(path)) == 2


def test_merged_run_requires_its_arguments(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as stop:
        main(["merged-run", "--run", "100"])
    assert stop.value.code == 2


def test_the_parent_summary_parser_reads_every_outcome_it_counts() -> None:
    from scripts.proof.live_acceptance import parse_pytest_summary

    line = "===== 13545 passed, 34 skipped, 27 xfailed, 2 xpassed, 1 failed, 3 errors, 5 warnings in 421.31s (0:07:01) ====="
    assert parse_pytest_summary("noise\n" + line + "\n") == {
        "passed": 13545,
        "skipped": 34,
        "xfailed": 27,
        "xpassed": 2,
        "failed": 1,
        "errors": 3,
        "warnings": 5,
    }
    assert parse_pytest_summary("12 passed in 0.5s") == {"passed": 12}
    assert parse_pytest_summary("nothing") is None


def test_the_summary_parser_reads_the_last_summary_line_not_an_earlier_one() -> None:
    from scripts.proof.live_acceptance import parse_pytest_summary

    text = "1 passed in 0.10s\nsome test printed this\n===== 40 passed, 2 skipped in 9.00s =====\n"
    assert parse_pytest_summary(text) == {"passed": 40, "skipped": 2}


# --- timing-leg ---


def timing_args(run: str = "100") -> list[str]:
    return ["timing-leg", "--run", run]


def test_timing_leg_passes_when_the_gate_passed_with_every_timing_test(
    gh: FakeGitHub, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed_merged(gh)
    monkeypatch.setattr(live, "collect_ids", Collected(ALL_IDS, TIMING_IDS))
    assert main(timing_args()) == 0
    out = capsys.readouterr().out
    assert f"unit_timing JUnit tests: {len(TIMING_IDS)}" in out
    assert f"collected timing tests: {len(TIMING_IDS)}" in out


def test_timing_leg_fails_when_the_gate_did_not_pass(
    gh: FakeGitHub, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed_merged(gh)
    files = _leg_files(1, unit_full=4, timing=len(TIMING_IDS))
    report = json.loads(files["proof-report-full.json"])
    report["gates"][1]["status"] = "fail"
    files["proof-report-full.json"] = json.dumps(report)
    gh.artifacts[("100", "proof-shard-1")] = files
    monkeypatch.setattr(live, "collect_ids", Collected(ALL_IDS, TIMING_IDS))
    assert main(timing_args()) == 1
    assert "FAIL" in capsys.readouterr().out


@pytest.mark.parametrize("junit_tests", [0, 1, 3])
def test_timing_leg_fails_when_the_count_differs_from_the_collected_timing_tests(
    gh: FakeGitHub,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    junit_tests: int,
) -> None:
    _seed_merged(gh, timing=junit_tests)
    monkeypatch.setattr(live, "collect_ids", Collected(ALL_IDS, TIMING_IDS))
    assert main(timing_args()) == 1


def test_timing_leg_fails_when_no_timing_test_is_collected(
    gh: FakeGitHub, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed_merged(gh, timing=0)
    monkeypatch.setattr(live, "collect_ids", Collected(ALL_IDS, []))
    code = main(timing_args())
    assert code == 1
    assert "FAIL" in capsys.readouterr().out


def test_timing_leg_fails_when_the_artifact_has_no_timing_junit(
    gh: FakeGitHub, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed_merged(gh)
    files = _leg_files(1, unit_full=4, timing=len(TIMING_IDS))
    del files["junit-unit_timing.xml"]
    gh.artifacts[("100", "proof-shard-1")] = files
    monkeypatch.setattr(live, "collect_ids", Collected(ALL_IDS, TIMING_IDS))
    assert main(timing_args()) == 1


def test_timing_leg_exits_two_on_a_fetch_failure(
    gh: FakeGitHub, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(live, "collect_ids", Collected(ALL_IDS, TIMING_IDS))
    assert main(timing_args("404")) == 2
    assert capsys.readouterr().err


# ---------------------------------------------------------------------------
# rounds
# ---------------------------------------------------------------------------

_ids = iter(range(1000, 10**6))


def _job(
    name: str, started: float | None, completed: float | None, conclusion: str = "success"
) -> dict[str, Any]:
    return {
        "name": name,
        "status": "completed" if completed is not None else "in_progress",
        "conclusion": conclusion if completed is not None else None,
        "started_at": iso(started) if started is not None else None,
        "completed_at": iso(completed) if completed is not None else None,
    }


def _add_head(
    gh: FakeGitHub,
    head: str,
    runs: list[tuple[str, float, float, list[dict[str, Any]]]],
    *,
    status: str = "completed",
    conclusion: str = "success",
    updated: float | None = None,
) -> None:
    """Runs of one head: (workflow name, created, last job end, jobs)."""
    entries = []
    for name, created, ends, jobs in runs:
        run_id = next(_ids)
        entries.append(
            {
                "id": run_id,
                "name": name,
                "event": "pull_request",
                "status": status,
                "conclusion": conclusion if status == "completed" else None,
                "created_at": iso(created),
                "updated_at": iso(updated if updated is not None else ends),
            }
        )
        gh.jobs[run_id] = jobs
    gh.runs[head] = entries


def pre_head(gh: FakeGitHub, head: str, *, total: float = 600.0, **kw: Any) -> None:
    _add_head(
        gh,
        head,
        [
            ("CI", 0, total - 100, [_job("test", 5, total - 100), _job("ci-ok", 5, total - 100)]),
            ("Proof", 0, total, [_job("proof-targeted", 40, 85), _job("proof-full", 90, total)]),
        ],
        **kw,
    )


def post_head(
    gh: FakeGitHub,
    head: str,
    *,
    ci_end: float = 420.0,
    leg_start: float = 20.0,
    leg_end: float = 300.0,
    aggregate_end: float = 340.0,
    legs: int = SHARDS,
    leg_conclusion: str = "success",
    aggregate_conclusion: str = "success",
    **kw: Any,
) -> None:
    proof_jobs = [_job("proof-targeted", 40, 85)]
    proof_jobs += [
        _job(f"proof-shard ({k})", leg_start + k, leg_end + 5 * k, leg_conclusion)
        for k in range(1, legs + 1)
    ]
    proof_jobs.append(_job("proof-full", leg_end + 20, aggregate_end, aggregate_conclusion))
    _add_head(
        gh,
        head,
        [
            ("CI", 0, ci_end, [_job("test", 5, ci_end - 40), _job("ci-ok", 5, ci_end)]),
            ("Proof", 0, aggregate_end, proof_jobs),
        ],
        **kw,
    )


def head_sha(side: str, index: int) -> str:
    return f"{(1 if side == 'pre' else 2):x}{index:039x}"


def write_heads(tmp_path: Path, side: str, count: int, first: int = 0) -> Path:
    path = tmp_path / f"{side}-heads.txt"
    path.write_text(
        "# newest first\n"
        + "\n".join(head_sha(side, i) for i in range(first, first + count))
        + "\n",
        encoding="utf-8",
    )
    return path


@pytest.fixture
def rounds_data(gh: FakeGitHub, tmp_path: Path) -> tuple[FakeGitHub, Path, Path]:
    for i in range(12):
        pre_head(gh, head_sha("pre", i), total=560 + i)
        post_head(gh, head_sha("post", i), ci_end=420 + i)
    return gh, write_heads(tmp_path, "pre", 12), write_heads(tmp_path, "post", 12)


def rounds_args(contract_path: Path, pre: Path, post: Path, rounds: int = 10) -> list[str]:
    return [
        "rounds",
        "--pre",
        str(pre),
        "--post",
        str(post),
        "--rounds",
        str(rounds),
        "--contract",
        str(contract_path),
    ]


def test_rounds_passes_on_good_data_and_prints_the_figures(
    rounds_data: tuple[FakeGitHub, Path, Path],
    contract_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _, pre, post = rounds_data
    code = main(rounds_args(contract_path, pre, post))
    out = capsys.readouterr().out
    assert code == 0, out
    for needle in (
        "pre-merge rounds: 10",
        "post-merge rounds: 10",
        "pre-merge median:",
        "post-merge median:",
        "range",
        "proof-full last to finish: 0 of 10",
        f"{RESEARCH_MEDIAN}",
        "leg start delay median",
        "leg duration spread",
    ):
        assert needle in out, needle


def test_the_round_ends_at_the_last_job_to_complete_not_at_the_run_update_time(
    gh: FakeGitHub, tmp_path: Path, contract_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Runs report an `updated_at` far after their last job: using it would fail the post side."""
    for i in range(11):
        pre_head(gh, head_sha("pre", i), total=600)
        post_head(gh, head_sha("post", i), ci_end=420, updated=420 + 400)
    code = main(
        rounds_args(
            contract_path, write_heads(tmp_path, "pre", 11), write_heads(tmp_path, "post", 11)
        )
    )
    out = capsys.readouterr().out
    assert code == 0, out
    assert "post-merge median: 420" in out


def test_a_round_starts_at_the_earliest_run_creation(
    gh: FakeGitHub, tmp_path: Path, contract_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    for i in range(11):
        pre_head(gh, head_sha("pre", i), total=600)
        post_head(gh, head_sha("post", i), ci_end=420)
        runs = gh.runs[head_sha("post", i)]
        runs[1]["created_at"] = iso(-30)
    assert (
        main(
            rounds_args(
                contract_path, write_heads(tmp_path, "pre", 11), write_heads(tmp_path, "post", 11)
            )
        )
        == 0
    )
    assert "post-merge median: 450" in capsys.readouterr().out


def _exclusion_case(name: str, gh: FakeGitHub, head: str) -> None:
    if name == "unfinished run":
        post_head(gh, head, status="in_progress")
    elif name == "cancelled run":
        post_head(gh, head, conclusion="cancelled")
    elif name == "run waiting for approval":
        post_head(gh, head, conclusion="action_required")
    elif name == "unfinished job":
        post_head(gh, head)
        gh.jobs[gh.runs[head][0]["id"]].append(_job("coverage", 50, None))
    elif name == "failed leg":
        post_head(gh, head, leg_conclusion="failure")
    elif name == "failed aggregate":
        post_head(gh, head, aggregate_conclusion="failure")
    elif name == "missing leg":
        post_head(gh, head, legs=SHARDS - 1)
    elif name == "extra leg":
        post_head(gh, head, legs=SHARDS + 1)
    elif name == "no aggregate":
        post_head(gh, head)
        jobs = gh.jobs[gh.runs[head][1]["id"]]
        jobs[:] = [j for j in jobs if j["name"] != "proof-full"]
    elif name == "two aggregates":
        post_head(gh, head)
        gh.jobs[gh.runs[head][1]["id"]].append(_job("proof-full", 300, 340))
    elif name == "no runs":
        gh.runs[head] = []
    else:
        raise AssertionError(name)


@pytest.mark.parametrize(
    ("name", "reason"),
    [
        ("unfinished run", "not finished"),
        ("cancelled run", "cancelled"),
        ("run waiting for approval", "approval"),
        ("unfinished job", "not finished"),
        ("failed leg", "proof-shard"),
        ("failed aggregate", "proof-full"),
        ("no aggregate", "proof-full"),
        ("two aggregates", "proof-full"),
        ("missing leg", "proof-shard"),
        ("extra leg", "proof-shard"),
        ("no runs", "no pull_request runs"),
    ],
)
def test_a_post_merge_head_that_does_not_count_is_excluded_reported_and_replaced(
    rounds_data: tuple[FakeGitHub, Path, Path],
    contract_path: Path,
    capsys: pytest.CaptureFixture[str],
    name: str,
    reason: str,
) -> None:
    gh, pre, post = rounds_data
    bad = head_sha("post", 3)
    _exclusion_case(name, gh, bad)
    code = main(rounds_args(contract_path, pre, post))
    out = capsys.readouterr().out
    assert code == 0, out
    assert "post-merge rounds: 10" in out
    excluded = [line for line in out.splitlines() if bad in line]
    assert excluded, out
    assert reason in excluded[0]
    assert "excluded" in excluded[0]


def test_the_replacement_head_is_the_next_one_in_the_list(
    rounds_data: tuple[FakeGitHub, Path, Path],
    contract_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    gh, pre, post = rounds_data
    _exclusion_case("cancelled run", gh, head_sha("post", 0))
    main(rounds_args(contract_path, pre, post))
    asked = {
        c[1].split("head_sha=")[1].split("&")[0]
        for c in gh.calls
        if c[0] == "api" and "head_sha=" in c[1]
    }
    assert head_sha("post", 10) in asked
    assert head_sha("post", 11) not in asked


def test_a_pre_merge_head_with_a_cancelled_run_is_excluded_too(
    rounds_data: tuple[FakeGitHub, Path, Path],
    contract_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    gh, pre, post = rounds_data
    pre_head(gh, head_sha("pre", 2), conclusion="cancelled")
    code = main(rounds_args(contract_path, pre, post))
    out = capsys.readouterr().out
    assert code == 0
    assert head_sha("pre", 2) in out and "cancelled" in out


@pytest.mark.parametrize("side", ["pre", "post"])
def test_fewer_counting_heads_than_requested_fails(
    gh: FakeGitHub,
    tmp_path: Path,
    contract_path: Path,
    capsys: pytest.CaptureFixture[str],
    side: str,
) -> None:
    for i in range(12):
        pre_head(gh, head_sha("pre", i), total=600)
        post_head(gh, head_sha("post", i), ci_end=420)
    for i in range(3):
        if side == "post":
            _exclusion_case("cancelled run", gh, head_sha("post", i))
        else:
            pre_head(gh, head_sha("pre", i), conclusion="cancelled")
    pre, post = write_heads(tmp_path, "pre", 12), write_heads(tmp_path, "post", 12)
    code = main(rounds_args(contract_path, pre, post))
    out = capsys.readouterr().out
    assert code == 1, out
    assert "FAIL" in out and "9" in out


def test_a_post_merge_median_above_the_research_median_fails(
    gh: FakeGitHub, tmp_path: Path, contract_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    for i in range(10):
        pre_head(gh, head_sha("pre", i), total=900)
        post_head(
            gh,
            head_sha("post", i),
            ci_end=RESEARCH_MEDIAN + 20,
            leg_end=RESEARCH_MEDIAN - 50,
            aggregate_end=RESEARCH_MEDIAN - 30,
        )
    code = main(
        rounds_args(
            contract_path, write_heads(tmp_path, "pre", 10), write_heads(tmp_path, "post", 10)
        )
    )
    out = capsys.readouterr().out
    assert code == 1
    assert re.search(r"FAIL.*(531|research)", out)


def test_a_post_merge_median_not_below_the_pre_merge_median_fails(
    gh: FakeGitHub, tmp_path: Path, contract_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    for i in range(10):
        pre_head(gh, head_sha("pre", i), total=430)
        post_head(gh, head_sha("post", i), ci_end=440)
    code = main(
        rounds_args(
            contract_path, write_heads(tmp_path, "pre", 10), write_heads(tmp_path, "post", 10)
        )
    )
    out = capsys.readouterr().out
    assert code == 1
    assert re.search(r"FAIL.*pre-merge", out)


def test_proof_full_last_to_finish_in_more_than_half_the_rounds_fails(
    gh: FakeGitHub, tmp_path: Path, contract_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    for i in range(10):
        pre_head(gh, head_sha("pre", i), total=600)
        last = i < 6
        post_head(
            gh,
            head_sha("post", i),
            ci_end=380,
            leg_end=400 if last else 300,
            aggregate_end=430 if last else 340,
        )
    code = main(
        rounds_args(
            contract_path, write_heads(tmp_path, "pre", 10), write_heads(tmp_path, "post", 10)
        )
    )
    out = capsys.readouterr().out
    assert code == 1
    assert "proof-full last to finish: 6 of 10" in out
    assert re.search(r"FAIL.*proof-full", out)


def test_proof_full_last_in_exactly_half_the_rounds_passes(
    gh: FakeGitHub, tmp_path: Path, contract_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    for i in range(10):
        pre_head(gh, head_sha("pre", i), total=600)
        last = i < 5
        post_head(
            gh,
            head_sha("post", i),
            ci_end=380,
            leg_end=400 if last else 300,
            aggregate_end=430 if last else 340,
        )
    code = main(
        rounds_args(
            contract_path, write_heads(tmp_path, "pre", 10), write_heads(tmp_path, "post", 10)
        )
    )
    assert code == 0, capsys.readouterr().out


def test_the_start_delay_and_leg_spread_are_recorded_not_asserted(
    gh: FakeGitHub, tmp_path: Path, contract_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    for i in range(10):
        pre_head(gh, head_sha("pre", i), total=600)
        post_head(gh, head_sha("post", i), ci_end=420, leg_start=120, leg_end=300)
    code = main(
        rounds_args(
            contract_path, write_heads(tmp_path, "pre", 10), write_heads(tmp_path, "post", 10)
        )
    )
    out = capsys.readouterr().out
    assert code == 0, out
    assert "leg start delay median: 122" in out
    assert "recorded, not asserted" in out
    assert "above 60 s" in out


def test_a_timestamp_without_an_offset_is_a_fetch_failure(
    rounds_data: tuple[FakeGitHub, Path, Path],
    contract_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    gh, pre, post = rounds_data
    gh.jobs[gh.runs[head_sha("post", 0)][0]["id"]][0]["completed_at"] = "2026-11-25T09:05:00"
    assert main(rounds_args(contract_path, pre, post)) == 2
    assert "offset" in capsys.readouterr().err


def test_a_job_without_a_completion_time_excludes_the_head(
    rounds_data: tuple[FakeGitHub, Path, Path],
    contract_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    gh, pre, post = rounds_data
    gh.jobs[gh.runs[head_sha("post", 1)][0]["id"]][0]["completed_at"] = None
    code = main(rounds_args(contract_path, pre, post))
    out = capsys.readouterr().out
    assert code == 0
    assert head_sha("post", 1) in out and "not finished" in out


def test_runs_of_other_events_are_not_part_of_a_round(
    rounds_data: tuple[FakeGitHub, Path, Path],
    contract_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    gh, pre, post = rounds_data
    gh.runs[head_sha("post", 0)].append(
        {
            "id": 999999,
            "name": "Release",
            "event": "push",
            "status": "in_progress",
            "conclusion": None,
            "created_at": iso(0),
            "updated_at": iso(9999),
        }
    )
    code = main(rounds_args(contract_path, pre, post))
    out = capsys.readouterr().out
    assert code == 0, out
    assert head_sha("post", 0) not in out
    assert "post-merge rounds: 10" in out
    assert "excluded" not in out


def test_a_head_list_with_a_bad_entry_is_a_usage_failure(
    rounds_data: tuple[FakeGitHub, Path, Path],
    contract_path: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _, pre, post = rounds_data
    bad = tmp_path / "bad.txt"
    bad.write_text("not-a-sha\n", encoding="utf-8")
    assert main(rounds_args(contract_path, bad, post)) == 2
    assert main(rounds_args(contract_path, pre, tmp_path / "missing.txt")) == 2


def test_the_same_head_listed_twice_is_not_two_rounds(
    gh: FakeGitHub, tmp_path: Path, contract_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    for i in range(12):
        pre_head(gh, head_sha("pre", i), total=600)
    post_head(gh, head_sha("post", 0), ci_end=420)
    post = tmp_path / "post-dup.txt"
    post.write_text("\n".join([head_sha("post", 0)] * 10) + "\n", encoding="utf-8")
    code = main(rounds_args(contract_path, write_heads(tmp_path, "pre", 12), post))
    captured = capsys.readouterr()
    assert code == 2
    assert "listed more than once" in captured.err
    assert head_sha("post", 0) in captured.err


def test_a_head_in_both_lists_is_refused(
    rounds_data: tuple[FakeGitHub, Path, Path],
    contract_path: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _, pre, _ = rounds_data
    shared = tmp_path / "shared.txt"
    shared.write_text(head_sha("pre", 1) + "\n" + head_sha("post", 1) + "\n", encoding="utf-8")
    code = main(rounds_args(contract_path, pre, shared))
    assert code == 2
    assert "both" in capsys.readouterr().err


def test_a_fetch_failure_in_rounds_exits_two(
    rounds_data: tuple[FakeGitHub, Path, Path],
    contract_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    gh, pre, post = rounds_data
    gh.fail_on = "head_sha"
    assert main(rounds_args(contract_path, pre, post)) == 2
    assert capsys.readouterr().err


def test_rounds_needs_a_positive_round_count(
    rounds_data: tuple[FakeGitHub, Path, Path], contract_path: Path
) -> None:
    _, pre, post = rounds_data
    assert main(rounds_args(contract_path, pre, post, rounds=0)) == 2


# ---------------------------------------------------------------------------
# unsharded: the report of a local run (task 7.4)
# ---------------------------------------------------------------------------


def _unsharded_report(**overrides: Any) -> dict[str, Any]:
    gates = [
        {
            "id": "format",
            "status": "pass",
            "exit_code": 0,
            "command": ["echo", "f"],
            "required": True,
        },
        {
            "id": "unit_targeted",
            "status": "skip",
            "exit_code": 0,
            "command": UNIT_FULL,
            "required": True,
        },
        {
            "id": "unit_full",
            "status": "pass",
            "exit_code": 0,
            "command": list(UNIT_FULL),
            "required": True,
        },
        {
            "id": "unit_timing",
            "status": "skip",
            "exit_code": 0,
            "command": list(UNIT_TIMING),
            "required": True,
        },
        {
            "id": "promptfoo",
            "status": "skip",
            "exit_code": 0,
            "command": ["echo", "p"],
            "required": False,
        },
    ]
    report: dict[str, Any] = {
        "ok": True,
        "mode": "full",
        "cache_mode": "no-cache",
        "shard": None,
        "targets": [],
        "gates": gates,
    }
    report.update(overrides)
    return report


def _write(tmp_path: Path, report: dict[str, Any]) -> Path:
    path = tmp_path / "proof-unsharded.json"
    path.write_text(json.dumps(report), encoding="utf-8")
    return path


def unsharded(path: Path, contract_path: Path) -> list[str]:
    return ["unsharded", "--report", str(path), "--contract", str(contract_path)]


def test_an_unsharded_report_that_ran_the_unchanged_command_passes(
    tmp_path: Path, contract_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(unsharded(_write(tmp_path, _unsharded_report()), contract_path))
    out = capsys.readouterr().out
    assert code == 0, out
    assert "shard: null" in out and "unit_timing: skip" in out


def _gate(report: dict[str, Any], gate_id: str) -> dict[str, Any]:
    return next(g for g in report["gates"] if g["id"] == gate_id)


UNSHARDED_BREAKS: dict[str, Callable[[dict[str, Any]], None]] = {
    "shard is an object": lambda r: r.update(shard={"index": 1, "count": 3}),
    "shard key missing": lambda r: r.pop("shard"),
    "unit_timing ran": lambda r: _gate(r, "unit_timing").update(status="pass"),
    "unit_timing missing": lambda r: r["gates"].remove(_gate(r, "unit_timing")),
    "unit_full has a marker filter": lambda r: _gate(r, "unit_full").update(
        command=[*UNIT_FULL, "-m", "not timing"]
    ),
    "unit_full has a split": lambda r: _gate(r, "unit_full").update(
        command=[*UNIT_FULL, "--splits", "3", "--group", "1"]
    ),
    "unit_full has a deselect": lambda r: _gate(r, "unit_full").update(
        command=[*UNIT_FULL, "--deselect", "tests/x.py::y"]
    ),
    "unit_full did not pass": lambda r: _gate(r, "unit_full").update(status="fail"),
    "unit_full missing": lambda r: r["gates"].remove(_gate(r, "unit_full")),
    "a passed gate with a nonzero exit code": lambda r: _gate(r, "format").update(exit_code=1),
    "a passed unit_full with a nonzero exit code": lambda r: _gate(r, "unit_full").update(
        exit_code=2
    ),
    "a contract-skipped gate with a nonzero exit code": lambda r: _gate(r, "unit_timing").update(
        exit_code=127
    ),
    "a gate with no exit code": lambda r: _gate(r, "format").pop("exit_code"),
    "an exit code that is not an integer": lambda r: _gate(r, "unit_full").update(exit_code="0"),
    "an exit code that is a boolean": lambda r: _gate(r, "unit_full").update(exit_code=False),
    "report not ok": lambda r: r.update(ok=False),
    "report is not full mode": lambda r: r.update(mode="targeted"),
    "report is not no-cache": lambda r: r.update(cache_mode="cache"),
    "another gate's command changed": lambda r: _gate(r, "format").update(command=["true"]),
    "an extra gate": lambda r: r["gates"].append(
        {"id": "x", "status": "pass", "command": ["x"], "required": True}
    ),
}


@pytest.mark.parametrize("name", sorted(UNSHARDED_BREAKS))
def test_an_unsharded_report_that_is_not_what_a_local_run_writes_fails(
    tmp_path: Path, contract_path: Path, capsys: pytest.CaptureFixture[str], name: str
) -> None:
    report = _unsharded_report()
    UNSHARDED_BREAKS[name](report)
    code = main(unsharded(_write(tmp_path, report), contract_path))
    out = capsys.readouterr().out
    assert code == 1, out
    assert "FAIL" in out


def test_an_unsharded_report_is_judged_by_its_content_not_only_by_success(
    tmp_path: Path, contract_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A run that exits 0 but was a leg of a sharded proof must not pass for a local run."""
    leg = _unsharded_report(shard={"index": 2, "count": 3})
    _gate(leg, "unit_full").update(
        command=[*UNIT_FULL, "-m", "not timing", "--splits", "3", "--group", "2"]
    )
    assert leg["ok"] is True
    assert main(unsharded(_write(tmp_path, leg), contract_path)) == 1


@pytest.mark.parametrize("content", ["{not json", "[1]", ""])
def test_an_unreadable_unsharded_report_is_a_failure_to_check(
    tmp_path: Path, contract_path: Path, capsys: pytest.CaptureFixture[str], content: str
) -> None:
    path = tmp_path / "r.json"
    path.write_text(content, encoding="utf-8")
    assert main(unsharded(path, contract_path)) == 2
    assert main(unsharded(tmp_path / "missing.json", contract_path)) == 2


def test_an_unsharded_report_with_wrong_typed_gates_is_a_failure_to_check(
    tmp_path: Path, contract_path: Path
) -> None:
    assert main(unsharded(_write(tmp_path, _unsharded_report(gates="x")), contract_path)) == 2
    assert main(unsharded(_write(tmp_path, _unsharded_report(gates=["x"])), contract_path)) == 2


# ---------------------------------------------------------------------------
# The added-test count comes from collected node ids (finding 4)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("parent", "merged", "added", "removed"),
    [
        ([], [], 0, 0),
        (["a::t[1]", "a::t[2]"], ["a::t[1]", "a::t[2]", "a::t[3]", "a::t[4]", "a::t[5]"], 3, 0),
        (["a::t[1]", "a::t[2]", "a::t[3]"], ["a::t[1]"], 0, 2),
        (["a::t"], ["b::t"], 1, 1),
        (["a::t", "b::t"], ["b::t", "a::t"], 0, 0),
        (["a::t[1]"], ["a::t[1]", "a::new_test", "a::new_test2[x]", "a::new_test2[y]"], 3, 0),
    ],
    ids=["nothing", "parametrization", "parameters removed", "rename", "reorder", "new tests"],
)
def test_the_added_count_is_the_difference_of_node_ids(
    parent: list[str], merged: list[str], added: int, removed: int
) -> None:
    assert diff_node_ids(parent, merged) == (added, removed)


def test_the_added_count_counts_each_parametrized_case_not_each_test_function(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Real git, real archive, real collection: a function-count diff would say 1."""
    for name in ("PYTEST_XDIST_WORKER", "PYTEST_XDIST_WORKER_COUNT", "PYTEST_CURRENT_TEST"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("PYTEST_ADDOPTS", raising=False)
    repo = tmp_path / "repo"
    repo.mkdir()
    env = clean_git_env()

    def git(*args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=repo, env=env, capture_output=True, text=True, check=True
        ).stdout.strip()

    git("init", "-q", "-b", "main")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_p.py").write_text(
        "import pytest\n\n@pytest.mark.parametrize('v', [1, 2])\ndef test_p(v):\n    assert v\n"
    )
    git("add", "-A")
    git("commit", "-q", "-m", "parent")
    parent = git("rev-parse", "HEAD")
    (repo / "tests" / "test_p.py").write_text(
        "import pytest\n\n@pytest.mark.parametrize('v', [1, 2, 3, 4, 5])\ndef test_p(v):\n    assert v\n\n"
        "def test_new():\n    assert True\n"
    )
    git("add", "-A")
    git("commit", "-q", "-m", "merged")
    merged = git("rev-parse", "HEAD")

    before = {p.name for p in tmp_path.iterdir()}
    parent_ids = live.collect_at_revision(parent, repo)
    merged_ids = live.collect_at_revision(merged, repo)
    assert parent_ids == ["tests/test_p.py::test_p[1]", "tests/test_p.py::test_p[2]"]
    assert diff_node_ids(parent_ids, merged_ids) == (4, 0)
    assert {p.name for p in tmp_path.iterdir()} == before
    assert git("worktree", "list").count("\n") == 0
    assert git("status", "--porcelain") == ""


def test_the_temporary_checkout_is_removed_when_collection_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    created: list[Path] = []
    real = live.checkout_revision

    def spy(rev: str, repo: Path) -> Any:
        manager = real(rev, repo)
        return _Spy(manager, created)

    class _Spy:
        def __init__(self, manager: Any, sink: list[Path]) -> None:
            self.manager, self.sink = manager, sink

        def __enter__(self) -> Path:
            path = self.manager.__enter__()
            self.sink.append(path)
            return path  # type: ignore[no-any-return]

        def __exit__(self, *exc: object) -> Any:
            return self.manager.__exit__(*exc)

    monkeypatch.setattr(live, "checkout_revision", spy)
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_broken.py").write_text("def test_x(:\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "x")
    rev = _git(repo, "rev-parse", "HEAD")
    with pytest.raises(FetchError):
        live.collect_at_revision(rev, repo)
    assert created, "the checkout was never made"
    assert all(not p.exists() for p in created)


def test_the_checkout_of_an_unknown_revision_is_a_fetch_failure(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    with pytest.raises(FetchError):
        live.collect_at_revision("0" * 40, repo)


def test_the_checkout_reads_the_repository_it_is_given_not_the_one_a_hook_runs_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    decoy = tmp_path / "decoy"
    real = tmp_path / "real"
    for path, text in ((decoy, "DECOY\n"), (real, "REAL\n")):
        path.mkdir()
        _git(path, "init", "-q", "-b", "main")
        (path / "marker.txt").write_text(text)
        _git(path, "add", "-A")
        _git(path, "commit", "-q", "-m", "x")
    rev = _git(real, "rev-parse", "HEAD")
    monkeypatch.setenv("GIT_DIR", str(decoy / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(decoy))
    with live.checkout_revision(rev, real) as checkout:
        assert (checkout / "marker.txt").read_text() == "REAL\n"
    assert _git(decoy, "status", "--porcelain") == ""


def test_the_module_lists_its_subcommands(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as stop:
        main(["--help"])
    assert stop.value.code == 0
    out = capsys.readouterr().out
    for name in ("merged-run", "timing-leg", "rounds", "unsharded"):
        assert name in out


# ---------------------------------------------------------------------------
# The real gh layer, with a stand-in gh executable
# ---------------------------------------------------------------------------


def _fake_gh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str) -> Path:
    shim = tmp_path / "ghbin"
    shim.mkdir()
    (shim / "gh").write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    (shim / "gh").chmod(0o755)
    monkeypatch.setenv("PATH", f"{shim}{os.pathsep}{os.environ['PATH']}")
    return shim


def test_the_real_gh_layer_returns_stdout_on_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_gh(tmp_path, monkeypatch, 'echo "args: $@"')
    assert live.run_gh(["api", "x"]).strip() == "args: api x"


def test_the_real_gh_layer_turns_a_failing_gh_into_a_fetch_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_gh(tmp_path, monkeypatch, 'echo "HTTP 404" >&2; exit 1')
    with pytest.raises(FetchError, match="404"):
        live.run_gh(["run", "download", "1"])


def test_the_real_gh_layer_reports_a_missing_gh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    with pytest.raises(FetchError, match="not installed"):
        live.run_gh(["api", "x"])


def test_a_gh_answer_that_is_not_json_is_a_fetch_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_gh(tmp_path, monkeypatch, "echo not-json")
    with pytest.raises(FetchError, match="not JSON"):
        live.gh_json("repos/{owner}/{repo}/actions/runs")
