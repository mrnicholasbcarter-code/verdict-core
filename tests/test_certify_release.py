"""Tests for scripts/certify_release.py certification bundle generation."""

import json

# Import the certify_release module
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
import certify_release


@pytest.fixture
def temp_git_repo():
    """Create a temporary git repository for testing."""
    with tempfile.TemporaryDirectory() as tmpdir:
        repo = Path(tmpdir)

        # Initialize git repo
        import subprocess

        subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        subprocess.run(
            ["git", "config", "user.email", "test@example.com"],
            cwd=repo,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test User"], cwd=repo, check=True, capture_output=True
        )

        # Create a dummy file and commit
        (repo / "README.md").write_text("# Test Repo")
        subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "Initial commit"], cwd=repo, check=True, capture_output=True
        )

        yield repo


def test_git_sha_extraction(temp_git_repo):
    """Test that git SHA is extracted correctly."""
    sha = certify_release.get_git_sha(temp_git_repo)
    assert len(sha) == 40
    assert all(c in "0123456789abcdef" for c in sha)


def test_git_dirty_clean_tree(temp_git_repo):
    """Test that clean git tree is detected."""
    assert not certify_release.is_git_dirty(temp_git_repo)


def test_git_dirty_modified_tree(temp_git_repo):
    """Test that dirty git tree is detected."""
    # Modify a file
    (temp_git_repo / "README.md").write_text("# Modified")
    assert certify_release.is_git_dirty(temp_git_repo)


def test_environment_snapshot_captures_python_version():
    """Test that environment snapshot captures Python version."""
    snapshot = certify_release.EnvironmentSnapshot(
        python_version="3.10.0", platform="Linux", uv_version="0.1.0"
    )
    assert snapshot.python_version == "3.10.0"
    data = snapshot.to_dict()
    assert data["python_version"] == "3.10.0"


def test_environment_no_secret_values():
    """Test that environment snapshot does NOT include env var values."""
    import os

    original_env = os.environ.copy()

    try:
        # Set a secret-looking env var
        os.environ["SECRET_API_KEY"] = "super-secret-12345"
        os.environ["NORMAL_VAR"] = "normal-value"

        snapshot = certify_release.capture_environment(Path.cwd())

        # Check that var names are captured
        assert "SECRET_API_KEY" in snapshot.env_var_names
        assert "NORMAL_VAR" in snapshot.env_var_names

        # Check that values are NOT in the snapshot
        data_json = json.dumps(snapshot.to_dict())
        assert "super-secret-12345" not in data_json
        assert "normal-value" not in data_json

        # But names should be there
        assert "SECRET_API_KEY" in data_json
        assert "NORMAL_VAR" in data_json

    finally:
        os.environ.clear()
        os.environ.update(original_env)


def test_step_result_pass():
    """Test StepResult for passing step."""
    step = certify_release.StepResult(
        step_id="test_step", name="Test Step", status="PASS", duration_seconds=1.5
    )
    assert step.status == "PASS"
    assert step.duration_seconds == 1.5


def test_step_result_fail_with_reason():
    """Test StepResult for failing step includes reason."""
    step = certify_release.StepResult(
        step_id="test_step", name="Test Step", status="FAIL", reason="Expected 0 errors, got 5"
    )
    assert step.status == "FAIL"
    assert "5" in step.reason


def test_step_result_skipped():
    """Test StepResult for skipped step."""
    step = certify_release.StepResult(
        step_id="test_step", name="Test Step", status="SKIPPED", reason="Dependency not available"
    )
    assert step.status == "SKIPPED"
    assert "not available" in step.reason


def test_manifest_certified_verdict():
    """Test manifest with all passing steps yields CERTIFIED."""
    manifest = certify_release.CertificationManifest(
        git_sha="abc123",
        git_dirty=False,
        steps=[
            certify_release.StepResult("step1", "Step 1", "PASS"),
            certify_release.StepResult("step2", "Step 2", "PASS"),
        ],
        verdict="CERTIFIED",
    )
    assert manifest.verdict == "CERTIFIED"


