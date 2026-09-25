"""Test artifact resolution logic in generate_gates_report.py."""

import json
import tempfile
from pathlib import Path

from scripts.generate_gates_report import Gate, _resolve_artifacts


def test_artifact_requires_explicit_pass():
    """Text artifact without RESULT: PASS should FAIL."""
    with tempfile.TemporaryDirectory() as tmpdir:
        evidence = Path(tmpdir)
        log = evidence / "test.log"
        log.write_text("Everything looks good\nAll tests passed\n")

        gate = Gate("TEST", "Test gate", artifacts=("test.log",))
        result = _resolve_artifacts(gate, evidence)

        assert result.status == "FAIL", "Artifact without RESULT: PASS should fail"
        assert "missing explicit RESULT: PASS" in result.body


def test_artifact_explicit_pass():
    """Text artifact with RESULT: PASS should PASS."""
    with tempfile.TemporaryDirectory() as tmpdir:
        evidence = Path(tmpdir)
        log = evidence / "test.log"
        log.write_text("Everything looks good\nAll tests passed\nRESULT: PASS\n")

        gate = Gate("TEST", "Test gate", artifacts=("test.log",))
        result = _resolve_artifacts(gate, evidence)

        assert result.status == "PASS", "Artifact with RESULT: PASS should pass"


def test_artifact_explicit_fail():
    """Text artifact with RESULT: FAIL should FAIL."""
    with tempfile.TemporaryDirectory() as tmpdir:
        evidence = Path(tmpdir)
        log = evidence / "test.log"
        log.write_text("Something went wrong\nRESULT: FAIL (exit 2)\n")

        gate = Gate("TEST", "Test gate", artifacts=("test.log",))
        result = _resolve_artifacts(gate, evidence)

        assert result.status == "FAIL", "Artifact with RESULT: FAIL should fail"


def test_json_artifact_no_result_needed():
    """JSON artifacts don't need RESULT: PASS, just valid JSON."""
    with tempfile.TemporaryDirectory() as tmpdir:
        evidence = Path(tmpdir)
        result_file = evidence / "results.json"
        result_file.write_text(json.dumps({"status": "ok", "data": [1, 2, 3]}))

        gate = Gate("TEST", "Test gate", artifacts=("results.json",))
        result = _resolve_artifacts(gate, evidence)

        assert result.status == "PASS", "Valid JSON artifact should pass"


def test_missing_artifact():
    """Missing artifact should be BLOCKED."""
    with tempfile.TemporaryDirectory() as tmpdir:
        evidence = Path(tmpdir)

        gate = Gate("TEST", "Test gate", artifacts=("missing.log",))
        result = _resolve_artifacts(gate, evidence)

        assert result.status == "BLOCKED", "Missing artifact should be blocked"
        assert "MISSING: missing.log" in result.body


def test_artifact_pass_then_fail_resolves_fail():
    """Log with RESULT: PASS followed by RESULT: FAIL should resolve FAIL (use last)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        evidence = Path(tmpdir)
        log = evidence / "test.log"
        log.write_text("""First attempt passed
RESULT: PASS
Retry failed
RESULT: FAIL (exit 2)
""")

        gate = Gate("TEST", "Test gate", artifacts=("test.log",))
        result = _resolve_artifacts(gate, evidence)

        assert result.status == "FAIL", "RESULT: FAIL after RESULT: PASS should fail"
        assert "RESULT: FAIL" in result.body
