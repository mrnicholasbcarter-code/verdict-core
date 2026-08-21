"""Tests for the public proof matrix and claims ledger."""

from pathlib import Path

import pytest

from scripts.verify_proof_matrix import LEDGER_PATH, MATRIX_PATH, validate


def test_checked_in_proof_artifacts_are_valid() -> None:
    matrix_rows, ledger_entries = validate()

    assert matrix_rows >= 10
    assert ledger_entries >= 8


def test_validator_rejects_missing_evidence(tmp_path: Path) -> None:
    matrix = tmp_path / "matrix.json"
    ledger = tmp_path / "ledger.json"
    matrix.write_text(MATRIX_PATH.read_text(encoding="utf-8"), encoding="utf-8")
    ledger.write_text(LEDGER_PATH.read_text(encoding="utf-8"), encoding="utf-8")
    payload = matrix.read_text(encoding="utf-8").replace(
        "verdict/eligibility.py", "verdict/not-a-real-file.py", 1
    )
    matrix.write_text(payload, encoding="utf-8")

    with pytest.raises(ValueError, match="does not exist"):
        validate(matrix, ledger)


def test_validator_rejects_secret_bearing_wording(tmp_path: Path) -> None:
    matrix = tmp_path / "matrix.json"
    ledger = tmp_path / "ledger.json"
    matrix.write_text(MATRIX_PATH.read_text(encoding="utf-8"), encoding="utf-8")
    ledger_payload = LEDGER_PATH.read_text(encoding="utf-8").replace(
        "A public claim may", "A api_key=secret public claim may", 1
    )
    ledger.write_text(ledger_payload, encoding="utf-8")

    with pytest.raises(ValueError, match="secret-bearing"):
        validate(matrix, ledger)