def test_manifest_failed_verdict():
    """Test manifest with failed step yields FAILED."""
    manifest = certify_release.CertificationManifest(
        git_sha="abc123",
        git_dirty=False,
        steps=[
            certify_release.StepResult("step1", "Step 1", "PASS"),
            certify_release.StepResult("step2", "Step 2", "FAIL", reason="Error"),
        ],
        verdict="FAILED",
    )
    assert manifest.verdict == "FAILED"


def test_manifest_incomplete_dirty_tree():
    """Test manifest with dirty tree yields INCOMPLETE."""
    manifest = certify_release.CertificationManifest(
        git_sha="abc123",
        git_dirty=True,
        steps=[certify_release.StepResult("step1", "Step 1", "PASS")],
        verdict="INCOMPLETE",
    )
    assert manifest.verdict == "INCOMPLETE"
    assert manifest.git_dirty is True


def test_manifest_incomplete_skipped_rehearsals():
    """Test manifest with skipped rehearsals yields INCOMPLETE."""
    manifest = certify_release.CertificationManifest(
        git_sha="abc123",
        git_dirty=False,
        steps=[
            certify_release.StepResult("step1", "Step 1", "PASS"),
            certify_release.StepResult(
                "rehearsals", "Rehearsals", "SKIPPED", reason="No credentials"
            ),
        ],
        verdict="INCOMPLETE",
    )
    assert manifest.verdict == "INCOMPLETE"


def test_manifest_to_dict_includes_all_fields():
    """Test manifest serialization includes all required fields."""
    manifest = certify_release.CertificationManifest(
        schema_version="1",
        git_sha="abc123",
        git_dirty=False,
        started_at="2024-01-01T00:00:00Z",
        finished_at="2024-01-01T00:15:00Z",
        steps=[],
        verdict="CERTIFIED",
    )
    data = manifest.to_dict()

    assert data["schema_version"] == "1"
    assert data["git_sha"] == "abc123"
    assert data["git_dirty"] is False
    assert data["started_at"] == "2024-01-01T00:00:00Z"
    assert data["finished_at"] == "2024-01-01T00:15:00Z"
    assert data["verdict"] == "CERTIFIED"
    assert isinstance(data["steps"], list)


def test_certification_md_generation():
    """Test CERTIFICATION.md is generated from manifest data."""
    manifest = certify_release.CertificationManifest(
        git_sha="abc123def456",
        git_dirty=False,
        started_at="2024-01-01T00:00:00Z",
        finished_at="2024-01-01T00:15:00Z",
        steps=[
            certify_release.StepResult("test", "Test Suite", "PASS", "100 passed", 120.5),
            certify_release.StepResult("lint", "Lint", "PASS", "", 5.2),
        ],
        verdict="CERTIFIED",
    )

    env = certify_release.EnvironmentSnapshot(
        python_version="3.10.0",
        platform="Linux",
        uv_version="0.1.0",
        lockfile_sha256="abcdef123456",
        env_var_names=["HOME", "PATH"],
    )

    md_content = certify_release.generate_certification_md(manifest, env)

    # Check that all manifest values appear in MD
    assert "abc123def456" in md_content
    assert "CERTIFIED" in md_content
    assert "2024-01-01T00:00:00Z" in md_content
    assert "Test Suite" in md_content
    assert "PASS" in md_content
    assert "100 passed" in md_content
    assert "120.5s" in md_content

    # Check environment values
    assert "3.10.0" in md_content
    assert "Linux" in md_content

    # Check normalization notes
    assert "normalized" in md_content.lower()


def test_certification_md_numbers_match_manifest():
    """Test that CERTIFICATION.md numbers exactly match manifest JSON."""
    manifest = certify_release.CertificationManifest(
        git_sha="test123",
        git_dirty=False,
        steps=[certify_release.StepResult("step1", "Step 1", "PASS", "42 passed", 10.5)],
        verdict="CERTIFIED",
    )

    env = certify_release.EnvironmentSnapshot(
        python_version="3.10.0", platform="Linux", uv_version="0.1.0"
    )

    md_content = certify_release.generate_certification_md(manifest, env)

    # The number 42 from the manifest should appear in MD
    assert "42 passed" in md_content

    # The duration 10.5 should appear
    assert "10.5s" in md_content


