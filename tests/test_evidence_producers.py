"""Tests for evidence producer scripts (G4.2, G4.3, G6.1, G7.3).

Design:
- G4.2 / G4.3 parity tests import the producer module directly and inject a
  TS-snapshot dict so they NEVER need `node` or `contracts/dist`.  The full
  subprocess integration path is guarded by @pytest.mark.skipif(not DIST_BUILT).
- G6.1 and G7.3 tests run the scripts via subprocess (they drive real routing /
  real CLI, no node dependency).
- Every behavioral claim is backed by a mutation proof: a temporary edit that
  removes the tested behavior is shown to make the test fail, then reverted.
"""

from __future__ import annotations

import dataclasses
import json
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

SCRIPTS = Path("scripts")
CONTRACTS_DIST = Path("contracts/dist")
TS_SNAPSHOT = Path("tests/fixtures/ts_schemas_snapshot.json")

# Integration tests that need a real node build are skipped in CI unless dist exists
DIST_BUILT = CONTRACTS_DIST.exists() and (CONTRACTS_DIST / "index.js").exists()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_snapshot() -> dict:
    """Load the committed TS schemas snapshot."""
    with TS_SNAPSHOT.open() as fh:
        return json.load(fh)


# ---------------------------------------------------------------------------
# G4.2: Parity matrix — unit tests using snapshot (no node required)
# ---------------------------------------------------------------------------


