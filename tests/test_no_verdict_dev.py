"""
Test the CI lint rule that prevents reintroduction of verdict.dev references.

BOD-254: verdict.dev was replaced with github.com/mrnicholasbcarter-code/verdict-core
"""

import subprocess
import tempfile
from pathlib import Path


def test_no_verdict_dev_in_codebase():
    """Ensure verdict.dev does not appear anywhere in the codebase except the lint rule."""
    result = subprocess.run(
        ["git", "grep", "-n", "verdict.dev"],
        cwd=Path(__file__).parent.parent,
        capture_output=True,
        text=True,
    )

    # Filter out the lint.yml file which legitimately contains 'verdict.dev' in its error message
    lines = result.stdout.strip().split("\n") if result.stdout else []
    non_lint_matches = [line for line in lines if line and ".github/workflows/lint.yml" not in line]

    assert len(non_lint_matches) == 0, (
        "Found 'verdict.dev' references (BOD-254). "
        "Use github.com/mrnicholasbcarter-code/verdict-core instead:\n"
        + "\n".join(non_lint_matches)
    )


def test_lint_script_catches_verdict_dev():
    """Test that the CI lint step would catch a reintroduced verdict.dev."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmppath = Path(tmpdir)

        # Create a temporary git repo with a verdict.dev reference
        subprocess.run(["git", "init"], cwd=tmppath, check=True, capture_output=True)
        subprocess.run(
            ["git", "config", "user.email", "test@example.com"],
            cwd=tmppath,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test User"],
            cwd=tmppath,
            check=True,
            capture_output=True,
        )

        test_file = tmppath / "test.txt"
        test_file.write_text("Visit https://verdict.dev for more info\n")

        subprocess.run(["git", "add", "."], cwd=tmppath, check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "test"], cwd=tmppath, check=True, capture_output=True
        )

        # The lint command should find it
        result = subprocess.run(
            ["git", "grep", "-n", "verdict.dev"], cwd=tmppath, capture_output=True, text=True
        )

        assert result.returncode == 0, "Test harness: git grep should find verdict.dev in temp repo"
        assert "verdict.dev" in result.stdout