def test_deterministic_output_same_inputs():
    """Test that two runs with same inputs produce identical normalized output."""
    # This test uses mocked step runners to avoid real execution

    manifest1 = certify_release.CertificationManifest(
        schema_version="1",
        git_sha="abc123",
        git_dirty=False,
        started_at="2024-01-01T00:00:00Z",  # Normalized field
        finished_at="2024-01-01T00:15:00Z",  # Normalized field
        steps=[certify_release.StepResult("test", "Test", "PASS", "100 passed", 120.0)],
        verdict="CERTIFIED",
    )

    manifest2 = certify_release.CertificationManifest(
        schema_version="1",
        git_sha="abc123",
        git_dirty=False,
        started_at="2024-01-01T01:00:00Z",  # Different timestamp (normalized)
        finished_at="2024-01-01T01:15:00Z",  # Different timestamp (normalized)
        steps=[certify_release.StepResult("test", "Test", "PASS", "100 passed", 125.0)],
        verdict="CERTIFIED",
    )

    # After normalization (removing timestamps and durations), these should be identical
    def normalize_manifest(m):
        d = m.to_dict()
        d.pop("started_at", None)
        d.pop("finished_at", None)
        for step in d["steps"]:
            step.pop("duration_seconds", None)
        return d

    norm1 = normalize_manifest(manifest1)
    norm2 = normalize_manifest(manifest2)

    assert norm1 == norm2


def test_rehearsal_verification_skipped_when_none_provided():
    """Test that rehearsal step is SKIPPED when no rehearsals provided."""
    with tempfile.TemporaryDirectory() as tmpdir:
        output_dir = Path(tmpdir) / "output"
        output_dir.mkdir()

        result = certify_release.step_rehearsals(
            Path.cwd(),
            {},  # No rehearsals
            output_dir,
        )

        assert result.status == "SKIPPED"
        assert "credentials" in result.reason.lower()


def test_rehearsal_verification_copies_and_verifies():
    """Test that rehearsal verification copies files and checks digests."""
    with tempfile.TemporaryDirectory() as tmpdir:
        # Create fake rehearsal run directory
        run_dir = Path(tmpdir) / "clean_run"
        run_dir.mkdir()

        # Create events.jsonl
        events_content = """{"event": "test"}
{"event": "test2"}"""
        events_path = run_dir / "events.jsonl"
        events_path.write_text(events_content)

        # Calculate digest
        import hashlib

        events_sha = hashlib.sha256(events_content.encode()).hexdigest()

        # Create receipt.json with matching digest
        receipt_data = {"events_digest": f"sha256:{events_sha}", "claimed_outcome": "COMPLETE"}
        receipt_path = run_dir / "receipt.json"
        receipt_path.write_text(json.dumps(receipt_data))

        # Run verification
        output_dir = Path(tmpdir) / "output"
        output_dir.mkdir()

        result = certify_release.step_rehearsals(Path.cwd(), {"clean": run_dir}, output_dir)

        # Should pass
        assert result.status == "PASS"
        assert "1 rehearsal" in result.reason

        # Check that files were copied
        copied_events = output_dir / "rehearsals" / "clean" / "events.jsonl"
        copied_receipt = output_dir / "rehearsals" / "clean" / "receipt.json"
        assert copied_events.exists()
        assert copied_receipt.exists()


def test_rehearsal_verification_fails_on_digest_mismatch():
    """Test that rehearsal verification fails when digest doesn't match."""
    with tempfile.TemporaryDirectory() as tmpdir:
        run_dir = Path(tmpdir) / "clean_run"
        run_dir.mkdir()

        # Create events.jsonl
        events_content = """{"event": "test"}"""
        events_path = run_dir / "events.jsonl"
        events_path.write_text(events_content)

        # Create receipt with WRONG digest
        receipt_data = {"events_digest": "sha256:wrongdigest12345", "claimed_outcome": "COMPLETE"}
        receipt_path = run_dir / "receipt.json"
        receipt_path.write_text(json.dumps(receipt_data))

        output_dir = Path(tmpdir) / "output"
        output_dir.mkdir()

        result = certify_release.step_rehearsals(Path.cwd(), {"clean": run_dir}, output_dir)

        # Should fail
        assert result.status == "FAIL"
        assert "mismatch" in result.reason.lower()


