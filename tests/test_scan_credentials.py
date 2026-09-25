"""Test the committed-credential scanner."""

import shutil
import subprocess
import sys
from pathlib import Path


def _setup_test_repo(repo: Path):
    """Set up a test git repo with the scanner script."""
    (repo / ".git").mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"], cwd=repo, check=True, capture_output=True
    )
    scripts_dir = repo / "scripts"
    scripts_dir.mkdir()
    shutil.copy(
        "scripts/scan_committed_credentials.py", scripts_dir / "scan_committed_credentials.py"
    )


def test_scan_blocks_real_credentials(tmp_path):
    """Real .pem/.key files should be blocked."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _setup_test_repo(repo)

    # Add a real credential file
    (repo / "secret.key").write_text("ssh-rsa AAAAB3...")
    subprocess.run(["git", "add", "secret.key"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "test"], cwd=repo, check=True, capture_output=True)

    # Run scanner
    result = subprocess.run(
        [sys.executable, "scripts/scan_committed_credentials.py"],
        cwd=repo,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0, "Scanner should fail on real credentials"
    assert "secret.key" in result.stdout or "secret.key" in result.stderr
    assert "RESULT: FAIL" in result.stdout


def test_scan_allows_empty_template(tmp_path):
    """Empty .env.example template should be allowed."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _setup_test_repo(repo)

    # Add a safe template
    (repo / ".env.example").write_text("""# Template
API_KEY=""
SECRET=""
""")
    subprocess.run(["git", "add", ".env.example"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "test"], cwd=repo, check=True, capture_output=True)

    result = subprocess.run(
        [sys.executable, "scripts/scan_committed_credentials.py"],
        cwd=repo,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, "Scanner should allow empty template"
    assert "RESULT: PASS" in result.stdout


def test_scan_blocks_template_with_real_values(tmp_path):
    """Template with non-empty values should be blocked."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _setup_test_repo(repo)

    # Add template with a real value
    (repo / ".env.example").write_text("""# Template
API_KEY="sk-1234567890abcdef"
SECRET=""
""")
    subprocess.run(["git", "add", ".env.example"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "test"], cwd=repo, check=True, capture_output=True)

    result = subprocess.run(
        [sys.executable, "scripts/scan_committed_credentials.py"],
        cwd=repo,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0, "Scanner should block template with real values"
    assert "RESULT: FAIL" in result.stdout
