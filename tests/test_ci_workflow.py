"""Tests that pin what ``ci-ok`` requires and how the shard union checks are wired (issues #1332, #1341).

They read ``.github/workflows/ci.yml`` as a file. ``ci-ok`` is the single required check,
so a job quietly dropped from its ``needs`` or moved from "required" to "may skip" would
let a red job merge. Each pin below is a ``check_*`` function that raises
``AssertionError``. The tests run it on the real workflow (it must pass) and on a mutated
copy (it must fail, for the stated reason), so a pin that cannot fail is caught here.
"""

from __future__ import annotations

import copy
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"

# What ci-ok treats as required and as skippable. Stage 2 (#1341) added `shard-union` and made
# `changes` required: it was the one job outside ci-ok, and when it failed every code-only job
# was skipped, which ci-ok accepts for a skippable job.
REQUIRED = (
    "changes",
    "test",
    "test-shard",
    "shard-union",
    "coverage",
    "lint",
    "security",
    "type-check",
)
SKIPPABLE = ("install-smoke", "build", "typescript-contract-parity", "client-package-smoke")
CI_OK_NEEDS = frozenset(REQUIRED + SKIPPABLE)
NOT_GATED = frozenset({"ci-ok"})

BUILD_IF = "needs.changes.outputs.code == 'true'"
UNION_JOB = "shard-union"
UNION_SCRIPT = "scripts/ci_shard_union.py"
UNION_SPLITS = "${{ matrix.splits }}"
MARKER = "not timing"
# The jobs that run pytest in shards; each needs a union leg under its own Python version.
SHARDED_JOBS = ("test", "test-shard")
SHARD_JOB = "test-shard"
SHARD_ARTIFACT = "coverage-shard-${{ matrix.group }}"
SHARD_ARTIFACT_PATTERN = "coverage-shard-*"
COVERAGE_JOB = "coverage"
COVERAGE_FLOOR = "70"
ONE_LEG_IF = "matrix.group == 1"
EVENTS = ("pull_request", "other")

Workflow = dict[str, Any]

_BASH = shutil.which("bash")
_HAS_BASH = _BASH is not None


def _workflow() -> Workflow:
    loaded = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def _jobs(wf: Workflow) -> dict[str, Any]:
    jobs = wf["jobs"]
    assert isinstance(jobs, dict)
    return jobs


def _job(wf: Workflow, name: str) -> dict[str, Any]:
    jobs = _jobs(wf)
    assert name in jobs, f"job {name!r} is missing from ci.yml"
    return jobs[name]


def _needs(job: dict[str, Any]) -> list[str]:
    needs = job.get("needs", [])
    return [needs] if isinstance(needs, str) else list(needs)


def _gate_step(wf: Workflow) -> dict[str, Any]:
    steps = [s for s in _job(wf, "ci-ok")["steps"] if s.get("name") == "Evaluate gate"]
    assert len(steps) == 1, "ci-ok must have exactly one 'Evaluate gate' step"
    return steps[0]


def _gate_loops(script: str) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """Return the (job, env var) pairs of the required loop and of the skippable loop."""
    required: list[tuple[str, str]] = []
    skippable: list[tuple[str, str]] = []
    loops = re.findall(r"for name_result in\s*\\?\s*(.*?)\bdo\b(.*?)\bdone\b", script, re.DOTALL)
    for header, body in loops:
        pairs = re.findall(r'"([a-z0-9-]+):\$([A-Z_]+)"', header)
        if '!= "success"' in body:
            required.extend(pairs)
        elif '= "failure"' in body and '= "cancelled"' in body:
            skippable.extend(pairs)
        else:
            raise AssertionError(f"unrecognised gate loop in ci-ok: {body.strip()[:80]!r}")
    return required, skippable


def _result_vars(wf: Workflow) -> dict[str, str]:
    """Map each job to the env var that carries its result into the gate script."""
    mapped: dict[str, str] = {}
    for var, value in (_gate_step(wf).get("env") or {}).items():
        found = re.fullmatch(r"\$\{\{\s*needs\.([a-z0-9-]+)\.result\s*\}\}", str(value).strip())
        assert found, f"{var} is not a needs.<job>.result expression: {value!r}"
        assert found.group(1) not in mapped, f"two env vars carry the result of {found.group(1)}"
        mapped[found.group(1)] = var
    return mapped


def check_ci_ok_needs(wf: Workflow) -> None:
    ci_ok = _job(wf, "ci-ok")
    needs = _needs(ci_ok)
    assert len(needs) == len(set(needs)), f"ci-ok needs lists a job twice: {needs}"
    assert set(needs) == CI_OK_NEEDS, (
        f"ci-ok needs changed: missing {sorted(CI_OK_NEEDS - set(needs))}, "
        f"extra {sorted(set(needs) - CI_OK_NEEDS)}"
    )
    assert str(ci_ok.get("if", "")).strip() == "always()", "ci-ok must run with if: always()"


def check_every_job_is_gated(wf: Workflow) -> None:
    ungated = set(_jobs(wf)) - set(_needs(_job(wf, "ci-ok"))) - NOT_GATED
    assert not ungated, f"jobs that no required check waits for: {sorted(ungated)}"


def check_required_and_skippable(wf: Workflow) -> None:
    required, skippable = _gate_loops(str(_gate_step(wf).get("run", "")))
    assert sorted(n for n, _ in required) == sorted(REQUIRED), (
        f"ci-ok required jobs changed: {[n for n, _ in required]}"
    )
    assert sorted(n for n, _ in skippable) == sorted(SKIPPABLE), (
        f"ci-ok skippable jobs changed: {[n for n, _ in skippable]}"
    )
    mapped = _result_vars(wf)
    for name, var in required + skippable:
        assert mapped.get(name) == var, f"{var} does not carry the result of {name}"
    assert set(mapped) == CI_OK_NEEDS, f"ci-ok env results changed: {sorted(mapped)}"


def _assert_default_shell(wf: Workflow, job_id: str, step: dict[str, Any]) -> None:
    """A step runs under GitHub's default ``bash -e`` unless a shell is set at some level."""
    levels = {
        "the step": step.get("shell"),
        f"{job_id} defaults": ((_job(wf, job_id).get("defaults") or {}).get("run") or {}).get(
            "shell"
        ),
        "the workflow defaults": ((wf.get("defaults") or {}).get("run") or {}).get("shell"),
    }
    set_at = [where for where, shell in levels.items() if shell is not None]
    assert not set_at, (
        f"{job_id} must run its step under the default shell, but it is set in {set_at}"
    )


def check_gate_cannot_be_bypassed(wf: Workflow) -> None:
    """The gate's exit code must decide ci-ok: no setting may turn a failing script green."""
    ci_ok = _job(wf, "ci-ok")
    step = _gate_step(wf)
    assert "continue-on-error" not in ci_ok, "ci-ok must not set continue-on-error"
    for key in ("if", "continue-on-error"):
        assert key not in step, f"the ci-ok gate step must not set {key!r}"
    _assert_default_shell(wf, "ci-ok", step)
    assert ci_ok["steps"] == [step], "ci-ok must run only its 'Evaluate gate' step"