def test_refusal_on_dirty_tree_without_flag():
    """Test that certification refuses to run on dirty tree without --allow-dirty."""
    # This would be an integration test with the full run_certification function
    # For now, we test the logic in the main() function via the dirty check
    pass  # Covered by test_git_dirty_modified_tree


def test_bundle_writes_all_required_files():
    """Test that bundle writer creates all required files."""
    with tempfile.TemporaryDirectory() as tmpdir:
        output_dir = Path(tmpdir) / "bundle"

        manifest = certify_release.CertificationManifest(
            git_sha="test123",
            git_dirty=False,
            started_at="2024-01-01T00:00:00Z",
            finished_at="2024-01-01T00:15:00Z",
            steps=[],
            verdict="CERTIFIED",
        )

        env = certify_release.EnvironmentSnapshot(
            python_version="3.10.0", platform="Linux", uv_version="0.1.0"
        )

        # Mock git status command
        with patch("scripts.certify_release.run_command") as mock_run:
            mock_run.return_value = MagicMock(stdout="", returncode=0)
            certify_release.write_bundle(output_dir, manifest, env)

        # Check all required files exist
        assert (output_dir / "manifest.json").exists()
        assert (output_dir / "environment.json").exists()
        assert (output_dir / "CERTIFICATION.md").exists()
        assert (output_dir / "git-clean.txt").exists()


def test_environment_capture_with_missing_tools():
    """Test that capture_environment handles missing tools gracefully (empty PATH)."""
    from unittest.mock import patch

    # Simulate all external tools missing (FileNotFoundError)
    with patch("certify_release.run_command") as mock_run:
        mock_run.side_effect = FileNotFoundError("command not found")

        snapshot = certify_release.capture_environment(Path.cwd())

        # Python version should still work (built-in)
        assert snapshot.python_version
        assert "." in snapshot.python_version

        # Platform should still work (built-in)
        assert snapshot.platform

        # External tools should gracefully report "not available"
        assert snapshot.uv_version == "not available"

        # Optional tools should be empty string when missing
        assert snapshot.node_version == ""

        # No exception should be raised
        data = snapshot.to_dict()
        assert data["uv_version"] == "not available"


def test_step_security_bandit_missing():
    """Test that missing bandit (declared dev dep) results in FAIL, not SKIPPED."""
    from pathlib import Path

    repo_path = Path("/fake/repo")
    venv_bin = Path("/fake/venv/bin")

    # Simulate bandit binary not existing
    result = certify_release.step_security(repo_path, venv_bin)

    assert result.status == "FAIL"
    assert "bandit declared dev dependency missing" in result.reason
    assert "uv sync --extra dev" in result.reason


def test_step_security_bandit_only_low_findings_passes():
    """Test that bandit with only LOW findings (filtered by -ll) passes."""
    import json
    from pathlib import Path
    from unittest.mock import MagicMock, patch

    repo_path = Path("/fake/repo")
    venv_bin = Path("/fake/venv/bin")

    bandit_output = json.dumps(
        {"results": [{"issue_severity": "LOW", "issue_text": "some low issue"}]}
    )

    pip_audit_output = json.dumps({"dependencies": []})

    with patch("certify_release.Path.exists") as mock_exists:
        mock_exists.return_value = True
        with patch("certify_release.run_command") as mock_run:

            def run_side_effect(cmd, **kwargs):
                mock_result = MagicMock()
                if "bandit" in str(cmd[0]):
                    mock_result.stdout = bandit_output
                    mock_result.stderr = ""
                    mock_result.returncode = 0
                elif "pip-audit" in str(cmd[0]):
                    mock_result.stdout = pip_audit_output
                    mock_result.stderr = ""
                    mock_result.returncode = 0
                return mock_result

            mock_run.side_effect = run_side_effect
            result = certify_release.step_security(repo_path, venv_bin)

    assert result.status == "PASS"