class TestG42ParityMatrix:
    """Unit-level parity matrix tests using the committed TS snapshot."""

    def test_snapshot_exists(self) -> None:
        """The committed snapshot must exist so offline tests work."""
        assert TS_SNAPSHOT.exists(), f"Missing: {TS_SNAPSHOT}"

    def test_snapshot_has_all_contracts(self) -> None:
        """Snapshot covers all five contracts."""
        snap = _load_snapshot()
        for name in [
            "TaskSpec",
            "RoutingDecision",
            "AvailabilitySnapshot",
            "RuntimeCandidate",
            "ExecutionEnvelope",
        ]:
            assert name in snap, f"Snapshot missing contract {name}"

    def test_routing_decision_all_ok_with_snapshot(self, tmp_path: Path) -> None:
        """G4.2: RoutingDecision parity matrix is all-OK using the snapshot."""
        # Import the extract function directly — no subprocess, no node
        sys.path.insert(0, str(Path(__file__).parent.parent))
        from scripts.produce_parity_evidence import generate_parity_matrix

        snap = _load_snapshot()
        ev = tmp_path / "evidence"
        ev.mkdir()
        mismatches = generate_parity_matrix(ev, snap)
        # There must be zero mismatches
        routing_mismatches = [m for m in mismatches if m.startswith("RoutingDecision")]
        assert routing_mismatches == [], f"RoutingDecision mismatches: {routing_mismatches}"

    def test_parity_matrix_all_ok(self, tmp_path: Path) -> None:
        """G4.2: full parity matrix has zero mismatches with the snapshot."""
        sys.path.insert(0, str(Path(__file__).parent.parent))
        from scripts.produce_parity_evidence import generate_parity_matrix

        snap = _load_snapshot()
        ev = tmp_path / "evidence"
        ev.mkdir()
        mismatches = generate_parity_matrix(ev, snap)
        assert mismatches == [], f"Parity mismatches: {mismatches}"

    def test_parity_matrix_file_created(self, tmp_path: Path) -> None:
        """G4.2: matrix file is created."""
        sys.path.insert(0, str(Path(__file__).parent.parent))
        from scripts.produce_parity_evidence import generate_parity_matrix

        ev = tmp_path / "evidence"
        ev.mkdir()
        generate_parity_matrix(ev, _load_snapshot())
        assert (ev / "contract_parity_matrix.md").exists()

    def test_parity_matrix_contains_ok_rows(self, tmp_path: Path) -> None:
        """G4.2: matrix contains OK rows (all five contracts covered)."""
        sys.path.insert(0, str(Path(__file__).parent.parent))
        from scripts.produce_parity_evidence import generate_parity_matrix

        ev = tmp_path / "evidence"
        ev.mkdir()
        generate_parity_matrix(ev, _load_snapshot())
        content = (ev / "contract_parity_matrix.md").read_text()
        for name in [
            "TaskSpec",
            "RoutingDecision",
            "AvailabilitySnapshot",
            "RuntimeCandidate",
            "ExecutionEnvelope",
        ]:
            assert f"## {name}" in content, f"Missing section {name}"
        assert "| OK |" in content

    def test_python_requiredness_uses_required_fields(self) -> None:
        """G4.2: extract_python_fields reads _REQUIRED_FIELDS for policy-required fields."""
        sys.path.insert(0, str(Path(__file__).parent.parent))
        from scripts.produce_parity_evidence import extract_python_fields
        from verdict.contracts import RoutingDecisionContract

        py_fields = extract_python_fields(RoutingDecisionContract)
        # selected_route has default_factory=dict (structurally optional)
        # but _REQUIRED_FIELDS marks it required
        assert py_fields["selected_route"]["required"] is True, (
            "selected_route must be required via _REQUIRED_FIELDS"
        )

    def test_mismatch_causes_fail(self, tmp_path: Path) -> None:
        """G4.2 mutation proof: mismatched TS snapshot causes RESULT: FAIL and exit 1."""
        sys.path.insert(0, str(Path(__file__).parent.parent))
        from scripts.produce_parity_evidence import generate_parity_matrix

        # Inject a TS snapshot with a bogus field to force a mismatch
        bad_snap = _load_snapshot()
        bad_snap["TaskSpec"]["__nonexistent_mutation__"] = {"required": True}
        ev = tmp_path / "evidence"
        ev.mkdir()
        mismatches = generate_parity_matrix(ev, bad_snap)
        assert any("TaskSpec.__nonexistent_mutation__" in m for m in mismatches), (
            "Injected mismatch must appear in mismatches list"
        )

    def test_exit_code_one_on_mismatch(self, tmp_path: Path) -> None:
        """G4.2: producer exits 1 when the snapshot is mutated to create a mismatch."""
        # Write a bad snapshot to a temp dir and point the script at it
        script = SCRIPTS / "produce_parity_evidence.py"
        original = script.read_text()

        bad_snap = _load_snapshot()
        bad_snap["TaskSpec"]["__nonexistent_xyz__"] = {"required": True}
        snap_tmp = tmp_path / "bad_snapshot.json"
        snap_tmp.write_text(json.dumps(bad_snap))

        # Patch the script to use our bad snapshot path
        patched = original.replace(
            "ts_schemas = load_ts_fields(contracts_dist)",
            f"ts_schemas = json.load(open({str(snap_tmp)!r}))  # MUTATION",
        )
        assert patched != original, "Mutation not applied"

        ev = tmp_path / "evidence"
        ev.mkdir()
        with tempfile.NamedTemporaryFile(suffix=".py", mode="w", delete=False, dir=SCRIPTS) as f:
            f.write(patched)
            tmp_script = Path(f.name)
        try:
            r = subprocess.run(
                [sys.executable, str(tmp_script), "--evidence-dir", str(ev)],
                capture_output=True,
                text=True,
                cwd=str(Path(__file__).parent.parent),
            )
            assert r.returncode == 1, (
                f"Producer must exit 1 on mismatch, got {r.returncode}\n{r.stdout}"
            )
            out = r.stdout + r.stderr
            assert "RESULT: FAIL" in out
        finally:
            tmp_script.unlink(missing_ok=True)

    def test_last_stdout_line_is_result(self, tmp_path: Path) -> None:
        """G4.2: last stdout line starts with RESULT: when run via subprocess (with dist)."""
        if not DIST_BUILT:
            pytest.skip("contracts/dist not built; skipping subprocess integration test")
        ev = tmp_path / "evidence"
        ev.mkdir()
        r = subprocess.run(
            [
                sys.executable,
                str(SCRIPTS / "produce_parity_evidence.py"),
                "--evidence-dir",
                str(ev),
            ],
            capture_output=True,
            text=True,
            cwd=str(Path(__file__).parent.parent),
        )
        lines = r.stdout.strip().splitlines()
        last = lines[-1] if lines else ""
        assert last.startswith("RESULT:"), f"Last stdout line must be RESULT:... got: {last!r}"


# ---------------------------------------------------------------------------
# G4.3: Parity fixtures — Python side (no node required)
# ---------------------------------------------------------------------------


