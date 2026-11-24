"""Tests that pin what ``ci-ok`` requires and how the shard union check is wired (issue #1332).

They read ``.github/workflows/ci.yml`` as a file. ``ci-ok`` is the single required check,
so a job quietly dropped from its ``needs`` or moved from "required" to "may skip" would
let a red job merge. Each pin below is a ``check_*`` function that raises
``AssertionError``. The tests run it on the real workflow (it must pass) and on a mutated
copy (it must fail, for the stated reason), so a pin that cannot fail is caught here.
"""

from __future__ import annotations

import copy
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"

# What ci-ok treated as required and as skippable on origin/main (af3c909c). The `changes`
# job is not in its needs: it only decides whether the code-only jobs run.
REQUIRED = ("test", "test-shard", "coverage", "lint", "security", "type-check")
SKIPPABLE = ("install-smoke", "build", "typescript-contract-parity", "client-package-smoke")
CI_OK_NEEDS = frozenset(REQUIRED + SKIPPABLE)
NOT_GATED = frozenset({"changes", "ci-ok"})

BUILD_IF = "needs.changes.outputs.code == 'true'"
UNION_JOB = "coverage"
UNION_SCRIPT = "scripts/ci_shard_union.py"
SHARD_JOB = "test-shard"
SHARD_ARTIFACT = "coverage-shard-${{ matrix.group }}"
SHARD_ARTIFACT_PATTERN = "coverage-shard-*"

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


def check_union_wiring(wf: Workflow) -> None:
    steps = _union_steps(wf)
    assert len(steps) == 1, f"{UNION_SCRIPT} must run in exactly one step, found {len(steps)}"
    job_id, union = steps[0]
    assert job_id == UNION_JOB, f"the shard union check runs in {job_id!r}, expected {UNION_JOB!r}"
    assert "if" not in union, "the shard union check must not be conditional"
    assert not union.get("continue-on-error"), "the shard union check must not be allowed to fail"
    _assert_default_shell(wf, UNION_JOB, union)
    assert "continue-on-error" not in _job(wf, UNION_JOB), f"{UNION_JOB} must not continue on error"
    command = str(union["run"])
    assert re.fullmatch(
        rf'python {re.escape(UNION_SCRIPT)}( --[a-z-]+ ("[^"]*"|\w+))*\s*', command
    ), f"the union step must be only the script and its flags, not {command!r}"
    splits = _flag(command, r"--splits\s+(\d+)")
    marker = _flag(command, r'--marker\s+"([^"]*)"')
    assert splits is not None and marker is not None, f"union command lacks flags: {command!r}"

    shard = _job(wf, SHARD_JOB)
    assert shard["strategy"]["matrix"]["group"] == list(range(1, int(splits) + 1)), (
        f"{SHARD_JOB} groups do not match the union check's --splits {splits}"
    )
    runs = [
        str(s["run"])
        for s in shard["steps"]
        if "pytest" in str(s.get("run", "")) and "--splits" in str(s.get("run", ""))
    ]
    assert len(runs) == 1, f"{SHARD_JOB} must run the sharded pytest in exactly one step"
    assert _flag(runs[0], r"--splits\s+(\d+)") == splits
    assert _flag(runs[0], r"--group\s+(\$\{\{\s*matrix\.group\s*\}\})") is not None
    assert _flag(runs[0], r'-m\s+"([^"]*)"') == marker, "shards and union check differ in marker"
    algorithm = r"--splitting-algorithm[ =](\S+)"
    assert _flag(runs[0], algorithm) == _flag(command, algorithm), (
        "shards and union check split with different algorithms"
    )

    uploads = [s for s in shard["steps"] if str(s.get("uses", "")).startswith("actions/upload-")]
    assert [u["with"]["name"] for u in uploads] == [SHARD_ARTIFACT], (
        f"{SHARD_JOB} must upload one artifact per shard named {SHARD_ARTIFACT!r}"
    )
    union_job = _job(wf, UNION_JOB)
    assert SHARD_JOB in _needs(union_job), f"{UNION_JOB} must wait for {SHARD_JOB}"
    downloads = [
        s["with"] for s in union_job["steps"] if str(s.get("uses", "")).startswith("actions/down")
    ]
    assert any(
        d.get("pattern") == SHARD_ARTIFACT_PATTERN and d.get("merge-multiple") is True
        for d in downloads
    ), f"{UNION_JOB} must download the artifacts of every shard ({SHARD_ARTIFACT_PATTERN})"