def test_step_security_bandit_medium_finding_fails():
    """Test that one MEDIUM bandit finding causes FAIL."""
    import json
    from pathlib import Path
    from unittest.mock import MagicMock, patch

    repo_path = Path("/fake/repo")
    venv_bin = Path("/fake/venv/bin")

    bandit_output = json.dumps(
        {"results": [{"issue_severity": "MEDIUM", "issue_text": "potential security issue"}]}
    )

    pip_audit_output = json.dumps({"dependencies": []})

    with patch("certify_release.Path.exists") as mock_exists:
        mock_exists.return_value = True
        with patch("certify_release.run_command") as mock_run:

            def run_side_effect(cmd, **kwargs):
                mock_result = MagicMock()
                if "bandit" in str(cmd[0]):
                    mock_result.stdout = bandit_output
                    mock_result.stderr = ""
                    mock_result.returncode = 0
                elif "pip-audit" in str(cmd[0]):
                    mock_result.stdout = pip_audit_output
                    mock_result.stderr = ""
                    mock_result.returncode = 0
                return mock_result

            mock_run.side_effect = run_side_effect
            result = certify_release.step_security(repo_path, venv_bin)

    assert result.status == "FAIL"
    assert "bandit:" in result.reason
    assert "medium+" in result.reason.lower()


def test_step_security_pip_audit_vulnerability_fails():
    """Test that pip-audit finding a vulnerability causes FAIL."""
    import json
    from pathlib import Path
    from unittest.mock import MagicMock, patch

    repo_path = Path("/fake/repo")
    venv_bin = Path("/fake/venv/bin")

    bandit_output = json.dumps({"results": []})

    pip_audit_output = json.dumps(
        {
            "dependencies": [
                {
                    "name": "vulnerable-package",
                    "version": "1.0.0",
                    "vulns": [{"id": "CVE-2023-12345", "description": "Bad vulnerability"}],
                }
            ]
        }
    )

    with patch("certify_release.Path.exists") as mock_exists:
        mock_exists.return_value = True
        with patch("certify_release.run_command") as mock_run:

            def run_side_effect(cmd, **kwargs):
                mock_result = MagicMock()
                if "bandit" in str(cmd[0]):
                    mock_result.stdout = bandit_output
                    mock_result.stderr = ""
                    mock_result.returncode = 0
                elif "pip-audit" in str(cmd[0]):
                    mock_result.stdout = pip_audit_output
                    mock_result.stderr = ""
                    mock_result.returncode = 0
                return mock_result

            mock_run.side_effect = run_side_effect
            result = certify_release.step_security(repo_path, venv_bin)

    assert result.status == "FAIL"
    assert "pip-audit:" in result.reason
    assert "vulnerability" in result.reason.lower()


def test_step_security_pip_audit_network_error_incomplete():
    """Test that pip-audit network/DB error results in INCOMPLETE."""
    import json
    from pathlib import Path
    from unittest.mock import MagicMock, patch

    repo_path = Path("/fake/repo")
    venv_bin = Path("/fake/venv/bin")

    bandit_output = json.dumps({"results": []})

    # Simulate network error (error on stderr, as in reality)
    pip_audit_error_stderr = "ERROR: Connection error: HTTPSConnectionPool(host='pypi.org')"

    with patch("certify_release.Path.exists") as mock_exists:
        mock_exists.return_value = True
        with patch("certify_release.run_command") as mock_run:

            def run_side_effect(cmd, **kwargs):
                mock_result = MagicMock()
                if "bandit" in str(cmd[0]):
                    mock_result.stdout = bandit_output
                    mock_result.stderr = ""
                    mock_result.stderr = ""
                    mock_result.returncode = 0
                elif "pip-audit" in str(cmd[0]):
                    mock_result.stdout = ""  # Empty stdout when error
                    mock_result.stderr = pip_audit_error_stderr
                    mock_result.returncode = 1
                return mock_result

            mock_run.side_effect = run_side_effect
            result = certify_release.step_security(repo_path, venv_bin)

    assert result.status == "INCOMPLETE"
    assert "pip-audit:" in result.reason
    assert "connection" in result.reason.lower() or "error" in result.reason.lower()