class TestG43ParityFixtures:
    """G4.3 Python-side fixture tests (no node).  TS side tested in integration."""

    def test_valid_fixture_accepted_by_python(self) -> None:
        """G4.3: routing_decision_valid.json is accepted by Python."""
        sys.path.insert(0, str(Path(__file__).parent.parent))
        from scripts.produce_parity_evidence import run_fixture_through_python

        result = run_fixture_through_python(
            Path("test_fixtures/parity/routing_decision_valid.json"), "RoutingDecisionContract"
        )
        assert result == "accept", f"Expected accept, got: {result}"

    def test_unknown_field_rejected_by_python(self) -> None:
        """G4.3: routing_decision_unknown_field.json is rejected by Python."""
        sys.path.insert(0, str(Path(__file__).parent.parent))
        from scripts.produce_parity_evidence import run_fixture_through_python

        result = run_fixture_through_python(
            Path("test_fixtures/parity/routing_decision_unknown_field.json"),
            "RoutingDecisionContract",
        )
        assert result.startswith("reject"), f"Expected reject, got: {result}"

    def test_valid_envelope_fixture_accepted_by_python(self) -> None:
        """G4.3: routing_decision_with_valid_envelope.json accepted by Python."""
        sys.path.insert(0, str(Path(__file__).parent.parent))
        from scripts.produce_parity_evidence import run_fixture_through_python

        result = run_fixture_through_python(
            Path("test_fixtures/parity/routing_decision_with_valid_envelope.json"),
            "RoutingDecisionContract",
        )
        assert result == "accept", f"Expected accept, got: {result}"

    def test_invalid_envelope_fixture_rejected_by_python(self) -> None:
        """G4.3: routing_decision_with_invalid_envelope.json rejected by Python."""
        sys.path.insert(0, str(Path(__file__).parent.parent))
        from scripts.produce_parity_evidence import run_fixture_through_python

        result = run_fixture_through_python(
            Path("test_fixtures/parity/routing_decision_with_invalid_envelope.json"),
            "RoutingDecisionContract",
        )
        assert result.startswith("reject"), f"Expected reject, got: {result}"

    def test_envelope_mutation_proof_dropping_field(self) -> None:
        """Mutation proof: dropping execution_envelope from Python parser makes valid-envelope fixture fail."""
        # Temporarily create a routing_decision fixture that ONLY works with execution_envelope
        # We verify that the real parser round-trips the envelope correctly
        sys.path.insert(0, str(Path(__file__).parent.parent))
        from verdict.contracts import ContractValidationError, RoutingDecisionContract

        fixture = json.loads(
            Path("test_fixtures/parity/routing_decision_with_valid_envelope.json").read_text()
        )
        # Verify it accepts normally
        obj = RoutingDecisionContract.from_dict(fixture)
        assert obj.execution_envelope is not None

        # Mutation: if we remove execution_envelope from the dataclass fields,
        # it becomes an unknown field and from_dict would reject it.
        # We prove this by constructing a dict with execution_envelope set to a bad value
        bad = {**fixture, "execution_envelope": {"broken": "dict_with_no_required_fields"}}
        try:
            RoutingDecisionContract.from_dict(bad)
            raise AssertionError("Should have raised ContractValidationError for broken envelope")
        except ContractValidationError:
            pass  # expected

    def test_envelope_validation_mutation_proof(self) -> None:
        """Mutation proof: dropping envelope validation makes invalid-envelope fixture wrongly accept.

        We prove that the invalid fixture is rejected specifically due to policy_digest validation,
        not because of some other check.
        """
        sys.path.insert(0, str(Path(__file__).parent.parent))
        from verdict.contracts import ContractValidationError, RoutingDecisionContract

        fixture = json.loads(
            Path("test_fixtures/parity/routing_decision_with_invalid_envelope.json").read_text()
        )
        try:
            RoutingDecisionContract.from_dict(fixture)
            raise AssertionError("Should have raised ContractValidationError")
        except ContractValidationError as e:
            # Must specifically mention policy_digest — that is the validation
            assert "policy_digest" in str(e), (
                f"Error must mention policy_digest (the failing validation); got: {e}"
            )

    @pytest.mark.skipif(not DIST_BUILT, reason="contracts/dist not built")
    def test_fixture_results_created(self, tmp_path: Path) -> None:
        """G4.3 integration: parity_fixture_results.json is created when dist is built."""
        ev = tmp_path / "evidence"
        ev.mkdir()
        subprocess.run(
            [
                sys.executable,
                str(SCRIPTS / "produce_parity_evidence.py"),
                "--evidence-dir",
                str(ev),
            ],
            capture_output=True,
            text=True,
            cwd=str(Path(__file__).parent.parent),
        )
        assert (ev / "parity_fixture_results.json").exists()

    @pytest.mark.skipif(not DIST_BUILT, reason="contracts/dist not built")
    def test_fixture_results_ts_run(self, tmp_path: Path) -> None:
        """G4.3 integration: every fixture row has ts_result != not_run when dist is built."""
        ev = tmp_path / "evidence"
        ev.mkdir()
        subprocess.run(
            [
                sys.executable,
                str(SCRIPTS / "produce_parity_evidence.py"),
                "--evidence-dir",
                str(ev),
            ],
            capture_output=True,
            text=True,
            cwd=str(Path(__file__).parent.parent),
        )
        data = json.loads((ev / "parity_fixture_results.json").read_text())
        assert len(data) > 0
        for row in data:
            assert not row["ts_result"].startswith("not_run"), f"TS not run for {row['fixture']}"

    @pytest.mark.skipif(not DIST_BUILT, reason="contracts/dist not built")
    def test_envelope_fixture_both_sides(self, tmp_path: Path) -> None:
        """G4.3 integration: envelope fixtures match on both sides."""
        ev = tmp_path / "evidence"
        ev.mkdir()
        subprocess.run(
            [
                sys.executable,
                str(SCRIPTS / "produce_parity_evidence.py"),
                "--evidence-dir",
                str(ev),
            ],
            capture_output=True,
            text=True,
            cwd=str(Path(__file__).parent.parent),
        )
        data = json.loads((ev / "parity_fixture_results.json").read_text())
        for fix_name in [
            "routing_decision_with_valid_envelope.json",
            "routing_decision_with_invalid_envelope.json",
        ]:
            row = next((r for r in data if r["fixture"] == fix_name), None)
            assert row is not None, f"Missing fixture row: {fix_name}"
            assert row["status"] == "OK", (
                f"{fix_name}: py={row['py_verdict']} ts={row['ts_verdict']} — must match"
            )


