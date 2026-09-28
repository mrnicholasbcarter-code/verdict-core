"""Tests for proof matrix doc freshness and evidence integrity (BOD-203)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MATRIX_JSON = ROOT / "docs" / "proof" / "proof_matrix.v1.json"
GENERATED_MD = ROOT / "docs" / "PROOF_MATRIX.md"
GENERATOR = ROOT / "scripts" / "generate_proof_matrix.py"


@pytest.fixture(scope="module")
def matrix() -> dict:
    return json.loads(MATRIX_JSON.read_text(encoding="utf-8"))


class TestProofMatrixDocFreshness:
    """Generated doc must match what the generator produces from the JSON source."""

    def test_generated_doc_exists(self) -> None:
        assert GENERATED_MD.is_file(), (
            f"{GENERATED_MD.relative_to(ROOT)} missing; "
            "run: python scripts/generate_proof_matrix.py"
        )

    def test_generated_doc_is_not_stale(self) -> None:
        """Regenerate and compare to catch drift."""
        result = subprocess.run(
            [sys.executable, str(GENERATOR), "--check"],
            capture_output=True,
            text=True,
            cwd=str(ROOT),
        )
        assert result.returncode == 0, (
            f"Proof matrix doc is stale:\n{result.stderr}\n"
            "Regenerate with: python scripts/generate_proof_matrix.py"
        )


class TestEvidenceFilesExist:
    """Every evidence path referenced in the matrix must exist in the repo."""

    def test_all_evidence_paths_resolve(self, matrix: dict) -> None:
        missing: list[str] = []
        for row in matrix["rows"]:
            for ev in row["evidence"]:
                p = ROOT / ev["path"]
                if not p.exists():
                    missing.append(f"{row['id']}: {ev['path']}")
        assert not missing, "Missing evidence files:\n" + "\n".join(missing)


class TestEvidenceTestIds:
    """Every evidence item of kind 'test' must have a locator that exists as a
    test function or class in the referenced file."""

    def test_all_test_locators_exist(self, matrix: dict) -> None:
        missing: list[str] = []
        for row in matrix["rows"]:
            for ev in row["evidence"]:
                if ev["kind"] != "test":
                    continue
                path = ROOT / ev["path"]
                if not path.is_file():
                    # Caught by TestEvidenceFilesExist
                    continue
                locator = ev["locator"]
                # Only verify locators that look like Python test identifiers
                # (test_foo or TestFoo). Descriptive locators are documentation.
                if not (locator.startswith("test_") or locator.startswith("Test")):
                    continue
                content = path.read_text(encoding="utf-8")
                if locator not in content:
                    missing.append(f"{row['id']}: {ev['path']}::{locator}")
        assert not missing, "Test locators not found in source:\n" + "\n".join(missing)


class TestMatrixStructure:
    """Basic structural checks on the JSON source."""

    def test_ids_are_unique(self, matrix: dict) -> None:
        ids = [r["id"] for r in matrix["rows"]]
        assert len(ids) == len(set(ids)), "Duplicate row IDs"

    def test_every_row_has_evidence(self, matrix: dict) -> None:
        empty = [r["id"] for r in matrix["rows"] if not r.get("evidence")]
        assert not empty, f"Rows without evidence: {empty}"

    def test_every_row_has_verification_command(self, matrix: dict) -> None:
        empty = [r["id"] for r in matrix["rows"] if not r.get("verification", "").strip()]
        assert not empty, f"Rows without verification command: {empty}"


class TestCheckGateDetectsStaleness:
    """Prove the --check gate exits 1 on stale content and 0 on fresh."""

    def test_check_exits_0_after_regeneration(self, tmp_path: Path) -> None:
        """After generating from the real JSON, --check must pass."""
        md_out = tmp_path / "PROOF_MATRIX.md"
        # Generate into tmp
        result = subprocess.run(
            [sys.executable, str(GENERATOR), "--matrix", str(MATRIX_JSON), "--output", str(md_out)],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
        # Now --check against that same file
        result = subprocess.run(
            [
                sys.executable,
                str(GENERATOR),
                "--check",
                "--matrix",
                str(MATRIX_JSON),
                "--output",
                str(md_out),
            ],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr

    def test_check_exits_1_when_json_mutated(self, tmp_path: Path) -> None:
        """Editing the JSON source makes --check detect the mismatch."""
        # Copy JSON and generate md from it
        json_copy = tmp_path / "matrix.json"
        md_out = tmp_path / "PROOF_MATRIX.md"
        original = json.loads(MATRIX_JSON.read_text(encoding="utf-8"))
        json_copy.write_text(json.dumps(original, indent=2), encoding="utf-8")

        subprocess.run(
            [sys.executable, str(GENERATOR), "--matrix", str(json_copy), "--output", str(md_out)],
            capture_output=True,
            text=True,
        )
        assert md_out.exists()

        # Mutate the JSON copy — change the first row's area
        original["rows"][0]["area"] = "MUTATED_AREA_FOR_TEST"
        json_copy.write_text(json.dumps(original, indent=2), encoding="utf-8")

        # --check must now fail
        result = subprocess.run(
            [
                sys.executable,
                str(GENERATOR),
                "--check",
                "--matrix",
                str(json_copy),
                "--output",
                str(md_out),
            ],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 1, (
            f"Expected exit 1 for stale doc, got {result.returncode}\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
        assert "stale" in result.stderr.lower() or "---" in result.stderr

    def test_check_exits_1_when_markdown_hand_edited(self, tmp_path: Path) -> None:
        """Hand-editing the markdown makes --check detect the mismatch."""
        md_out = tmp_path / "PROOF_MATRIX.md"
        subprocess.run(
            [sys.executable, str(GENERATOR), "--matrix", str(MATRIX_JSON), "--output", str(md_out)],
            capture_output=True,
            text=True,
        )
        # Hand-edit the markdown
        text = md_out.read_text(encoding="utf-8")
        md_out.write_text(text + "\n<!-- hand edit -->\n", encoding="utf-8")

        result = subprocess.run(
            [
                sys.executable,
                str(GENERATOR),
                "--check",
                "--matrix",
                str(MATRIX_JSON),
                "--output",
                str(md_out),
            ],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 1, (
            f"Expected exit 1 for hand-edited doc, got {result.returncode}\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
