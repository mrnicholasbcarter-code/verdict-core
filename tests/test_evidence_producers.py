"""Tests for evidence producer scripts (G4.2, G4.3, G6.1, G7.3)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest


@pytest.fixture
def evidence_dir(tmp_path: Path) -> Path:
    """Temporary evidence directory."""
    return tmp_path / "evidence"


def test_produce_parity_evidence_creates_matrix(evidence_dir: Path) -> None:
    """Test that parity evidence producer creates contract_parity_matrix.md."""
    result = subprocess.run(
        ["python", "scripts/produce_parity_evidence.py", "--evidence-dir", str(evidence_dir)],
        capture_output=True,
        text=True,
    )
    
    assert result.returncode == 0, f"Script failed: {result.stderr}"
    assert "RESULT: PASS" in result.stdout
    
    matrix_file = evidence_dir / "contract_parity_matrix.md"
    assert matrix_file.exists()
    
    content = matrix_file.read_text()
    assert "# Contract Parity Matrix" in content
    assert "TaskSpec" in content
    assert "RoutingDecision" in content
    assert "AvailabilitySnapshot" in content
    assert "RuntimeCandidate" in content
    assert "ExecutionEnvelope" in content


def test_produce_parity_evidence_creates_fixture_results(evidence_dir: Path) -> None:
    """Test that parity evidence producer creates parity_fixture_results.json."""
    result = subprocess.run(
        ["python", "scripts/produce_parity_evidence.py", "--evidence-dir", str(evidence_dir)],
        capture_output=True,
        text=True,
    )
    
    assert result.returncode == 0
    
    results_file = evidence_dir / "parity_fixture_results.json"
    assert results_file.exists()
    
    data = json.loads(results_file.read_text())
    assert "python" in data
    assert "typescript" in data


def test_produce_assignment_log_creates_schema(evidence_dir: Path) -> None:
    """Test that assignment log producer creates assignment_log_schema.json."""
    result = subprocess.run(
        ["python", "scripts/produce_assignment_log_evidence.py", "--evidence-dir", str(evidence_dir)],
        capture_output=True,
        text=True,
    )
    
    assert result.returncode == 0, f"Script failed: {result.stderr}"
    assert "RESULT: PASS" in result.stdout
    
    schema_file = evidence_dir / "assignment_log_schema.json"
    assert schema_file.exists()
    
    schema = json.loads(schema_file.read_text())
    assert schema["type"] == "object"
    assert "assignment_id" in schema["required"]
    assert "model" in schema["required"]
    assert "provider" in schema["required"]
    assert "availability_snapshot" in schema["required"]
    assert "estimated_cost_usd" in schema["required"]
    assert "reason" in schema["required"]


def test_produce_assignment_log_creates_sample(evidence_dir: Path) -> None:
    """Test that assignment log producer creates assignment_log_sample.json."""
    result = subprocess.run(
        ["python", "scripts/produce_assignment_log_evidence.py", "--evidence-dir", str(evidence_dir)],
        capture_output=True,
        text=True,
    )
    
    assert result.returncode == 0
    
    sample_file = evidence_dir / "assignment_log_sample.json"
    assert sample_file.exists()
    
    sample = json.loads(sample_file.read_text())
    assert "assignment_id" in sample
    assert "model" in sample
    assert "provider" in sample
    assert "availability_snapshot" in sample
    assert isinstance(sample["estimated_cost_usd"], (int, float))
    assert "reason" in sample


def test_produce_readme_verification_creates_log(evidence_dir: Path) -> None:
    """Test that README verification producer creates readme_verification.log."""
    result = subprocess.run(
        ["python", "scripts/produce_readme_verification.py", "--evidence-dir", str(evidence_dir)],
        capture_output=True,
        text=True,
    )
    
    assert result.returncode == 0, f"Script failed: {result.stderr}"
    assert "RESULT: PASS" in result.stdout
    
    log_file = evidence_dir / "readme_verification.log"
    assert log_file.exists()
    
    content = log_file.read_text()
    assert "README Verification Results" in content
    assert "PASS:" in content or "SKIPPED:" in content


def test_assignment_logger_schema_valid() -> None:
    """Test that ASSIGNMENT_LOG_SCHEMA is valid JSON Schema."""
    from verdict.assignment_logger import ASSIGNMENT_LOG_SCHEMA
    
    assert ASSIGNMENT_LOG_SCHEMA["type"] == "object"
    assert "assignment_id" in ASSIGNMENT_LOG_SCHEMA["required"]
    assert "timestamp" in ASSIGNMENT_LOG_SCHEMA["required"]
    assert "model" in ASSIGNMENT_LOG_SCHEMA["required"]


def test_assignment_logger_creates_record() -> None:
    """Test that create_assignment_log produces valid records."""
    from verdict.assignment_logger import create_assignment_log
    
    record = create_assignment_log(
        model="test/model",
        provider="test",
        availability_snapshot={"observed_at": "2024-01-01T00:00:00Z", "state": "healthy"},
        estimated_cost_usd=0.001,
        reason="test",
    )
    
    assert record.assignment_id
    assert record.model == "test/model"
    assert record.provider == "test"
    assert record.estimated_cost_usd == 0.001
    
    data = record.to_dict()
    assert "assignment_id" in data
    assert "timestamp" in data


def test_assignment_logger_sample_conforms_to_schema() -> None:
    """Test that create_sample_log produces schema-conforming output."""
    from verdict.assignment_logger import create_sample_log, ASSIGNMENT_LOG_SCHEMA
    
    sample = create_sample_log()
    
    # Validate required fields
    required = ASSIGNMENT_LOG_SCHEMA["required"]
    for field in required:
        assert field in sample, f"Missing required field: {field}"
    
    # Validate types
    assert isinstance(sample["assignment_id"], str)
    assert isinstance(sample["model"], str)
    assert isinstance(sample["provider"], str)
    assert isinstance(sample["availability_snapshot"], dict)
    assert isinstance(sample["estimated_cost_usd"], (int, float))
    assert isinstance(sample["reason"], str)
