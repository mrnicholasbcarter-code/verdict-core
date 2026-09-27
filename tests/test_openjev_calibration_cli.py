"""Tests for openjev_calibration_report CLI."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path

import pytest
PYTHON = Path(__file__).parent.parent / ".venv" / "bin" / "python"


FIXTURES = Path(__file__).parent / "fixtures" / "openjev" / "calibration"
SCRIPT = Path(__file__).parent.parent / "scripts" / "openjev_calibration_report.py"


def test_cli_prints_markdown_by_default() -> None:
    """CLI prints markdown output by default."""
    result = subprocess.run(
        [str(PYTHON), str(SCRIPT), str(FIXTURES / "replay_sample.jsonl")],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert "# OpenJev calibration report" in result.stdout
    assert "recommended state:" in result.stdout


def test_cli_json_output() -> None:
    """CLI outputs JSON when --json flag is used."""
    result = subprocess.run(
        [str(PYTHON), str(SCRIPT), "--json", str(FIXTURES / "replay_sample.jsonl")],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    data = json.loads(result.stdout)
    assert "schema" in data
    assert data["schema"] == "verdict.openjev-calibration/v1"
    assert "records" in data
    assert "recommended_state" in data


def test_cli_custom_threshold() -> None:
    """CLI accepts custom threshold via --threshold flag."""
    result = subprocess.run(
        [str(PYTHON), str(SCRIPT), "--threshold", "0.7", str(FIXTURES / "replay_sample.jsonl")],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    # Should still produce valid output with custom threshold
    assert "# OpenJev calibration report" in result.stdout


def test_cli_custom_min_confidence() -> None:
    """CLI accepts custom min_confidence via --min-confidence flag."""
    result = subprocess.run(
        [str(PYTHON), str(SCRIPT), "--min-confidence", "0.5", str(FIXTURES / "replay_sample.jsonl")],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert "# OpenJev calibration report" in result.stdout


def test_cli_missing_file() -> None:
    """CLI exits non-zero when input file is missing."""
    result = subprocess.run(
        [str(PYTHON), str(SCRIPT), "/nonexistent/file.jsonl"],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "not found" in result.stderr.lower()
