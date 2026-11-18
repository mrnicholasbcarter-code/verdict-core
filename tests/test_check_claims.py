"""Tests for the claim inventory check script."""

import subprocess
import sys
from pathlib import Path


def test_check_claims_passes():
    """The check_claims script should pass on the current repo."""
    repo_root = Path(__file__).parent.parent
    script = repo_root / "scripts" / "check_claims.py"

    result = subprocess.run(
        [sys.executable, str(script)], cwd=repo_root, capture_output=True, text=True
    )

    # Print output for debugging
    if result.stdout:
        print(result.stdout)
    if result.stderr:
        print(result.stderr, file=sys.stderr)

    assert result.returncode == 0, f"check_claims.py failed with exit code {result.returncode}"


def test_check_claims_output_contains_summary():
    """The check_claims script should output a summary with claim counts."""
    repo_root = Path(__file__).parent.parent
    script = repo_root / "scripts" / "check_claims.py"

    result = subprocess.run(
        [sys.executable, str(script)], cwd=repo_root, capture_output=True, text=True
    )

    assert "Claim Count by Label" in result.stdout
    assert "Total claims:" in result.stdout
    assert "All checks passed!" in result.stdout