def check_union_failure_fails_the_step(wf: Workflow, stub_dir: Path) -> None:
    """Run the union step's command with a ``python`` that exits 0 or 1: the step must follow."""
    [(_, union)] = _union_steps(wf)
    stub = stub_dir / "python"
    for code in (0, 1):
        stub.write_text(f"#!/bin/sh\nexit {code}\n", encoding="utf-8")
        stub.chmod(0o755)
        done = subprocess.run(
            [_BASH or "bash", "-e", "-c", str(union["run"])],
            env={"PATH": f"{stub_dir}:/usr/bin:/bin"},
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert (done.returncode == 0) == (code == 0), (
            f"the union step exits {done.returncode} when the script exits {code}"
        )


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
    _job(wf, "ci-ok")["needs"] = [*_needs(_job(wf, "ci-ok")), "changes"]
    with pytest.raises(AssertionError, match=r"extra \['changes'\]"):
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


def test_removing_the_union_step_fails_the_pin() -> None:
    wf = _mutated()
    steps = _job(wf, UNION_JOB)["steps"]
    _job(wf, UNION_JOB)["steps"] = [s for s in steps if UNION_SCRIPT not in str(s.get("run", ""))]
    with pytest.raises(AssertionError, match="exactly one step, found 0"):
        check_union_wiring(wf)


def test_moving_the_union_step_to_another_job_fails_the_pin() -> None:
    wf = _mutated()
    steps = _job(wf, UNION_JOB)["steps"]
    moved = [s for s in steps if UNION_SCRIPT in str(s.get("run", ""))]
    _job(wf, UNION_JOB)["steps"] = [s for s in steps if s not in moved]
    _job(wf, "type-check")["steps"].extend(moved)
    with pytest.raises(AssertionError, match="runs in 'type-check'"):
        check_union_wiring(wf)


@pytest.mark.parametrize("key", ["if", "continue-on-error"])
def test_making_the_union_step_conditional_or_optional_fails_the_pin(key: str) -> None:
    wf = _mutated()
    [union] = [s for s in _job(wf, UNION_JOB)["steps"] if UNION_SCRIPT in str(s.get("run", ""))]
    union[key] = "false" if key == "if" else True
    with pytest.raises(AssertionError, match="must not be"):
        check_union_wiring(wf)


@pytest.mark.parametrize(
    "suffix", [" || true", " ; true", " && true", " | cat", "\ntrue", " --splits 4 || exit 0"]
)
def test_a_union_command_that_can_hide_its_failure_fails_the_pin(suffix: str) -> None:
    wf = _mutated()
    [union] = [s for s in _job(wf, UNION_JOB)["steps"] if UNION_SCRIPT in str(s.get("run", ""))]
    union["run"] = union["run"].rstrip() + suffix
    with pytest.raises(AssertionError, match="only the script and its flags"):
        check_union_wiring(wf)


@pytest.mark.parametrize("where", ["step", "job", "workflow"])
def test_a_shell_override_on_the_union_step_fails_the_pin(where: str) -> None:
    wf = _mutated()
    [union] = [s for s in _job(wf, UNION_JOB)["steps"] if UNION_SCRIPT in str(s.get("run", ""))]
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
    [union] = [s for s in _job(wf, UNION_JOB)["steps"] if UNION_SCRIPT in str(s.get("run", ""))]
    check_union_failure_fails_the_step(wf, tmp_path)
    union["run"] += " || true"
    with pytest.raises(AssertionError, match="the union step exits 0 when the script exits 1"):
        check_union_failure_fails_the_step(wf, tmp_path)


def test_union_splits_that_differ_from_the_shard_count_fails_the_pin() -> None:
    wf = _mutated()
    [union] = [s for s in _job(wf, UNION_JOB)["steps"] if UNION_SCRIPT in str(s.get("run", ""))]
    union["run"] = union["run"].replace("--splits 4", "--splits 3")
    with pytest.raises(AssertionError, match="groups do not match the union check's --splits 3"):
        check_union_wiring(wf)


def test_a_shard_missing_from_the_matrix_fails_the_pin() -> None:
    wf = _mutated()
    _job(wf, SHARD_JOB)["strategy"]["matrix"]["group"] = [1, 2, 3]
    with pytest.raises(AssertionError, match="groups do not match"):
        check_union_wiring(wf)


def test_a_different_marker_in_the_shards_fails_the_pin() -> None:
    wf = _mutated()
    [run] = [s for s in _job(wf, SHARD_JOB)["steps"] if "--splits" in str(s.get("run", ""))]
    run["run"] = run["run"].replace('-m "not timing"', '-m "not slow"')
    with pytest.raises(AssertionError, match="differ in marker"):
        check_union_wiring(wf)


def test_a_split_algorithm_only_the_shards_use_fails_the_pin() -> None:
    wf = _mutated()
    [run] = [s for s in _job(wf, SHARD_JOB)["steps"] if "--splits" in str(s.get("run", ""))]
    run["run"] = run["run"].replace("--splits 4", "--splits 4 --splitting-algorithm least_duration")
    with pytest.raises(AssertionError, match="different algorithms"):
        check_union_wiring(wf)


def test_a_union_job_that_stops_waiting_for_the_shards_fails_the_pin() -> None:
    wf = _mutated()
    _job(wf, UNION_JOB)["needs"] = []
    with pytest.raises(AssertionError, match=f"{UNION_JOB} must wait for {SHARD_JOB}"):
        check_union_wiring(wf)


def test_a_union_job_that_stops_downloading_every_shard_fails_the_pin() -> None:
    wf = _mutated()
    for step in _job(wf, UNION_JOB)["steps"]:
        if str(step.get("uses", "")).startswith("actions/download-artifact"):
            step["with"]["pattern"] = "coverage-shard-1"
    with pytest.raises(AssertionError, match="artifacts of every shard"):
        check_union_wiring(wf)


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