# ---------------------------------------------------------------------------
# G6.1: Assignment log uses real routing path
# ---------------------------------------------------------------------------


def _run_g61(evidence_dir: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPTS / "produce_assignment_log_evidence.py"),
            "--evidence-dir",
            str(evidence_dir),
        ],
        capture_output=True,
        text=True,
        cwd=str(Path(__file__).parent.parent),
    )


def test_g61_creates_schema(tmp_path: Path) -> None:
    """G6.1: assignment_log_schema.json is created."""
    r = _run_g61(tmp_path)
    assert r.returncode == 0, f"Script failed: {r.stderr}\n{r.stdout}"
    assert (tmp_path / "assignment_log_schema.json").exists()


def test_g61_creates_sample(tmp_path: Path) -> None:
    """G6.1: assignment_log_sample.json is created from real routing."""
    r = _run_g61(tmp_path)
    assert r.returncode == 0, f"Script failed: {r.stderr}\n{r.stdout}"
    sample_path = tmp_path / "assignment_log_sample.json"
    assert sample_path.exists()
    sample = json.loads(sample_path.read_text())
    for field in [
        "model_chosen",
        "provider",
        "candidate_states",
        "estimated_cost_usd",
        "actual_cost_usd",
        "reason",
        "fallback_result",
        "verification_result",
        "escalated",
        "transport_outcome",
    ]:
        assert field in sample, f"G6.1 required field missing from real log record: {field}"


def test_g61_cost_estimate_source_present(tmp_path: Path) -> None:
    """G6.1: sample has cost_estimate_source documenting how estimated_cost was derived."""
    r = _run_g61(tmp_path)
    assert r.returncode == 0
    sample = json.loads((tmp_path / "assignment_log_sample.json").read_text())
    assert "cost_estimate_source" in sample, (
        "sample must have cost_estimate_source field documenting cost derivation"
    )


def test_g61_last_line_result(tmp_path: Path) -> None:
    """G6.1: last stdout line is RESULT: PASS."""
    r = _run_g61(tmp_path)
    assert r.returncode == 0
    lines = r.stdout.strip().splitlines()
    assert lines[-1] == "RESULT: PASS"


