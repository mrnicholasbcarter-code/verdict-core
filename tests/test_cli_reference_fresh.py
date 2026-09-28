"""Test that CLI reference docs are fresh (regenerated from current registry)."""

from __future__ import annotations

import sys
from pathlib import Path


def _get_python_executable() -> str:
    """Get the path to the current Python executable."""
    return sys.executable


def test_cli_reference_md_fresh() -> None:
    """CLI_REFERENCE.md must match current regeneration from registry."""
    import subprocess

    result = subprocess.run(
        [_get_python_executable(), "scripts/gen_cli_reference.py"],
        capture_output=True,
        text=True,
        cwd=Path(__file__).parent.parent,
    )
    assert result.returncode == 0, f"gen_cli_reference.py failed: {result.stderr}"

    expected = Path("docs/cli/CLI_REFERENCE.md").read_text()
    # Re-generate and compare
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from scripts.gen_cli_reference import generate

    regenerated = generate() + "\n"
    assert regenerated == expected, (
        "docs/cli/CLI_REFERENCE.md is stale. Run: python scripts/gen_cli_reference.py"
    )


def test_parity_matrix_md_fresh() -> None:
    """PARITY_MATRIX.md must match current regeneration from registry."""
    import subprocess

    result = subprocess.run(
        [_get_python_executable(), "scripts/gen_parity_matrix.py"],
        capture_output=True,
        text=True,
        cwd=Path(__file__).parent.parent,
    )
    assert result.returncode == 0, f"gen_parity_matrix.py failed: {result.stderr}"

    expected = Path("docs/cli/PARITY_MATRIX.md").read_text()
    # Re-generate and compare
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from scripts.gen_parity_matrix import generate

    regenerated = generate() + "\n"
    assert regenerated == expected, (
        "docs/cli/PARITY_MATRIX.md is stale. Run: python scripts/gen_parity_matrix.py"
    )