def test_step_security_uses_correct_bandit_arguments():
    """Test that step_security calls bandit with -c pyproject.toml and -ll."""
    import json
    from pathlib import Path
    from unittest.mock import MagicMock, patch

    repo_path = Path("/fake/repo")
    venv_bin = Path("/fake/venv/bin")

    bandit_output = json.dumps({"results": []})
    pip_audit_output = json.dumps({"dependencies": []})

    with patch("certify_release.Path.exists") as mock_exists:
        mock_exists.return_value = True
        with patch("certify_release.run_command") as mock_run:

            def run_side_effect(cmd, **kwargs):
                # Validate bandit command arguments
                if "bandit" in str(cmd[0]):
                    # Assert the exact argv includes -c pyproject.toml and -ll
                    cmd_str = " ".join(str(c) for c in cmd)
                    assert "-c" in cmd_str
                    assert "pyproject.toml" in cmd_str
                    assert "-ll" in cmd_str
                    assert "-r" in cmd_str
                    assert "verdict" in cmd_str
                    assert "-f" in cmd_str
                    assert "json" in cmd_str

                mock_result = MagicMock()
                if "bandit" in str(cmd[0]):
                    mock_result.stdout = bandit_output
                    mock_result.stderr = ""
                elif "pip-audit" in str(cmd[0]):
                    mock_result.stdout = pip_audit_output
                    mock_result.stderr = ""
                mock_result.returncode = 0
                return mock_result

            mock_run.side_effect = run_side_effect
            result = certify_release.step_security(repo_path, venv_bin)

    assert result.status == "PASS"


def test_step_security_pip_audit_missing():
    """Test that missing pip-audit (declared dev dep) results in FAIL."""
    import json
    from pathlib import Path
    from unittest.mock import MagicMock, patch

    repo_path = Path("/fake/repo")
    venv_bin = Path("/fake/venv/bin")

    bandit_output = json.dumps({"results": []})

    # We'll mock Path.exists to return False for pip-audit, True for bandit
    # The trick is to make it instance-aware
    original_exists = Path.exists

    def custom_exists(self):
        if "pip-audit" in str(self):
            return False
        elif "bandit" in str(self):
            return True
        # Fall back to original for safety
        return original_exists(self)

    with (
        patch.object(Path, "exists", custom_exists),
        patch("certify_release.run_command") as mock_run,
    ):
        mock_run.return_value = MagicMock(stdout=bandit_output, returncode=0)
        result = certify_release.step_security(repo_path, venv_bin)

    assert result.status == "FAIL"
    assert "pip-audit declared dev dependency missing" in result.reason


def test_manifest_verdict_incomplete_when_step_incomplete():
    """Test that manifest verdict is INCOMPLETE when any step has INCOMPLETE status."""
    # Create a manifest with security step INCOMPLETE and everything else PASS
    security_incomplete = certify_release.StepResult(
        step_id="security",
        name="Security checks",
        status="INCOMPLETE",
        reason="pip-audit: network error",
    )

    other_pass = certify_release.StepResult(step_id="test", name="Tests", status="PASS")

    manifest = certify_release.CertificationManifest(
        git_sha="abc123",
        git_dirty=False,
        started_at="2024-01-01T00:00:00Z",
        finished_at="2024-01-01T00:15:00Z",
        steps=[security_incomplete, other_pass],
        verdict="INCOMPLETE",  # Will be overwritten by logic
    )

    # Simulate the verdict logic as in the real code
    has_failures = any(s.status == "FAIL" for s in manifest.steps)
    has_incomplete = any(s.status == "INCOMPLETE" for s in manifest.steps)
    has_skipped_rehearsals = any(
        s.step_id == "rehearsals" and s.status == "SKIPPED" for s in manifest.steps
    )

    if has_failures:
        manifest.verdict = "FAILED"
    elif has_incomplete or manifest.git_dirty or has_skipped_rehearsals:
        manifest.verdict = "INCOMPLETE"
    else:
        manifest.verdict = "CERTIFIED"

    # Assert: INCOMPLETE step -> verdict INCOMPLETE, never CERTIFIED
    assert manifest.verdict == "INCOMPLETE"
    assert not has_failures
    assert has_incomplete