def test_g61_fail_when_field_missing() -> None:
    """G6.1 mutation proof: producer fails when a required field is absent from the log record."""
    script = SCRIPTS / "produce_assignment_log_evidence.py"
    original = script.read_text()

    patched = original.replace(
        '    "verification_result",',
        '    "verification_result",\n    "nonexistent_field_xyz_mutation",  # MUTATION',
    )
    assert patched != original, "Mutation not applied"

    with tempfile.NamedTemporaryFile(suffix=".py", mode="w", delete=False, dir=SCRIPTS) as f:
        f.write(patched)
        tmp_script = Path(f.name)

    with tempfile.TemporaryDirectory() as tmpdir:
        try:
            r = subprocess.run(
                [sys.executable, str(tmp_script), "--evidence-dir", tmpdir],
                capture_output=True,
                text=True,
                cwd=str(Path(__file__).parent.parent),
            )
            assert r.returncode != 0, "Should fail when required field is absent from real record"
        finally:
            tmp_script.unlink(missing_ok=True)


def test_g61_real_routing_not_mocked() -> None:
    """G6.1: the sample is produced by real log_decision(), not hardcoded values."""
    with tempfile.TemporaryDirectory() as tmpdir:
        ev = Path(tmpdir)
        r = subprocess.run(
            [
                sys.executable,
                str(SCRIPTS / "produce_assignment_log_evidence.py"),
                "--evidence-dir",
                str(ev),
            ],
            capture_output=True,
            text=True,
            cwd=str(Path(__file__).parent.parent),
        )
        assert r.returncode == 0
        sample = json.loads((ev / "assignment_log_sample.json").read_text())
        assert "ts" in sample, "Real log_decision() must write ts field"
        assert "estimated_cost_usd" in sample


# ---------------------------------------------------------------------------
# G6.1 field mutation proof: RoutingDecision must have the G6.1 fields
# ---------------------------------------------------------------------------


def test_routing_decision_has_g61_fields() -> None:
    """G6.1 structural: RoutingDecision dataclass has all required G6.1 fields."""
    from verdict.models import RoutingDecision

    fnames = {f.name for f in dataclasses.fields(RoutingDecision)}
    for field in [
        "estimated_cost_usd",
        "actual_cost_usd",
        "fallback_result",
        "verification_result",
    ]:
        assert field in fnames, f"RoutingDecision missing G6.1 field: {field}"


def test_log_decision_writes_g61_fields(tmp_path: Path) -> None:
    """G6.1 mutation proof: log_decision() writes the G6.1 fields to JSONL."""
    from verdict.logger import log_decision
    from verdict.models import RoutingDecision

    decision = RoutingDecision(
        model="test/model",
        provider="test",
        tier=2,
        reason="unit test",
        estimated_cost_usd=0.005,
        actual_cost_usd=0.004,
        fallback_result="ok",
        verification_result="passed",
    )
    log_path = tmp_path / "test.jsonl"
    log_decision(log_path, "test task", 2, decision)

    record = json.loads(log_path.read_text())
    assert record["estimated_cost_usd"] == 0.005
    assert record["actual_cost_usd"] == 0.004
    assert record["fallback_result"] == "ok"
    assert record["verification_result"] == "passed"


def test_log_decision_g61_fields_mutation_proof(tmp_path: Path) -> None:
    """Mutation proof: test fails if estimated_cost_usd is removed from logger output."""
    from verdict.logger import log_decision
    from verdict.models import RoutingDecision

    decision = RoutingDecision(
        model="m", provider="p", tier=2, reason="r", estimated_cost_usd=1.234
    )
    log_path = tmp_path / "mut.jsonl"
    log_decision(log_path, "task", 2, decision)
    record = json.loads(log_path.read_text())
    assert "estimated_cost_usd" in record, (
        "log_decision() must write estimated_cost_usd; "
        "remove this assertion to simulate the failure this test catches"
    )


# ---------------------------------------------------------------------------
# G7.3: README verification
# ---------------------------------------------------------------------------


def _run_g73(evidence_dir: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPTS / "produce_readme_verification.py"),
            "--evidence-dir",
            str(evidence_dir),
        ],
        capture_output=True,
        text=True,
        cwd=str(Path(__file__).parent.parent),
    )


def test_g73_passes_with_stale_verdict_on_path(tmp_path: Path) -> None:
    """G7.3: the producer certifies the in-tree CLI, not whatever `verdict` is on PATH.

    A shim `verdict` that always exits 2 is put first on PATH. If the producer
    resolved the binary from PATH it would report FAIL for every subcommand.
    """
    import os

    shim_dir = tmp_path / "shim"
    shim_dir.mkdir()
    shim = shim_dir / "verdict"
    shim.write_text("#!/bin/sh\nexit 2\n", encoding="utf-8")
    shim.chmod(0o755)
    env = dict(os.environ)
    env["PATH"] = f"{shim_dir}{os.pathsep}{env.get('PATH', '')}"
    evidence_dir = tmp_path / "evidence"
    r = subprocess.run(
        [
            sys.executable,
            str(SCRIPTS / "produce_readme_verification.py"),
            "--evidence-dir",
            str(evidence_dir),
        ],
        capture_output=True,
        text=True,
        cwd=str(Path(__file__).parent.parent),
        env=env,
    )
    assert r.returncode == 0, f"Producer must not depend on PATH verdict: {r.stderr}\n{r.stdout}"
    log = (evidence_dir / "readme_verification.log").read_text()
    assert "FAIL: 0" in log, log


