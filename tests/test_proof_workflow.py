"""Tests for the proof workflow, its contract and its documentation (issue #1307).

They read ``.github/workflows/proof.yml``, ``proof/contract.yaml`` and
``proof/README.md`` as files, so a workflow edited without the contract (or the
reverse) fails here, before CI.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from scripts.proof.contract import load_contract

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "proof.yml"
CONTRACT = ROOT / "proof" / "contract.yaml"

# The steps of the single `proof-full` job on origin/main (34c10464), up to the one that
# runs the proof. Every leg repeats them, so a leg runs the dependency set, the tools and
# the history depth the single job ran with.
RECORDED_SETUP_STEPS: list[dict[str, Any]] = [
    {"uses": "actions/checkout@v7", "with": {"fetch-depth": 0}},
    {
        "name": "Set up Python",
        "uses": "actions/setup-python@v7",
        "with": {"python-version": "3.13", "cache": "pip"},
    },
    {"name": "Install uv", "uses": "astral-sh/setup-uv@37802adc94f370d6bfd71619e3f0bf239e1f3b78"},
    {"name": "Install dependencies", "run": "uv sync --extra dev --extra server --extra dashboard"},
    {"name": "Install Semgrep CE (isolated tool, not in the project venv)", "run": None},
    {"name": "Install build tooling", "run": "uv pip install build"},
]
RECORDED_SEMGREP_RUN = 'uv tool install semgrep\nuv tool dir --bin >> "$GITHUB_PATH"\n'
RECORDED_PROOF_FLAGS = [
    "--mode full",
    "--no-cache",
    "--otel",
    "--targets tests/test_proof_pipeline.py",
]


def _workflow() -> dict[str, Any]:
    loaded = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def _jobs() -> dict[str, dict[str, Any]]:
    jobs = _workflow()["jobs"]
    assert isinstance(jobs, dict)
    return jobs


def _needs(job: dict[str, Any]) -> list[str]:
    needs = job.get("needs", [])
    return [needs] if isinstance(needs, str) else list(needs)


def _shards() -> int:
    declared = load_contract(CONTRACT).shards
    assert declared is not None
    return declared


def _step_text(step: dict[str, Any]) -> str:
    return str(step.get("run", ""))


def _proof_step(job: dict[str, Any]) -> dict[str, Any]:
    steps = [s for s in job["steps"] if "scripts/proof/run.py" in _step_text(s)]
    assert len(steps) == 1
    return steps[0]


def test_the_leg_job_exists_and_the_matrix_lists_exactly_the_declared_legs() -> None:
    jobs = _jobs()
    assert "proof-shard" in jobs
    strategy = jobs["proof-shard"]["strategy"]
    assert strategy["matrix"]["shard"] == list(range(1, _shards() + 1))
    assert set(strategy["matrix"]) == {"shard"}
    assert strategy["fail-fast"] is False


def test_the_leg_job_waits_for_nothing_and_is_time_bounded() -> None:
    jobs = _jobs()
    assert "proof-shard" in jobs
    leg = jobs["proof-shard"]
    assert "needs" not in leg
    assert isinstance(leg["timeout-minutes"], int)
    assert 0 < leg["timeout-minutes"] < 360


def test_the_aggregate_is_named_proof_full_and_always_runs() -> None:
    jobs = _jobs()
    assert "proof-full" in jobs
    aggregate = jobs["proof-full"]
    assert aggregate.get("name", "proof-full") == "proof-full"
    assert _needs(aggregate) == ["proof-shard"]
    assert str(aggregate["if"]).strip() == "always()"
    assert isinstance(aggregate["timeout-minutes"], int)
    assert 0 < aggregate["timeout-minutes"] < 360


def test_the_leg_uploads_its_files_even_after_a_failure_and_can_be_re_run() -> None:
    jobs = _jobs()
    assert "proof-shard" in jobs
    uploads = [
        s
        for s in jobs["proof-shard"]["steps"]
        if str(s.get("uses", "")).startswith("actions/upload-artifact@")
    ]
    assert len(uploads) == 1
    upload = uploads[0]
    assert upload["uses"] == "actions/upload-artifact@v7"
    assert str(upload["if"]).strip() == "always()"
    assert upload["with"]["overwrite"] is True
    assert upload["with"]["name"] == "proof-shard-${{ matrix.shard }}"
    assert upload["with"]["path"] == "proof-shard-${{ matrix.shard }}/"
    assert upload["with"]["if-no-files-found"] == "warn"


def test_every_leg_repeats_the_setup_the_single_job_had() -> None:
    jobs = _jobs()
    assert "proof-shard" in jobs
    steps = jobs["proof-shard"]["steps"]
    for recorded, actual in zip(RECORDED_SETUP_STEPS, steps, strict=False):
        for key, value in recorded.items():
            if key == "run" and value is None:
                assert actual["run"] == RECORDED_SEMGREP_RUN
            elif key == "uses" and "setup-uv" in value:
                assert actual["uses"].startswith(value)
            else:
                assert actual.get(key) == value, (recorded.get("name"), key)
    assert len(steps) >= len(RECORDED_SETUP_STEPS) + 1


def test_every_leg_runs_the_proof_with_todays_flags_and_its_own_index() -> None:
    jobs = _jobs()
    assert "proof-shard" in jobs
    step = _proof_step(jobs["proof-shard"])
    assert step["env"]["PYTEST_ADDOPTS"] == "-p xdist -n auto"
    command = " ".join(step["run"].replace("\\\n", " ").split())
    assert command.startswith("uv run python scripts/proof/run.py")
    for flag in RECORDED_PROOF_FLAGS:
        assert flag in command
    assert "--shard ${{ matrix.shard }}" in command
    assert "--report proof-shard-${{ matrix.shard }}/proof-report-full.json" in command
    assert command.endswith("--json")
    assert "--llm-boundary" not in command


def test_the_aggregate_downloads_the_legs_and_runs_the_aggregate_script() -> None:
    jobs = _jobs()
    assert "proof-full" in jobs
    steps = jobs["proof-full"]["steps"]
    checkout = next(s for s in steps if str(s.get("uses", "")).startswith("actions/checkout@"))
    assert checkout["with"]["fetch-depth"] == 0
    setup = next(s for s in steps if str(s.get("uses", "")).startswith("actions/setup-python@"))
    assert setup["with"]["python-version"] == "3.13"
    deps = next(s for s in steps if "uv sync" in _step_text(s))
    assert deps["run"] == "uv sync --extra dev --extra server --extra dashboard"
    download = next(
        s for s in steps if str(s.get("uses", "")).startswith("actions/download-artifact@")
    )
    assert download["uses"] == "actions/download-artifact@v7"
    assert download["with"]["pattern"] == "proof-shard-*"
    assert download["with"]["path"] == "proof-legs"
    assert download.get("continue-on-error") is True
    assert download["with"].get("merge-multiple") in (None, False)
    run = next(s for s in steps if "scripts/proof/aggregate.py" in _step_text(s))
    command = " ".join(run["run"].replace("\\\n", " ").split())
    assert "--reports proof-legs" in command
    assert '--leg-result "${{ needs.proof-shard.result }}"' in command
    assert '--expected-sha "$GITHUB_SHA"' in command
    assert "--report proof-report-full.json" in command
    assert "--json" in command
    assert "PYTEST_ADDOPTS" not in str(run.get("env", {}))


def test_the_aggregate_uploads_the_merged_report_even_when_it_failed() -> None:
    jobs = _jobs()
    assert "proof-full" in jobs
    uploads = [
        s
        for s in jobs["proof-full"]["steps"]
        if str(s.get("uses", "")).startswith("actions/upload-artifact@")
    ]
    assert len(uploads) == 1
    assert str(uploads[0]["if"]).strip() == "always()"
    assert uploads[0]["with"]["name"] == "proof-report-full"
    assert uploads[0]["with"]["path"] == "proof-report-full.json"


def test_the_targeted_job_is_kept_and_nothing_waits_on_it() -> None:
    jobs = _jobs()
    assert "proof-targeted" in jobs
    for name, job in jobs.items():
        assert "proof-targeted" not in _needs(job), name


def test_the_matrix_freshness_job_is_still_there() -> None:
    assert "proof-matrix-freshness" in _jobs()


def test_no_workflow_or_script_writes_branch_protection() -> None:
    needles = ("required_status_checks", "/protection")
    offenders: list[str] = []
    candidates = [*(ROOT / ".github").rglob("*.yml"), *(ROOT / "scripts").rglob("*.py")]
    candidates += list((ROOT / "verdict").rglob("*.py"))
    for path in candidates:
        text = path.read_text(encoding="utf-8", errors="replace")
        if any(n in text for n in needles):
            offenders.append(path.relative_to(ROOT).as_posix())
    assert offenders == []
