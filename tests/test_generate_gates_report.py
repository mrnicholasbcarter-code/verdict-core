"""The gate report must describe evidence that exists, and nothing else."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import generate_gates_report as generator
import verify_gates as verifier

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def evidence_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "evidence"
    directory.mkdir()
    return directory


def _report(evidence_dir: Path) -> dict:
    return generator.build_report(evidence_dir, REPO_ROOT)


def _gate(report: dict, gate_id: str) -> dict:
    return next(gate for gate in report["gates"] if gate["id"] == gate_id)


def test_report_covers_every_documented_gate_in_order():
    assert tuple(gate.gate_id for gate in generator.GATES) == verifier.GATE_IDS


def test_generated_report_satisfies_the_verifier_with_no_evidence_at_all(evidence_dir: Path):
    """An empty evidence directory must still produce a structurally valid report.

    The point of the generator is that "nothing was produced" is a reportable
    outcome, not a crash: the verifier has to be able to say which gates are
    blocked rather than reject the report wholesale.
    """
    report = _report(evidence_dir)
    evidence_dir.joinpath(generator.REPORT_NAME).write_bytes(generator._canonical_json(report))

    summary = verifier.validate_gates(evidence_dir)
    assert summary["gate_count"] == len(verifier.GATE_IDS)
    assert summary["all_passed"] is False


def test_absent_evidence_is_blocked_never_passed(evidence_dir: Path):
    report = _report(evidence_dir)
    assert _gate(report, "G5.1")["status"] == "BLOCKED"
    assert _gate(report, "G6.2")["status"] == "BLOCKED"


def test_a_missing_junit_report_blocks_every_test_backed_gate(evidence_dir: Path):
    report = _report(evidence_dir)
    for gate in generator.GATES:
        if gate.tests:
            assert _gate(report, gate.gate_id)["status"] == "BLOCKED"


def test_a_present_artifact_passes_its_gate(evidence_dir: Path):
    evidence_dir.joinpath("benchmark_results.json").write_text("{}", encoding="utf-8")
    report = _report(evidence_dir)
    gate = _gate(report, "G6.2")
    assert gate["status"] == "PASS"
    assert "benchmark_results.json" in gate["evidence"]


def test_a_failing_test_case_fails_its_gate_rather_than_blocking_it(evidence_dir: Path):
    evidence_dir.joinpath(generator.JUNIT_NAME).write_text(
        '<testsuites><testsuite name="t">'
        '<testcase name="test_deterministic_selection"><failure>boom</failure></testcase>'
        "</testsuite></testsuites>",
        encoding="utf-8",
    )
    assert _gate(_report(evidence_dir), "G1.4")["status"] == "FAIL"


def test_a_passing_test_case_passes_its_gate(evidence_dir: Path):
    evidence_dir.joinpath(generator.JUNIT_NAME).write_text(
        '<testsuites><testsuite name="t">'
        '<testcase name="test_deterministic_selection"/>'
        "</testsuite></testsuites>",
        encoding="utf-8",
    )
    gate = _gate(_report(evidence_dir), "G1.4")
    assert gate["status"] == "PASS"
    assert generator.JUNIT_NAME in gate["evidence"]


def test_a_skipped_test_case_does_not_pass_its_gate(evidence_dir: Path):
    evidence_dir.joinpath(generator.JUNIT_NAME).write_text(
        '<testsuites><testsuite name="t">'
        '<testcase name="test_deterministic_selection"><skipped/></testcase>'
        "</testsuite></testsuites>",
        encoding="utf-8",
    )
    assert _gate(_report(evidence_dir), "G1.4")["status"] == "FAIL"


def test_every_gate_cites_at_least_one_file_that_exists(evidence_dir: Path):
    report = _report(evidence_dir)
    for gate in report["gates"]:
        assert gate["evidence"], f"{gate['id']} cites no evidence"
        for relative in gate["evidence"]:
            assert (evidence_dir / relative).is_file(), f"{gate['id']} cites missing {relative}"


def test_an_advisory_verifier_step_fails_the_gate_workflow_check(tmp_path: Path):
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    workflows.joinpath("acceptance-gates.yml").write_text(
        "jobs:\n  g:\n    steps:\n"
        "      - name: Verify\n        continue-on-error: true\n"
        "        run: python scripts/verify_gates.py --evidence-dir evidence\n",
        encoding="utf-8",
    )
    assert generator._check_gate_workflow(tmp_path).status == "FAIL"


def test_a_repository_without_a_verifier_step_blocks_the_gate_workflow_check(tmp_path: Path):
    (tmp_path / ".github" / "workflows").mkdir(parents=True)
    assert generator._check_gate_workflow(tmp_path).status == "BLOCKED"


def test_this_repository_runs_the_verifier_non_advisorily():
    assert generator._check_gate_workflow(REPO_ROOT).status == "PASS"


def test_advisory_evidence_steps_do_not_taint_the_supply_chain_check():
    """Evidence producers are advisory on purpose; the scanners are not."""
    assert generator._check_supply_chain_scans(REPO_ROOT).status == "PASS"


def test_the_cli_writes_a_canonical_report(evidence_dir: Path):
    result = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "generate_gates_report.py"),
            "--evidence-dir",
            str(evidence_dir),
            "--repo-root",
            str(REPO_ROOT),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    raw = evidence_dir.joinpath(generator.REPORT_NAME).read_bytes()
    assert raw.endswith(b"\n")
    assert raw == generator._canonical_json(json.loads(raw))


def test_the_verifier_rejects_the_repository_today(evidence_dir: Path):
    """Launch readiness is genuinely incomplete; the verifier must say so."""
    subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "generate_gates_report.py"),
            "--evidence-dir",
            str(evidence_dir),
            "--repo-root",
            str(REPO_ROOT),
        ],
        capture_output=True,
        check=True,
    )
    result = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "verify_gates.py"),
            "--evidence-dir",
            str(evidence_dir),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