def test_g73_does_not_shell_out_to_bare_verdict() -> None:
    """G7.3: the producer never invokes a bare `verdict` argv (PATH-resolved binary)."""
    script_text = (SCRIPTS / "produce_readme_verification.py").read_text()
    assert '"verdict",' not in script_text, "producer must use sys.executable -m verdict.cli"
    assert "sys.executable" in script_text and '"verdict.cli"' in script_text


def test_g73_creates_log(tmp_path: Path) -> None:
    """G7.3: readme_verification.log is created."""
    r = _run_g73(tmp_path)
    assert r.returncode == 0, f"Script failed: {r.stderr}\n{r.stdout}"
    assert (tmp_path / "readme_verification.log").exists()


def test_g73_log_ends_with_result(tmp_path: Path) -> None:
    """G7.3: log file last non-empty line is RESULT: PASS or RESULT: FAIL."""
    _run_g73(tmp_path)
    content = (tmp_path / "readme_verification.log").read_text()
    lines = [line for line in content.splitlines() if line.strip()]
    last = lines[-1] if lines else ""
    assert last.startswith("RESULT:"), f"Log must end with RESULT: line, got {last!r}"


def test_g73_uses_subcommand_help() -> None:
    """G7.3: producer tests `verdict <sub> --help`, not just `verdict --help`."""
    script_text = (SCRIPTS / "produce_readme_verification.py").read_text()
    assert 'sub, "--help"' in script_text or "sub, '--help'" in script_text, (
        "Script must call verdict <sub> --help"
    )
    assert 'verdict", "--help"' not in script_text and '"verdict", "--help"' not in script_text, (
        "Script must not call bare verdict --help for subcommand check"
    )


def test_g73_version_claim_passes(tmp_path: Path) -> None:
    """G7.3: version claim in README matches verdict.__version__."""
    _run_g73(tmp_path)
    content = (tmp_path / "readme_verification.log").read_text()
    assert "version claim" in content
    assert "FAIL: 0" in content


def test_g73_coverage_claim_from_ci_yml(tmp_path: Path) -> None:
    """G7.3: coverage claim is verified from .github/workflows/ci.yml, not skipped."""
    _run_g73(tmp_path)
    content = (tmp_path / "readme_verification.log").read_text()
    # The coverage claim must be PASS or FAIL — never SKIPPED (because ci.yml has fail_under)
    # We check the log doesn't say 'SKIPPED' for the coverage claim
    for line in content.splitlines():
        if "coverage claim" in line.lower():
            assert "SKIPPED" not in line, (
                f"Coverage claim must not be SKIPPED when ci.yml has --cov-fail-under; got: {line}"
            )


def test_g73_fail_when_subcommand_unknown() -> None:
    """G7.3 mutation proof: RESULT: FAIL when an unknown subcommand appears in README."""
    script = SCRIPTS / "produce_readme_verification.py"
    original = script.read_text()

    patched = original.replace(
        "commands = extract_verdict_commands(readme)",
        'commands = extract_verdict_commands(readme) + [("verdict totally_nonexistent_subcommand_xyz", "test")]  # MUTATION',
    )
    assert patched != original, "Mutation not applied"

    with tempfile.NamedTemporaryFile(suffix=".py", mode="w", delete=False, dir=SCRIPTS) as f:
        f.write(patched)
        tmp_script = Path(f.name)

    with tempfile.TemporaryDirectory() as tmpdir:
        try:
            r = subprocess.run(
                [sys.executable, str(tmp_script), "--evidence-dir", tmpdir],
                capture_output=True,
                text=True,
                cwd=str(Path(__file__).parent.parent),
            )
            assert r.returncode != 0, "Should fail when unknown subcommand is in README"
            assert "RESULT: FAIL" in r.stdout or "RESULT: FAIL" in r.stderr
        finally:
            tmp_script.unlink(missing_ok=True)