def _run_gate(wf: Workflow, results: dict[str, str]) -> int:
    mapped = _result_vars(wf)
    env = {mapped[name]: result for name, result in results.items()}
    done = subprocess.run(
        [_BASH or "bash", "-e", "-c", str(_gate_step(wf)["run"])],
        env={"PATH": "/usr/bin:/bin", **env},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    return done.returncode


def check_gate_outcome(wf: Workflow, name: str, result: str) -> None:
    """Run the gate script with every job green except ``name`` and check its exit code."""
    results = {job: "success" for job in CI_OK_NEEDS}
    results[name] = result
    passes = result == "success" or (name in SKIPPABLE and result == "skipped")
    code = _run_gate(wf, results)
    assert (code == 0) == passes, (
        f"gate exit code {code} for {name}={result}: "
        f"{'must pass' if passes else 'must fail (not allowed to merge)'}"
    )


def _union_steps(wf: Workflow) -> list[tuple[str, dict[str, Any]]]:
    return [
        (job_id, step)
        for job_id, job in _jobs(wf).items()
        for step in job.get("steps", [])
        if UNION_SCRIPT in str(step.get("run", ""))
    ]


def _flag(text: str, pattern: str) -> str | None:
    found = re.search(pattern, text)
    return found.group(1) if found else None


_EVENT_TERNARY = re.compile(
    r"\$\{\{\s*\(github\.event_name == 'pull_request'\)\s*&&\s*fromJson\('([^']*)'\)"
    r"\s*\|\|\s*fromJson\('([^']*)'\)\s*\}\}"
)


def _by_event(value: Any) -> dict[str, list[Any]]:
    """A matrix value as it resolves on a pull request and on any other event."""
    if isinstance(value, list):
        return {"pull_request": list(value), "other": list(value)}
    found = _EVENT_TERNARY.fullmatch(str(value).strip())
    assert found, f"a matrix value must be a list or the pull-request ternary, not {value!r}"
    return {"pull_request": json.loads(found.group(1)), "other": json.loads(found.group(2))}


def _matrix(wf: Workflow, job_id: str) -> dict[str, Any]:
    matrix = (_job(wf, job_id).get("strategy") or {}).get("matrix")
    assert isinstance(matrix, dict), f"{job_id} has no strategy matrix"
    return matrix


def _groups(wf: Workflow, job_id: str) -> list[int]:
    groups = _matrix(wf, job_id).get("group")
    assert isinstance(groups, list), f"{job_id} must have a matrix of shard groups"
    return groups


def _setup_python(wf: Workflow, job_id: str) -> str:
    steps = [
        s
        for s in _job(wf, job_id)["steps"]
        if str(s.get("uses", "")).startswith("actions/setup-python")
    ]
    assert len(steps) == 1, f"{job_id} must set up Python in exactly one step"
    return str(steps[0]["with"]["python-version"]).strip()


def _pythons(wf: Workflow, job_id: str, event: str) -> list[str]:
    version = _setup_python(wf, job_id)
    if version == "${{ matrix.python-version }}":
        return [str(v) for v in _by_event(_matrix(wf, job_id)["python-version"])[event]]
    return [version]


def _union_legs(wf: Workflow, event: str) -> list[tuple[str, int]]:
    """The (Python version, splits) of each leg of the union job on a pull request or otherwise."""
    matrix = _matrix(wf, UNION_JOB)
    assert set(matrix) == {"include"}, (
        f"the {UNION_JOB} matrix must be an include list only, found {sorted(matrix)}"
    )
    legs = _by_event(matrix["include"])[event]
    for leg in legs:
        assert isinstance(leg, dict) and set(leg) == {"python-version", "splits"}, (
            f"a {UNION_JOB} leg must set exactly python-version and splits, not {leg!r}"
        )
    return [(str(leg["python-version"]), int(leg["splits"])) for leg in legs]


def _expected_legs(wf: Workflow, event: str) -> list[tuple[str, int]]:
    """One union leg per Python version of each sharded job, with that job's shard count."""
    return [
        (python, len(_groups(wf, job_id)))
        for job_id in SHARDED_JOBS
        for python in _pythons(wf, job_id, event)
    ]


def _split_runs(wf: Workflow, job_id: str) -> list[str]:
    return [
        str(s["run"])
        for s in _job(wf, job_id).get("steps", [])
        if "pytest" in str(s.get("run", "")) and "--splits" in str(s.get("run", ""))
    ]


def _install_run(wf: Workflow, job_id: str) -> str:
    runs = [
        str(s["run"])
        for s in _job(wf, job_id)["steps"]
        if "pip install -e" in str(s.get("run", ""))
    ]
    assert len(runs) == 1, f"{job_id} must install the project in exactly one step"
    return runs[0]


def check_union_step(wf: Workflow) -> None:
    """The one place the union script runs: its job, its flags, and that nothing hides a failure."""
    steps = _union_steps(wf)
    assert len(steps) == 1, f"{UNION_SCRIPT} must run in exactly one step, found {len(steps)}"
    job_id, union = steps[0]
    assert job_id == UNION_JOB, f"the shard union check runs in {job_id!r}, expected {UNION_JOB!r}"
    assert "if" not in union, "the shard union check must not be conditional"
    assert not union.get("continue-on-error"), "the shard union check must not be allowed to fail"
    _assert_default_shell(wf, UNION_JOB, union)
    job = _job(wf, UNION_JOB)
    assert "continue-on-error" not in job, f"{UNION_JOB} must not continue on error"
    assert "if" not in job, f"{UNION_JOB} must not be conditional: ci-ok requires it on every run"
    command = str(union["run"])
    value = rf'("[^"]*"|\w+|{re.escape(UNION_SPLITS)})'
    assert re.fullmatch(rf"python {re.escape(UNION_SCRIPT)}( --[a-z-]+ {value})*\s*", command), (
        f"the union step must be only the script and its flags, not {command!r}"
    )
    assert f"--splits {UNION_SPLITS}" in command, (
        f"the union step must pass --splits {UNION_SPLITS}"
    )
    assert _flag(command, r'--marker\s+"([^"]*)"') == MARKER, f"the union marker must be {MARKER!r}"


def check_union_legs(wf: Workflow) -> None:
    """Each sharded job has a union leg under its own Python, with the same shard count."""
    assert _setup_python(wf, UNION_JOB) == "${{ matrix.python-version }}", (
        f"{UNION_JOB} must run each leg under the Python version of that leg"
    )
    installs = {job_id: _install_run(wf, job_id) for job_id in (*SHARDED_JOBS, UNION_JOB)}
    for job_id in SHARDED_JOBS:
        assert installs[UNION_JOB] == installs[job_id], (
            f"{UNION_JOB} must install what {job_id} installs: collection depends on it"
        )
    n_test = len(_groups(wf, "test"))
    assert ("3.10", n_test) in _union_legs(wf, "pull_request"), (
        f"no union check runs under 3.10 for the {n_test}-way split of test"
    )
    for event in EVENTS:
        legs, expected = sorted(_union_legs(wf, event)), sorted(_expected_legs(wf, event))
        assert legs == expected, (
            f"{UNION_JOB} legs on {event} are {legs}, the shard jobs need {expected}"
        )


def check_shard_runs(wf: Workflow) -> None:
    """Every sharded pytest run splits like the union check does, with the same flags."""
    [(_, union)] = _union_steps(wf)
    command = str(union["run"])
    algorithm = r"--splitting-algorithm[ =](\S+)"
    for job_id in SHARDED_JOBS:
        groups = _groups(wf, job_id)
        assert len(groups) >= 2, f"{job_id} must be split into at least two shards"
        runs = _split_runs(wf, job_id)
        assert len(runs) == 1, f"{job_id} must run the sharded pytest in exactly one step"
        splits = _flag(runs[0], r"--splits\s+(\d+)")
        assert splits is not None and groups == list(range(1, int(splits) + 1)), (
            f"{job_id} groups {groups} do not match its --splits {splits}"
        )
        assert _flag(runs[0], r"--group\s+(\$\{\{\s*matrix\.group\s*\}\})") is not None, (
            f"{job_id} must run --group ${{{{ matrix.group }}}}"
        )
        assert _flag(runs[0], r'-m\s+"([^"]*)"') == MARKER, (
            f"{job_id} and the union check differ in marker"
        )
        assert _flag(runs[0], algorithm) == _flag(command, algorithm), (
            f"{job_id} and the union check split with different algorithms"
        )
    stray = [j for j, job in _jobs(wf).items() if j not in SHARDED_JOBS and _split_runs(wf, j)]
    assert not stray, f"jobs that shard pytest without a union check: {sorted(stray)}"


def check_one_leg_steps(wf: Workflow) -> None:
    """The timing tests, the schema check and the proof matrix check run once per job."""
    for job_id in SHARDED_JOBS:
        steps = _job(wf, job_id)["steps"]
        timing = [s for s in steps if re.search(r"-m\s+timing\b", str(s.get("run", "")))]
        assert len(timing) == 1, (
            f"{job_id} must run the timing tests in one step, found {len(timing)}"
        )
        assert "-p no:xdist" in str(timing[0]["run"]), (
            f"{job_id} must run the timing tests serially"
        )
        once = [timing[0]]
        for script in ("scripts/validate_contract_schema.py", "scripts/verify_proof_matrix.py"):
            found = [s for s in steps if script in str(s.get("run", ""))]
            assert len(found) == 1, f"{job_id} must run {script} in one step, found {len(found)}"
            once.extend(found)
        for step in once:
            assert str(step.get("if", "")).strip() == ONE_LEG_IF, (
                f"{job_id} must run {step.get('name')!r} on one group only ({ONE_LEG_IF})"
            )


def check_union_wiring(wf: Workflow) -> None:
    check_union_step(wf)
    check_union_legs(wf)
    check_shard_runs(wf)
    check_one_leg_steps(wf)


def _render(command: str, splits: int) -> str:
    return command.replace(UNION_SPLITS, str(splits))


def check_union_failure_fails_the_step(wf: Workflow, stub_dir: Path) -> None:
    """Run the union step's command with a ``python`` that exits 0 or 1: the step must follow."""
    [(_, union)] = _union_steps(wf)
    stub = stub_dir / "python"
    for code in (0, 1):
        stub.write_text(f"#!/bin/sh\nexit {code}\n", encoding="utf-8")
        stub.chmod(0o755)
        done = subprocess.run(
            [_BASH or "bash", "-e", "-c", _render(str(union["run"]), 2)],
            env={"PATH": f"{stub_dir}:/usr/bin:/bin"},
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert (done.returncode == 0) == (code == 0), (
            f"the union step exits {done.returncode} when the script exits {code}"
        )


def _coverage_steps(wf: Workflow) -> list[dict[str, Any]]:
    return list(_job(wf, COVERAGE_JOB)["steps"])


def _coverage_install_step(wf: Workflow) -> dict[str, Any]:
    found = [s for s in _coverage_steps(wf) if "pip install" in str(s.get("run", ""))]
    assert len(found) == 1, f"{COVERAGE_JOB} must install in exactly one step, found {len(found)}"
    return found[0]


def check_coverage_job(wf: Workflow) -> None:
    """The job combines the shards' data under the floor and installs only coverage."""
    job = _job(wf, COVERAGE_JOB)
    assert SHARD_JOB in _needs(job), f"{COVERAGE_JOB} must wait for {SHARD_JOB}"
    assert "continue-on-error" not in job, f"{COVERAGE_JOB} must not continue on error"
    shard = _job(wf, SHARD_JOB)
    uploads = [s for s in shard["steps"] if str(s.get("uses", "")).startswith("actions/upload-")]
    assert [u["with"]["name"] for u in uploads] == [SHARD_ARTIFACT], (
        f"{SHARD_JOB} must upload one artifact per shard named {SHARD_ARTIFACT!r}"
    )
    steps = _coverage_steps(wf)
    downloads = [s for s in steps if str(s.get("uses", "")).startswith("actions/down")]
    assert any(
        s["with"].get("pattern") == SHARD_ARTIFACT_PATTERN
        and s["with"].get("merge-multiple") is True
        for s in downloads
    ), f"{COVERAGE_JOB} must download the artifacts of every shard ({SHARD_ARTIFACT_PATTERN})"
    assert _setup_python(wf, COVERAGE_JOB) == _setup_python(wf, SHARD_JOB), (
        f"{COVERAGE_JOB} must read the sources with the Python the shards measured under"
    )
    combine = [s for s in steps if "coverage combine" in str(s.get("run", ""))]
    assert len(combine) == 1, f"{COVERAGE_JOB} must combine the shards in exactly one step"
    assert "continue-on-error" not in combine[0], "the combine step must not continue on error"
    assert "if" not in combine[0], "the combine step must not be conditional"
    _assert_default_shell(wf, COVERAGE_JOB, combine[0])
    lines = [line.strip() for line in str(combine[0]["run"]).splitlines() if line.strip()]
    assert lines == [
        "coverage combine .coverage.shard.*",
        f"coverage report --fail-under={COVERAGE_FLOOR}",
        "coverage xml -o coverage.xml",
    ], f"the combine step must be exactly combine, the {COVERAGE_FLOOR}% floor and xml: {lines}"
    install = _coverage_install_step(wf)
    assert str(install["run"]).count("pip install") == 1, "the install step must install once"
    assert re.search(r'^pip install "coverage==\$version"$', str(install["run"]), re.MULTILINE), (
        'the install step must be `pip install "coverage==$version"`'
    )
    assert ".coverage.shard.*" in str(install["run"]) and "from meta" in str(install["run"]), (
        "the coverage version must be read from the shard data files"
    )
    assert steps.index(install) > steps.index(downloads[0]), (
        "the coverage install must come after the download of the shard data it reads"
    )


def _write_shard_data(path: Path, version: str | None = None) -> None:
    """A coverage data file as coverage writes it; ``version`` rewrites the version it records."""
    import coverage

    data = coverage.CoverageData(basename=str(path))
    data.add_lines({"/source/module.py": [1, 2]})
    data.close()
    if version is not None:
        import sqlite3

        db = sqlite3.connect(path)
        db.execute("update meta set value = ? where key = 'version'", (version,))
        db.commit()
        db.close()


def _run_coverage_install(
    wf: Workflow, work: Path, shards: dict[str, str | None]
) -> tuple[int, str]:
    """Run the install step in a new ``work`` dir with a recording ``pip``.

    ``shards`` maps a data file name to the version it records (``None``: as coverage writes
    it). Returns the step's exit code and what ``pip`` was asked to do.
    """
    stubs = work / "bin"
    stubs.mkdir(parents=True)
    for name, version in shards.items():
        _write_shard_data(work / name, version)
    log = work / "pip.log"
    (stubs / "pip").write_text(f'#!/bin/sh\necho "$*" >> "{log}"\n', encoding="utf-8")
    (stubs / "python").write_text(
        f'#!/bin/sh\nif [ "$1" = "-m" ] && [ "$2" = "pip" ]; then shift 2; echo "$*" >> "{log}"; '
        f'exit 0; fi\nexec "{sys.executable}" "$@"\n',
        encoding="utf-8",
    )
    for stub in stubs.iterdir():
        stub.chmod(0o755)
    done = subprocess.run(
        [_BASH or "bash", "-e", "-c", str(_coverage_install_step(wf)["run"])],
        cwd=work,
        env={"PATH": f"{stubs}:/usr/bin:/bin"},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    return done.returncode, log.read_text(encoding="utf-8") if log.exists() else ""


def check_coverage_install_follows_the_shards(wf: Workflow, work: Path) -> None:
    """The step installs the coverage version that wrote the shard data, and refuses a mix."""
    import coverage

    assert _HAS_BASH, "bash is needed to run the coverage install step"
    both = {".coverage.shard.1": None, ".coverage.shard.2": None}
    code, log = _run_coverage_install(wf, work / "same", both)
    assert code == 0, f"the install step failed on matching shard data: exit {code}"
    assert log.split() == ["install", f"coverage=={coverage.__version__}"], (
        f"the install step must install coverage=={coverage.__version__}, pip got {log!r}"
    )
    mixed = {".coverage.shard.1": None, ".coverage.shard.2": "0.0.1"}
    code, log = _run_coverage_install(wf, work / "mixed", mixed)
    assert code != 0 and log == "", (
        f"shards written by two versions must fail: exit {code}, {log!r}"
    )
    code, log = _run_coverage_install(wf, work / "none", {})
    assert code != 0 and log == "", f"no shard data must fail: exit {code}, {log!r}"


def check_build_needs_only_changes(wf: Workflow) -> None:
    build = _job(wf, "build")
    assert _needs(build) == ["changes"], f"build needs {_needs(build)}, expected ['changes']"
    assert str(build.get("if", "")).strip() == BUILD_IF, f"build if changed: {build.get('if')!r}"
    assert "code" in _job(wf, "changes").get("outputs", {}), "changes no longer outputs code"
    assert "build" in _needs(_job(wf, "ci-ok")), "ci-ok no longer waits for build"


# ---------------------------------------------------------------------------
# The real workflow satisfies every pin
# ---------------------------------------------------------------------------


def test_ci_ok_needs_exactly_the_recorded_jobs() -> None:
    check_ci_ok_needs(_workflow())


def test_no_job_is_left_outside_ci_ok() -> None:
    check_every_job_is_gated(_workflow())


def test_ci_ok_treats_the_recorded_jobs_as_required_and_as_skippable() -> None:
    check_required_and_skippable(_workflow())


def test_the_gate_step_cannot_be_bypassed() -> None:
    check_gate_cannot_be_bypassed(_workflow())


@pytest.mark.skipif(not _HAS_BASH, reason="bash is needed to run the gate script")
@pytest.mark.parametrize("result", ["success", "skipped", "failure", "cancelled"])
@pytest.mark.parametrize("name", sorted(CI_OK_NEEDS))
def test_gate_script_outcome_for_each_job_and_result(name: str, result: str) -> None:
    check_gate_outcome(_workflow(), name, result)


def test_shard_union_check_runs_in_its_job_on_every_shard() -> None:
    check_union_wiring(_workflow())


@pytest.mark.skipif(not _HAS_BASH, reason="bash is needed to run the union step")
def test_a_failing_shard_union_check_fails_its_step(tmp_path: Path) -> None:
    check_union_failure_fails_the_step(_workflow(), tmp_path)


def test_coverage_job_combines_under_the_floor_and_installs_only_coverage() -> None:
    check_coverage_job(_workflow())


def test_the_coverage_install_installs_the_version_that_wrote_the_shards(tmp_path: Path) -> None:
    check_coverage_install_follows_the_shards(_workflow(), tmp_path)


def test_build_needs_only_changes_keeps_its_condition_and_ci_ok_waits_for_it() -> None:
    check_build_needs_only_changes(_workflow())


# ---------------------------------------------------------------------------
# Each pin fails when the thing it pins is changed
# ---------------------------------------------------------------------------


def _mutated() -> Workflow:
    return copy.deepcopy(_workflow())


def _gate_script(wf: Workflow) -> str:
    return str(_gate_step(wf)["run"])


def _set_gate_script(wf: Workflow, script: str) -> None:
    _gate_step(wf)["run"] = script


def _move_entry(script: str, name: str, *, to_required: bool) -> str:
    """Move the ``"<name>:$VAR" \\`` line of a (non-final) entry to the other gate loop."""
    line = re.search(rf'^[ \t]*"{re.escape(name)}:\$[A-Z_]+" \\\n', script, re.MULTILINE)
    assert line is not None, f"{name} is not a non-final entry of a gate loop"
    without = script.replace(line.group(0), "", 1)
    anchor = (
        '"test:$RESULT_TEST" \\\n' if to_required else '"install-smoke:$RESULT_INSTALL_SMOKE" \\\n'
    )
    at = without.index(anchor)
    start = without.rindex("\n", 0, at) + 1
    return without[:start] + line.group(0) + without[start:]


@pytest.mark.parametrize("name", sorted(CI_OK_NEEDS))
def test_dropping_a_job_from_ci_ok_needs_fails_the_pin(name: str) -> None:
    wf = _mutated()
    _job(wf, "ci-ok")["needs"] = [n for n in _needs(_job(wf, "ci-ok")) if n != name]
    with pytest.raises(AssertionError, match=rf"ci-ok needs changed: missing \['{name}'\]"):
        check_ci_ok_needs(wf)


def test_adding_a_job_to_ci_ok_needs_fails_the_pin() -> None:
    wf = _mutated()
    _job(wf, "ci-ok")["needs"] = [*_needs(_job(wf, "ci-ok")), "extra-check"]
    with pytest.raises(AssertionError, match=r"extra \['extra-check'\]"):
        check_ci_ok_needs(wf)


def test_ci_ok_without_always_fails_the_pin() -> None:
    wf = _mutated()
    del _job(wf, "ci-ok")["if"]
    with pytest.raises(AssertionError, match="if: always"):
        check_ci_ok_needs(wf)


def test_a_new_job_that_ci_ok_does_not_wait_for_fails_the_pin() -> None:
    wf = _mutated()
    _jobs(wf)["extra-check"] = {"runs-on": "ubuntu-latest", "steps": []}
    with pytest.raises(AssertionError, match=r"no required check waits for: \['extra-check'\]"):
        check_every_job_is_gated(wf)


def test_moving_a_required_job_to_skippable_fails_the_pin() -> None:
    wf = _mutated()
    _set_gate_script(wf, _move_entry(_gate_script(wf), "lint", to_required=False))
    with pytest.raises(AssertionError, match="required jobs changed"):
        check_required_and_skippable(wf)


def test_moving_a_skippable_job_to_required_fails_the_pin() -> None:
    wf = _mutated()
    _set_gate_script(wf, _move_entry(_gate_script(wf), "build", to_required=True))
    with pytest.raises(AssertionError, match="required jobs changed"):
        check_required_and_skippable(wf)


@pytest.mark.parametrize(
    ("where", "key", "value"),
    [
        ("step", "continue-on-error", True),
        ("step", "continue-on-error", "${{ always() }}"),
        ("step", "if", "false"),
        ("job", "continue-on-error", True),
    ],
)
def test_a_setting_that_hides_a_gate_failure_fails_the_pin(
    where: str, key: str, value: Any
) -> None:
    wf = _mutated()
    target = _gate_step(wf) if where == "step" else _job(wf, "ci-ok")
    target[key] = value
    with pytest.raises(AssertionError, match=rf"must not set '?{re.escape(key)}'?"):
        check_gate_cannot_be_bypassed(wf)


def test_an_extra_ci_ok_step_fails_the_pin() -> None:
    wf = _mutated()
    _job(wf, "ci-ok")["steps"].append({"run": "true"})
    with pytest.raises(AssertionError, match="only its 'Evaluate gate' step"):
        check_gate_cannot_be_bypassed(wf)


@pytest.mark.parametrize("level", ["step", "job", "workflow"])
def test_a_shell_override_that_hides_a_gate_failure_fails_the_pin(level: str) -> None:
    wf = _mutated()
    shell = "bash -c true {0}"
    if level == "step":
        _gate_step(wf)["shell"] = shell
    elif level == "job":
        _job(wf, "ci-ok")["defaults"] = {"run": {"shell": shell}}
    else:
        wf["defaults"] = {"run": {"shell": shell}}
    with pytest.raises(AssertionError, match="must run its step under the default shell"):
        check_gate_cannot_be_bypassed(wf)


def test_a_result_variable_wired_to_the_wrong_job_fails_the_pin() -> None:
    wf = _mutated()
    _gate_step(wf)["env"]["RESULT_LINT"] = "${{ needs.test.result }}"
    with pytest.raises(AssertionError, match="two env vars carry the result of test"):
        check_required_and_skippable(wf)


@pytest.mark.skipif(not _HAS_BASH, reason="bash is needed to run the gate script")
def test_a_gate_that_prints_fail_but_does_not_fail_fails_the_outcome_pin() -> None:
    wf = _mutated()
    script = _gate_script(wf)
    marker = '(required; must be success)"; ok=false'
    assert marker in script
    _set_gate_script(wf, script.replace(marker, '(required; must be success)"; ok=true'))
    check_gate_outcome(wf, "lint", "success")
    with pytest.raises(AssertionError, match=r"gate exit code 0 for lint=skipped"):
        check_gate_outcome(wf, "lint", "skipped")


@pytest.mark.skipif(not _HAS_BASH, reason="bash is needed to run the gate script")
def test_a_gate_that_lets_a_skippable_job_fail_fails_the_outcome_pin() -> None:
    wf = _mutated()
    script = _gate_script(wf)
    marker = '(must not fail/cancel; skip OK)"; ok=false'
    assert marker in script
    _set_gate_script(wf, script.replace(marker, '(must not fail/cancel; skip OK)"; ok=true'))
    with pytest.raises(AssertionError, match=r"gate exit code 0 for build=failure"):
        check_gate_outcome(wf, "build", "failure")


def _union_step(wf: Workflow, job_id: str = UNION_JOB) -> dict[str, Any]:
    [step] = [s for s in _job(wf, job_id)["steps"] if UNION_SCRIPT in str(s.get("run", ""))]
    return step


def _run_step(wf: Workflow, job_id: str) -> dict[str, Any]:
    [step] = [s for s in _job(wf, job_id)["steps"] if "--splits" in str(s.get("run", ""))]
    return step


def _ternary(pull_request: Any, other: Any) -> str:
    def literal(value: Any) -> str:
        return "fromJson('" + json.dumps(value, separators=(",", ":")) + "')"

    return (
        "${{ (github.event_name == 'pull_request') && "
        f"{literal(pull_request)} || {literal(other)} }}}}"
    )


def _set_union_legs(wf: Workflow, event: str, legs: list[tuple[str, int]]) -> None:
    """Rewrite the union job's legs for one event, leaving the other event's legs as they are."""
    both = {e: [(p, n) for p, n in _union_legs(wf, e)] for e in EVENTS}
    both[event] = legs
    as_include = {e: [{"python-version": p, "splits": n} for p, n in both[e]] for e in EVENTS}
    _matrix(wf, UNION_JOB)["include"] = _ternary(as_include["pull_request"], as_include["other"])


def _set_python_versions(wf: Workflow, job_id: str, pull_request: Any, other: Any) -> None:
    _matrix(wf, job_id)["python-version"] = _ternary(pull_request, other)


def test_removing_the_union_step_fails_the_pin() -> None:
    wf = _mutated()
    steps = _job(wf, UNION_JOB)["steps"]
    _job(wf, UNION_JOB)["steps"] = [s for s in steps if UNION_SCRIPT not in str(s.get("run", ""))]
    with pytest.raises(AssertionError, match="exactly one step, found 0"):
        check_union_wiring(wf)


@pytest.mark.parametrize("target", ["type-check", "coverage"])
def test_moving_the_union_step_to_another_job_fails_the_pin(target: str) -> None:
    wf = _mutated()
    steps = _job(wf, UNION_JOB)["steps"]
    moved = [s for s in steps if UNION_SCRIPT in str(s.get("run", ""))]
    _job(wf, UNION_JOB)["steps"] = [s for s in steps if s not in moved]
    _job(wf, target)["steps"].extend(moved)
    with pytest.raises(AssertionError, match=f"runs in '{target}'"):
        check_union_wiring(wf)


@pytest.mark.parametrize("key", ["if", "continue-on-error"])
def test_making_the_union_step_conditional_or_optional_fails_the_pin(key: str) -> None:
    wf = _mutated()
    _union_step(wf)[key] = "false" if key == "if" else True
    with pytest.raises(AssertionError, match="must not be"):
        check_union_wiring(wf)


def test_a_conditional_union_job_fails_the_pin() -> None:
    wf = _mutated()
    _job(wf, UNION_JOB)["if"] = "needs.changes.outputs.code == 'true'"
    with pytest.raises(AssertionError, match=f"{UNION_JOB} must not be conditional"):
        check_union_wiring(wf)


@pytest.mark.parametrize(
    "suffix", [" || true", " ; true", " && true", " | cat", "\ntrue", " --splits 4 || exit 0"]
)
def test_a_union_command_that_can_hide_its_failure_fails_the_pin(suffix: str) -> None:
    wf = _mutated()
    union = _union_step(wf)
    union["run"] = union["run"].rstrip() + suffix
    with pytest.raises(AssertionError, match="only the script and its flags"):
        check_union_wiring(wf)


@pytest.mark.parametrize("where", ["step", "job", "workflow"])
def test_a_shell_override_on_the_union_step_fails_the_pin(where: str) -> None:
    wf = _mutated()
    union = _union_step(wf)
    if where == "step":
        union["shell"] = "bash {0} || true"
    elif where == "job":
        _job(wf, UNION_JOB)["defaults"] = {"run": {"shell": "bash {0} || true"}}
    else:
        wf["defaults"] = {"run": {"shell": "bash {0} || true"}}
    with pytest.raises(AssertionError, match="must run its step under the default shell"):
        check_union_wiring(wf)


def test_a_union_job_that_continues_on_error_fails_the_pin() -> None:
    wf = _mutated()
    _job(wf, UNION_JOB)["continue-on-error"] = True
    with pytest.raises(AssertionError, match="must not continue on error"):
        check_union_wiring(wf)


@pytest.mark.skipif(not _HAS_BASH, reason="bash is needed to run the union step")
def test_a_union_step_that_swallows_the_exit_code_fails_the_outcome_pin(tmp_path: Path) -> None:
    wf = _mutated()
    union = _union_step(wf)
    check_union_failure_fails_the_step(wf, tmp_path)
    union["run"] += " || true"
    with pytest.raises(AssertionError, match="the union step exits 0 when the script exits 1"):
        check_union_failure_fails_the_step(wf, tmp_path)


def test_a_union_step_with_its_own_splits_fails_the_pin() -> None:
    wf = _mutated()
    union = _union_step(wf)
    union["run"] = union["run"].replace(f"--splits {UNION_SPLITS}", "--splits 3")
    with pytest.raises(AssertionError, match="must pass --splits"):
        check_union_wiring(wf)


def test_a_union_marker_that_is_not_the_shards_marker_fails_the_pin() -> None:
    wf = _mutated()
    union = _union_step(wf)
    union["run"] = union["run"].replace(f'"{MARKER}"', '"not slow"')
    with pytest.raises(AssertionError, match="union marker must be"):
        check_union_wiring(wf)


@pytest.mark.parametrize("job_id", SHARDED_JOBS)
def test_a_shard_added_to_or_missing_from_the_matrix_fails_the_pin(job_id: str) -> None:
    for groups in ([1], [1, 2, 3, 4, 5, 6, 7]):
        wf = _mutated()
        _matrix(wf, job_id)["group"] = groups
        with pytest.raises(
            AssertionError,
            match=r"legs on .* need|no union check runs under 3\.10|at least two shards",
        ):
            check_union_wiring(wf)


@pytest.mark.parametrize("job_id", SHARDED_JOBS)
def test_shards_that_split_differently_from_their_matrix_fail_the_pin(job_id: str) -> None:
    wf = _mutated()
    run = _run_step(wf, job_id)
    run["run"] = re.sub(r"--splits (\d+)", lambda m: f"--splits {int(m.group(1)) + 1}", run["run"])
    with pytest.raises(AssertionError, match=r"groups \[.*\] do not match its --splits"):
        check_union_wiring(wf)


@pytest.mark.parametrize("job_id", SHARDED_JOBS)
def test_a_group_flag_that_is_not_the_matrix_group_fails_the_pin(job_id: str) -> None:
    wf = _mutated()
    run = _run_step(wf, job_id)
    run["run"] = run["run"].replace("--group ${{ matrix.group }}", "--group 1")
    with pytest.raises(AssertionError, match="must run --group"):
        check_union_wiring(wf)


@pytest.mark.parametrize("job_id", SHARDED_JOBS)
def test_a_different_marker_in_the_shards_fails_the_pin(job_id: str) -> None:
    wf = _mutated()
    run = _run_step(wf, job_id)
    run["run"] = run["run"].replace(f'-m "{MARKER}"', '-m "not slow"')
    with pytest.raises(AssertionError, match="differ in marker"):
        check_union_wiring(wf)


@pytest.mark.parametrize("job_id", SHARDED_JOBS)
def test_a_split_algorithm_only_the_shards_use_fails_the_pin(job_id: str) -> None:
    wf = _mutated()
    run = _run_step(wf, job_id)
    run["run"] = re.sub(
        r"(--splits \d+)", r"\1 --splitting-algorithm least_duration", run["run"], count=1
    )
    with pytest.raises(AssertionError, match="different algorithms"):
        check_union_wiring(wf)


@pytest.mark.parametrize("job_id", SHARDED_JOBS)
def test_a_split_algorithm_only_the_union_check_uses_fails_the_pin(job_id: str) -> None:
    wf = _mutated()
    union = _union_step(wf)
    union["run"] = union["run"].rstrip() + " --splitting-algorithm least_duration"
    with pytest.raises(AssertionError, match="different algorithms"):
        check_union_wiring(wf)


def test_a_job_that_shards_pytest_without_a_union_check_fails_the_pin() -> None:
    wf = _mutated()
    _jobs(wf)["extra-shards"] = {
        "runs-on": "ubuntu-latest",
        "steps": [{"run": 'pytest tests/ -m "not timing" --splits 2 --group 1'}],
    }
    with pytest.raises(
        AssertionError, match=r"shard pytest without a union check: \['extra-shards'\]"
    ):
        check_union_wiring(wf)


def test_a_test_split_with_one_shard_fails_the_pin() -> None:
    wf = _mutated()
    _matrix(wf, "test")["group"] = [1]
    _run_step(wf, "test")["run"] = _run_step(wf, "test")["run"].replace("--splits 2", "--splits 1")
    _set_union_legs(wf, "pull_request", [("3.13", 4), ("3.10", 1)])
    _set_union_legs(wf, "other", [("3.13", 4), *[(v, 1) for v in ("3.10", "3.11", "3.12")]])
    with pytest.raises(AssertionError, match="at least two shards"):
        check_union_wiring(wf)


def test_a_missing_3_10_union_leg_fails_the_pin() -> None:
    wf = _mutated()
    legs = [leg for leg in _union_legs(wf, "pull_request") if leg[0] != "3.10"]
    _set_union_legs(wf, "pull_request", legs)
    with pytest.raises(AssertionError, match=r"no union check runs under 3\.10"):
        check_union_wiring(wf)


def test_a_missing_3_13_union_leg_fails_the_pin() -> None:
    wf = _mutated()
    legs = [leg for leg in _union_legs(wf, "pull_request") if leg[0] != "3.13"]
    _set_union_legs(wf, "pull_request", legs)
    with pytest.raises(AssertionError, match=r"legs on pull_request are .*the shard jobs need"):
        check_union_wiring(wf)


def test_a_leg_missing_only_on_pushes_fails_the_pin() -> None:
    wf = _mutated()
    legs = [leg for leg in _union_legs(wf, "other") if leg[0] != "3.12"]
    _set_union_legs(wf, "other", legs)
    with pytest.raises(AssertionError, match=r"legs on other are .*the shard jobs need"):
        check_union_wiring(wf)


@pytest.mark.parametrize("python", ["3.13", "3.10"])
@pytest.mark.parametrize("event", EVENTS)
def test_a_leg_with_the_wrong_split_count_fails_the_pin(event: str, python: str) -> None:
    wf = _mutated()
    legs = [(p, n + 1 if p == python else n) for p, n in _union_legs(wf, event)]
    _set_union_legs(wf, event, legs)
    # A wrong 3.10 leg on a pull request is caught by the test-split pin before the full compare.
    with pytest.raises(
        AssertionError, match=rf"legs on {event} are|no union check runs under 3\.10"
    ):
        check_union_wiring(wf)


def test_a_union_leg_for_a_python_no_job_runs_fails_the_pin() -> None:
    wf = _mutated()
    _set_union_legs(wf, "other", [*_union_legs(wf, "other"), ("3.9", 2)])
    with pytest.raises(AssertionError, match=r"legs on other are .*3\.9"):
        check_union_wiring(wf)


def test_a_python_added_to_the_test_matrix_without_a_union_leg_fails_the_pin() -> None:
    wf = _mutated()
    both = _by_event(_matrix(wf, "test")["python-version"])
    _set_python_versions(wf, "test", both["pull_request"], [*both["other"], "3.14"])
    with pytest.raises(AssertionError, match=r"legs on other are .*the shard jobs need .*3\.14"):
        check_union_wiring(wf)


def test_union_matrix_keys_other_than_include_fail_the_pin() -> None:
    wf = _mutated()
    _matrix(wf, UNION_JOB)["exclude"] = [{"python-version": "3.10"}]
    with pytest.raises(AssertionError, match="include list only"):
        check_union_wiring(wf)


def test_a_union_leg_with_extra_keys_fails_the_pin() -> None:
    wf = _mutated()
    include = _by_event(_matrix(wf, UNION_JOB)["include"])
    legs = [{**leg, "marker": "not slow"} for leg in include["pull_request"]]
    _matrix(wf, UNION_JOB)["include"] = _ternary(legs, include["other"])
    with pytest.raises(AssertionError, match="exactly python-version and splits"):
        check_union_wiring(wf)


def test_a_union_job_that_ignores_the_python_of_its_leg_fails_the_pin() -> None:
    wf = _mutated()
    for step in _job(wf, UNION_JOB)["steps"]:
        if str(step.get("uses", "")).startswith("actions/setup-python"):
            step["with"]["python-version"] = "3.13"
    with pytest.raises(AssertionError, match="under the Python version of that leg"):
        check_union_wiring(wf)


def test_a_union_job_that_installs_something_else_fails_the_pin() -> None:
    wf = _mutated()
    for step in _job(wf, UNION_JOB)["steps"]:
        if "pip install -e" in str(step.get("run", "")):
            step["run"] = step["run"].replace(",dashboard", "")
    with pytest.raises(AssertionError, match=f"{UNION_JOB} must install what"):
        check_union_wiring(wf)


@pytest.mark.parametrize("job_id", SHARDED_JOBS)
def test_timing_tests_that_run_on_every_group_fail_the_pin(job_id: str) -> None:
    wf = _mutated()
    [step] = [s for s in _job(wf, job_id)["steps"] if "-m timing" in str(s.get("run", ""))]
    del step["if"]
    with pytest.raises(AssertionError, match="on one group only"):
        check_union_wiring(wf)


@pytest.mark.parametrize("job_id", SHARDED_JOBS)
def test_timing_tests_that_are_dropped_or_run_twice_fail_the_pin(job_id: str) -> None:
    wf = _mutated()
    steps = _job(wf, job_id)["steps"]
    timing = [s for s in steps if "-m timing" in str(s.get("run", ""))]
    _job(wf, job_id)["steps"] = [s for s in steps if s not in timing]
    with pytest.raises(AssertionError, match="in one step, found 0"):
        check_union_wiring(wf)
    _job(wf, job_id)["steps"] = [*steps, copy.deepcopy(timing[0])]
    with pytest.raises(AssertionError, match="in one step, found 2"):
        check_union_wiring(wf)


@pytest.mark.parametrize("job_id", SHARDED_JOBS)
def test_timing_tests_that_run_in_parallel_fail_the_pin(job_id: str) -> None:
    wf = _mutated()
    [step] = [s for s in _job(wf, job_id)["steps"] if "-m timing" in str(s.get("run", ""))]
    step["run"] = step["run"].replace(" -p no:xdist", "")
    with pytest.raises(AssertionError, match="timing tests serially"):
        check_union_wiring(wf)


@pytest.mark.parametrize("script", ["validate_contract_schema.py", "verify_proof_matrix.py"])
@pytest.mark.parametrize("job_id", SHARDED_JOBS)
def test_a_check_that_runs_on_every_group_or_not_at_all_fails_the_pin(
    job_id: str, script: str
) -> None:
    wf = _mutated()
    steps = _job(wf, job_id)["steps"]
    [step] = [s for s in steps if script in str(s.get("run", ""))]
    del step["if"]
    with pytest.raises(AssertionError, match="on one group only"):
        check_union_wiring(wf)
    _job(wf, job_id)["steps"] = [s for s in steps if s is not step]
    with pytest.raises(AssertionError, match=rf"{script} in one step, found 0"):
        check_union_wiring(wf)


def test_a_union_job_that_stops_being_required_by_ci_ok_fails_the_pin() -> None:
    wf = _mutated()
    _job(wf, "ci-ok")["needs"] = [n for n in _needs(_job(wf, "ci-ok")) if n != UNION_JOB]
    with pytest.raises(AssertionError, match=rf"missing \['{UNION_JOB}'\]"):
        check_ci_ok_needs(wf)


@pytest.mark.parametrize("name", ["lint", "changes", UNION_JOB])
def test_moving_a_required_job_to_skippable_fails_the_pin_for_each_required_job(name: str) -> None:
    wf = _mutated()
    _set_gate_script(wf, _move_entry(_gate_script(wf), name, to_required=False))
    with pytest.raises(AssertionError, match="required jobs changed"):
        check_required_and_skippable(wf)


def test_a_coverage_job_that_waits_for_nothing_fails_the_pin() -> None:
    wf = _mutated()
    _job(wf, COVERAGE_JOB)["needs"] = []
    with pytest.raises(AssertionError, match=f"{COVERAGE_JOB} must wait for {SHARD_JOB}"):
        check_coverage_job(wf)


def test_a_coverage_job_that_stops_downloading_every_shard_fails_the_pin() -> None:
    wf = _mutated()
    for step in _job(wf, COVERAGE_JOB)["steps"]:
        if str(step.get("uses", "")).startswith("actions/download-artifact"):
            step["with"]["pattern"] = "coverage-shard-1"
    with pytest.raises(AssertionError, match="artifacts of every shard"):
        check_coverage_job(wf)


def _coverage_step(wf: Workflow, text: str) -> dict[str, Any]:
    [step] = [s for s in _coverage_steps(wf) if text in str(s.get("run", ""))]
    return step


@pytest.mark.parametrize(
    "replacement",
    [
        "coverage report --fail-under=60",
        "coverage report",
        "coverage report --fail-under=70 || true",
    ],
)
def test_a_lowered_or_hidden_coverage_floor_fails_the_pin(replacement: str) -> None:
    wf = _mutated()
    step = _coverage_step(wf, "coverage combine")
    step["run"] = step["run"].replace(f"coverage report --fail-under={COVERAGE_FLOOR}", replacement)
    with pytest.raises(AssertionError, match="must be exactly combine"):
        check_coverage_job(wf)


def test_a_coverage_combine_of_other_files_fails_the_pin() -> None:
    wf = _mutated()
    step = _coverage_step(wf, "coverage combine")
    step["run"] = step["run"].replace(".coverage.shard.*", ".coverage.shard.1")
    with pytest.raises(AssertionError, match="must be exactly combine"):
        check_coverage_job(wf)


def test_a_coverage_job_under_another_python_fails_the_pin() -> None:
    wf = _mutated()
    for step in _job(wf, COVERAGE_JOB)["steps"]:
        if str(step.get("uses", "")).startswith("actions/setup-python"):
            step["with"]["python-version"] = "3.12"
    with pytest.raises(AssertionError, match="Python the shards measured under"):
        check_coverage_job(wf)


@pytest.mark.parametrize(
    "run",
    [
        'pip install -e ".[dev,server,dashboard]"',
        "pip install coverage",
        'pip install "coverage==7.0.0"',
        'pip install "coverage==$version" ".[dev]"',
    ],
)
def test_a_coverage_install_that_is_not_the_shards_version_fails_the_pin(run: str) -> None:
    wf = _mutated()
    _coverage_install_step(wf)["run"] = run
    with pytest.raises(AssertionError, match="the install step must"):
        check_coverage_job(wf)


def test_a_second_coverage_install_fails_the_pin() -> None:
    wf = _mutated()
    step = _coverage_install_step(wf)
    step["run"] += '\npip install -e ".[dev,server,dashboard]"\n'
    with pytest.raises(AssertionError, match="install once"):
        check_coverage_job(wf)


def test_an_install_that_does_not_read_the_shard_data_fails_the_pin() -> None:
    wf = _mutated()
    _coverage_install_step(wf)["run"] = 'version=7.15.2\npip install "coverage==$version"\n'
    with pytest.raises(AssertionError, match="read from the shard data files"):
        check_coverage_job(wf)


def test_a_coverage_install_before_the_download_fails_the_pin() -> None:
    wf = _mutated()
    steps = _job(wf, COVERAGE_JOB)["steps"]
    install = _coverage_install_step(wf)
    steps.remove(install)
    steps.insert(1, install)
    with pytest.raises(AssertionError, match="must come after the download"):
        check_coverage_job(wf)


def test_a_union_check_left_in_the_coverage_job_fails_the_pin() -> None:
    wf = _mutated()
    union = copy.deepcopy(_union_step(wf))
    _job(wf, COVERAGE_JOB)["steps"].append(union)
    with pytest.raises(AssertionError, match="exactly one step, found 2"):
        check_union_wiring(wf)


def test_an_install_with_a_fixed_version_fails_the_behaviour_pin(tmp_path: Path) -> None:
    import coverage

    wf = _mutated()
    step = _coverage_install_step(wf)
    # Static checks still see the glob, the query and the pip line; only running it can tell.
    step["run"] = '# .coverage.shard.* from meta\nversion=0.0.1\npip install "coverage==$version"\n'
    check_coverage_job(wf)
    with pytest.raises(
        AssertionError, match=rf"must install coverage=={re.escape(coverage.__version__)}"
    ):
        check_coverage_install_follows_the_shards(wf, tmp_path)


def test_an_install_that_accepts_shards_from_two_versions_fails_the_behaviour_pin(
    tmp_path: Path,
) -> None:
    wf = _mutated()
    step = _coverage_install_step(wf)
    assert "len(versions) != 1" in step["run"]
    step["run"] = step["run"].replace("len(versions) != 1", "len(versions) < 1")
    with pytest.raises(AssertionError, match="shards written by two versions must fail"):
        check_coverage_install_follows_the_shards(wf, tmp_path)


def test_an_install_that_ignores_missing_shard_data_fails_the_behaviour_pin(tmp_path: Path) -> None:
    wf = _mutated()
    step = _coverage_install_step(wf)
    assert "len(versions) != 1 or None in versions" in step["run"]
    assert "print(versions.pop())" in step["run"]
    step["run"] = step["run"].replace("len(versions) != 1 or None in versions", "len(versions) > 1")
    step["run"] = step["run"].replace(
        "print(versions.pop())", 'print(versions.pop() if versions else "7.0")'
    )
    with pytest.raises(AssertionError, match="no shard data must fail"):
        check_coverage_install_follows_the_shards(wf, tmp_path)


@pytest.mark.parametrize(
    "needs", [["changes", "lint"], ["lint"], [], "lint", ["changes", "test-shard", "coverage"]]
)
def test_build_that_waits_for_anything_but_changes_fails_the_pin(needs: Any) -> None:
    wf = _mutated()
    _job(wf, "build")["needs"] = needs
    with pytest.raises(AssertionError, match=r"build needs .*expected \['changes'\]"):
        check_build_needs_only_changes(wf)


def test_build_that_loses_its_condition_fails_the_pin() -> None:
    wf = _mutated()
    del _job(wf, "build")["if"]
    with pytest.raises(AssertionError, match="build if changed"):
        check_build_needs_only_changes(wf)


def test_ci_ok_that_stops_waiting_for_build_fails_the_pin() -> None:
    wf = _mutated()
    _job(wf, "ci-ok")["needs"] = [n for n in _needs(_job(wf, "ci-ok")) if n != "build"]
    with pytest.raises(AssertionError, match="ci-ok no longer waits for build"):
        check_build_needs_only_changes(wf)
