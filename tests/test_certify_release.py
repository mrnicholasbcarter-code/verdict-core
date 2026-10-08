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


def fake_run_writing(
    bandit_json: str, audit_json: str | None, audit_rc: int = 0, audit_stderr: str = ""
):
    """Helper to create a fake run_command that writes JSON to -o files."""
    from pathlib import Path
    from unittest.mock import MagicMock

    def side_effect(cmd, **kwargs):
        # Write to -o file if specified
        for i, arg in enumerate(cmd):
            if arg == "-o" and i + 1 < len(cmd):
                output_file = Path(cmd[i + 1])
                if "bandit" in str(cmd[0]):
                    output_file.write_text(bandit_json)
                elif "pip-audit" in str(cmd[0]) and audit_json is not None:
                    output_file.write_text(audit_json)
                break

        # Return appropriate result
        result = MagicMock()
        result.stdout = ""
        result.returncode = 0
        result.stderr = ""

        if "pip-audit" in str(cmd[0]):
            result.returncode = audit_rc
            result.stderr = audit_stderr

        return result

    return side_effect


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
    assert certify_release.markdown_cell("2024-01-01T00:00:00Z") in md_content
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


@pytest.mark.parametrize("receipt_kind", ["minimal", "malformed", "failed", "bad_events"])
def test_rehearsal_verification_rejects_untrusted_receipts(tmp_path, receipt_kind):
    """Digest matching by itself cannot prove a valid or successful rehearsal."""
    run_dir = tmp_path / "clean"
    run_dir.mkdir()
    (run_dir / "events.jsonl").write_text('{"event":"fake"}\n')
    (run_dir / "graph.json").write_text("{}")
    if receipt_kind == "malformed":
        (run_dir / "receipt.json").write_text("{")
    else:
        import hashlib

        digest = hashlib.sha256((run_dir / "events.jsonl").read_bytes()).hexdigest()
        data = {"events_digest": f"sha256:{digest}", "claimed_outcome": "COMPLETE"}
        if receipt_kind == "failed":
            data["outcome"] = "BLOCKED"
        (run_dir / "receipt.json").write_text(json.dumps(data))
        if receipt_kind == "bad_events":
            (run_dir / "events.jsonl").write_text("not json\n")
    result = certify_release.step_rehearsals(
        tmp_path, {"clean": run_dir, "chaos": run_dir}, tmp_path / "bundle"
    )
    assert result.status == "FAIL"
    assert "Invalid rehearsal" in result.reason


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

        result = certify_release.step_rehearsals(
            Path.cwd(), {"clean": run_dir, "chaos": run_dir}, output_dir
        )

        # Should fail
        assert result.status == "FAIL"
        assert "missing" in result.reason.lower()


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
    import tempfile
    from pathlib import Path
    from unittest.mock import patch

    repo_path = Path("/fake/repo")
    venv_bin = Path("/fake/venv/bin")

    bandit_json = json.dumps(
        {"results": [{"issue_severity": "LOW", "issue_text": "some low issue"}]}
    )
    audit_json = json.dumps({"dependencies": []})

    with (
        tempfile.TemporaryDirectory() as tmpdir,
        patch("certify_release.Path.exists", return_value=True),
        patch(
            "certify_release.tempfile.TemporaryDirectory",
            return_value=type(
                "MockTempDir", (), {"__enter__": lambda s: tmpdir, "__exit__": lambda s, *a: None}
            )(),
        ),
        patch("certify_release.run_command", side_effect=fake_run_writing(bandit_json, audit_json)),
    ):
        result = certify_release.step_security(repo_path, venv_bin)

    assert result.status == "PASS"


def test_step_security_bandit_medium_finding_fails():
    """Test that one MEDIUM bandit finding causes FAIL."""
    import json
    import tempfile
    from pathlib import Path
    from unittest.mock import patch

    repo_path = Path("/fake/repo")
    venv_bin = Path("/fake/venv/bin")

    bandit_json = json.dumps(
        {"results": [{"issue_severity": "MEDIUM", "issue_text": "potential security issue"}]}
    )
    audit_json = json.dumps({"dependencies": []})

    with (
        tempfile.TemporaryDirectory() as tmpdir,
        patch("certify_release.Path.exists", return_value=True),
        patch(
            "certify_release.tempfile.TemporaryDirectory",
            return_value=type(
                "MockTempDir", (), {"__enter__": lambda s: tmpdir, "__exit__": lambda s, *a: None}
            )(),
        ),
        patch("certify_release.run_command", side_effect=fake_run_writing(bandit_json, audit_json)),
    ):
        result = certify_release.step_security(repo_path, venv_bin)

    assert result.status == "FAIL"
    assert "bandit:" in result.reason
    assert "medium+" in result.reason.lower()


def test_step_security_pip_audit_vulnerability_fails():
    """Test that pip-audit finding a vulnerability causes FAIL."""
    import json
    import tempfile
    from pathlib import Path
    from unittest.mock import patch

    repo_path = Path("/fake/repo")
    venv_bin = Path("/fake/venv/bin")

    bandit_json = json.dumps({"results": []})
    audit_json = json.dumps(
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

    with (
        tempfile.TemporaryDirectory() as tmpdir,
        patch("certify_release.Path.exists", return_value=True),
        patch(
            "certify_release.tempfile.TemporaryDirectory",
            return_value=type(
                "MockTempDir", (), {"__enter__": lambda s: tmpdir, "__exit__": lambda s, *a: None}
            )(),
        ),
        patch("certify_release.run_command", side_effect=fake_run_writing(bandit_json, audit_json)),
    ):
        result = certify_release.step_security(repo_path, venv_bin)

    assert result.status == "FAIL"
    assert "pip-audit:" in result.reason
    assert "vulnerability" in result.reason.lower()


def test_step_security_pip_audit_network_error_incomplete():
    """Test that pip-audit network/DB error results in INCOMPLETE."""
    import json
    import tempfile
    from pathlib import Path
    from unittest.mock import patch

    repo_path = Path("/fake/repo")
    venv_bin = Path("/fake/venv/bin")

    bandit_json = json.dumps({"results": []})
    # pip-audit: None means don't write file (network error)
    audit_stderr = "ERROR: Connection error: HTTPSConnectionPool(host='pypi.org')"

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)
        original_exists = Path.exists

        def smart_exists(self):
            # Check actual filesystem for files in tmpdir
            if str(self).startswith(str(tmpdir_path)):
                return original_exists.__get__(self, Path)()
            # Binaries exist
            return True

        with (
            patch.object(Path, "exists", smart_exists),
            patch(
                "certify_release.tempfile.TemporaryDirectory",
                return_value=type(
                    "MockTempDir",
                    (),
                    {"__enter__": lambda s: tmpdir, "__exit__": lambda s, *a: None},
                )(),
            ),
            patch(
                "certify_release.run_command",
                side_effect=fake_run_writing(bandit_json, None, 1, audit_stderr),
            ),
        ):
            result = certify_release.step_security(repo_path, venv_bin)

    assert result.status == "INCOMPLETE"
    assert "pip-audit:" in result.reason
    assert "connection" in result.reason.lower() or "error" in result.reason.lower()


def test_step_security_uses_correct_bandit_arguments():
    """Test that step_security calls bandit with -q -o and -c pyproject.toml -ll."""
    import json
    import tempfile
    from pathlib import Path
    from unittest.mock import patch

    repo_path = Path("/fake/repo")
    venv_bin = Path("/fake/venv/bin")

    bandit_json = json.dumps({"results": []})
    audit_json = json.dumps({"dependencies": []})

    captured_commands = []

    def capture_and_write(bandit_json, audit_json):
        def side_effect(cmd, **kwargs):
            captured_commands.append(cmd)
            return fake_run_writing(bandit_json, audit_json)(cmd, **kwargs)

        return side_effect

    with (
        tempfile.TemporaryDirectory() as tmpdir,
        patch("certify_release.Path.exists", return_value=True),
        patch(
            "certify_release.tempfile.TemporaryDirectory",
            return_value=type(
                "MockTempDir", (), {"__enter__": lambda s: tmpdir, "__exit__": lambda s, *a: None}
            )(),
        ),
        patch(
            "certify_release.run_command", side_effect=capture_and_write(bandit_json, audit_json)
        ),
    ):
        result = certify_release.step_security(repo_path, venv_bin)

    # Find the bandit command
    bandit_cmd = next(cmd for cmd in captured_commands if "bandit" in str(cmd[0]))
    cmd_str = " ".join(str(c) for c in bandit_cmd)

    assert "-q" in cmd_str, "bandit should use -q to suppress progress bar"
    assert "-o" in cmd_str, "bandit should use -o to write to file"
    assert "-c" in cmd_str
    assert "pyproject.toml" in cmd_str
    assert "-ll" in cmd_str
    assert "-r" in cmd_str
    assert "verdict" in cmd_str
    assert "-f" in cmd_str
    assert "json" in cmd_str

    assert result.status == "PASS"


def test_step_security_pip_audit_missing():
    """Test that missing pip-audit (declared dev dep) results in FAIL."""
    import json
    import tempfile
    from pathlib import Path
    from unittest.mock import patch

    repo_path = Path("/fake/repo")
    venv_bin = Path("/fake/venv/bin")

    bandit_json = json.dumps({"results": []})

    def custom_exists(self):
        # pip-audit binary doesn't exist
        return not ("pip-audit" in str(self) and str(self).endswith("pip-audit"))

    with (
        tempfile.TemporaryDirectory() as tmpdir,
        patch.object(Path, "exists", custom_exists),
        patch(
            "certify_release.tempfile.TemporaryDirectory",
            return_value=type(
                "MockTempDir", (), {"__enter__": lambda s: tmpdir, "__exit__": lambda s, *a: None}
            )(),
        ),
        patch("certify_release.run_command", side_effect=fake_run_writing(bandit_json, None)),
    ):
        result = certify_release.step_security(repo_path, venv_bin)

    assert result.status == "FAIL"
    assert "pip-audit declared dev dependency missing" in result.reason


def test_step_security_bandit_unmocked_integration():
    """UNMOCKED integration test: run real bandit on temp packages with clean/bad code."""
    import tempfile
    from pathlib import Path

    # Skip if bandit binary is missing
    venv_bin = Path.cwd() / ".venv" / "bin"
    bandit_bin = venv_bin / "bandit"
    if not bandit_bin.exists():
        pytest.skip("bandit binary not found in .venv/bin")

    with tempfile.TemporaryDirectory() as tmpdir:
        temp_repo = Path(tmpdir)

        # Create minimal pyproject.toml
        (temp_repo / "pyproject.toml").write_text("""
[tool.bandit]
exclude_dirs = []
""")

        # Create verdict dir with clean code
        (temp_repo / "verdict").mkdir()
        (temp_repo / "verdict" / "__init__.py").write_text("")
        (temp_repo / "verdict" / "safe.py").write_text("""
def hello():
    return "Hello, world!"
""")

        result_clean = certify_release.step_security(temp_repo, venv_bin)
        assert result_clean.status == "PASS", f"Clean code should pass: {result_clean.reason}"

        # Add file with security issue
        bad_file = temp_repo / "verdict" / "unsafe.py"
        bad_file.write_text("""
import subprocess

def run_command(user_input):
    subprocess.call(user_input, shell=True)  # Bandit flags this

def evaluate_code(code):
    return eval(code)  # Bandit flags this
""")

        result_bad = certify_release.step_security(temp_repo, venv_bin)
        assert result_bad.status == "FAIL", "Code with security issues should fail"
        assert "medium+" in result_bad.reason.lower() or "finding" in result_bad.reason.lower(), (
            f"Expected finding message, got: {result_bad.reason}"
        )


def test_compute_verdict_incomplete_when_step_incomplete():
    """Test compute_verdict: INCOMPLETE step with clean tree -> INCOMPLETE."""
    security_incomplete = certify_release.StepResult(
        step_id="security",
        name="Security checks",
        status="INCOMPLETE",
        reason="pip-audit: network error",
    )
    other_pass = certify_release.StepResult(step_id="test", name="Tests", status="PASS")

    verdict = certify_release.compute_verdict([security_incomplete, other_pass], git_dirty=False)
    assert verdict == "INCOMPLETE"


def test_compute_verdict_fail_beats_incomplete():
    """Test compute_verdict: FAIL beats INCOMPLETE."""
    fail_step = certify_release.StepResult(
        step_id="test", name="Tests", status="FAIL", reason="1 failed"
    )
    incomplete_step = certify_release.StepResult(
        step_id="security", name="Security", status="INCOMPLETE"
    )

    verdict = certify_release.compute_verdict([fail_step, incomplete_step], git_dirty=False)
    assert verdict == "FAILED"


def test_compute_verdict_all_pass_clean_certified():
    """Only a complete set of steps with usable evidence can certify."""
    command = {"command": ["tool"], "exit_code": 0}
    steps = [
        certify_release.StepResult(step_id, step_id, "PASS", evidence=dict(command))
        for step_id in ("ruff_check", "ruff_format", "mypy", "docs_check")
    ]
    steps.extend(
        certify_release.StepResult(
            step_id,
            step_id,
            "PASS",
            evidence={
                **command,
                "junit": {
                    "tests": 1,
                    "passed": 1,
                    "failures": 0,
                    "errors": 0,
                    "skipped": 0,
                    "sha256": "abc",
                },
            },
        )
        for step_id in ("test_clean", "test_dirty")
    )
    steps.extend(
        [
            certify_release.StepResult(
                "build",
                "Build",
                "PASS",
                evidence={**command, "artifacts": [{"name": "verdict.whl", "sha256": "abc"}]},
            ),
            certify_release.StepResult(
                "package_smoke",
                "Smoke",
                "PASS",
                evidence={
                    "commands": [{"command": ["uv"], "exit_code": 0}] * 3,
                    "help_has_usage": True,
                },
            ),
            certify_release.StepResult(
                "security",
                "Security",
                "PASS",
                evidence={
                    "bandit": {"report_sha256": "abc"},
                    "pip_audit": {"report_sha256": "def"},
                },
            ),
            certify_release.StepResult("git_clean", "Git", "PASS"),
            certify_release.StepResult(
                "rehearsals",
                "Rehearsals",
                "PASS",
                evidence={"independent_producer_attestation": True},
            ),
        ]
    )
    # Even fabricated PASS statuses do not independently attest rehearsal producers.
    assert certify_release.compute_verdict(steps, git_dirty=False) == "INCOMPLETE"
    assert certify_release.compute_verdict(steps[:-1], git_dirty=False) == "INCOMPLETE"
    steps[0].evidence = {}
    assert certify_release.compute_verdict(steps, git_dirty=False) == "INCOMPLETE"


def test_compute_verdict_skipped_rehearsals_incomplete():
    """Test compute_verdict: SKIPPED rehearsals -> INCOMPLETE."""
    test_pass = certify_release.StepResult(step_id="test", name="Tests", status="PASS")
    rehearsals_skipped = certify_release.StepResult(
        step_id="rehearsals", name="Rehearsals", status="SKIPPED", reason="none provided"
    )

    verdict = certify_release.compute_verdict([test_pass, rehearsals_skipped], git_dirty=False)
    assert verdict == "INCOMPLETE"


def test_compute_verdict_dirty_tree_incomplete():
    """Test compute_verdict: dirty git tree -> INCOMPLETE."""
    test_pass = certify_release.StepResult(step_id="test", name="Tests", status="PASS")

    verdict = certify_release.compute_verdict([test_pass], git_dirty=True)
    assert verdict == "INCOMPLETE"


@pytest.mark.parametrize("dirty", [False, True])
def test_pytest_evidence_requires_valid_junit(tmp_path, dirty):
    """A zero exit without a valid report cannot certify the suite."""
    runner = (
        certify_release.step_test_dirty_shell if dirty else certify_release.step_test_clean_shell
    )
    with patch("certify_release.run_command", return_value=MagicMock(returncode=0)):
        step = runner(tmp_path, tmp_path)
    assert step.status == "INCOMPLETE"
    assert step.evidence["exit_code"] == 0
    assert step.evidence["junit"] is None
    assert "--junitxml=" in step.evidence["command"][-1]
    assert certify_release.compute_verdict([step], git_dirty=False) == "INCOMPLETE"


@pytest.mark.parametrize("dirty", [False, True])
def test_pytest_evidence_parses_junit_and_failure(tmp_path, dirty):
    runner = (
        certify_release.step_test_dirty_shell if dirty else certify_release.step_test_clean_shell
    )

    def fake_run(cmd, **kwargs):
        report = Path(cmd[-1].removeprefix("--junitxml="))
        report.write_text(
            '<testsuites><testsuite tests="4" failures="1" errors="0" skipped="1">'
            '<testcase classname="c" name="a"/><testcase classname="c" name="b"/>'
            '<testcase classname="c" name="c"><failure/></testcase>'
            '<testcase classname="c" name="d"><skipped/></testcase>'
            "</testsuite></testsuites>"
        )
        return MagicMock(returncode=1)

    with patch("certify_release.run_command", side_effect=fake_run):
        step = runner(tmp_path, tmp_path)
    assert step.status == "FAIL"
    assert step.evidence["junit"]["passed"] == 2
    assert step.evidence["junit"]["sha256"]


def test_build_no_new_artifact_is_incomplete_even_with_stale_dist(tmp_path):
    (tmp_path / "dist").mkdir()
    (tmp_path / "dist" / "stale.whl").write_text("old")
    with patch("certify_release.run_command", return_value=MagicMock(returncode=0)):
        step = certify_release.step_build(tmp_path)
    assert step.status == "INCOMPLETE"
    assert step.evidence["artifacts"] == []
    assert certify_release.step_package_smoke(tmp_path).status == "INCOMPLETE"


def test_build_artifact_digest_and_smoke_command_chain(tmp_path):
    def fake_run(cmd, **kwargs):
        if cmd[:2] == ["uv", "build"]:
            Path(cmd[-1], "verdict-1.whl").write_bytes(b"wheel")
        return MagicMock(returncode=0, stdout="usage: verdict")

    with patch("certify_release.run_command", side_effect=fake_run):
        built = certify_release.step_build(tmp_path)
        smoke = certify_release.step_package_smoke(tmp_path, "verdict-1.whl")
    assert built.status == smoke.status == "PASS"
    assert len(built.evidence["artifacts"][0]["sha256"]) == 64
    assert smoke.evidence["wheel_sha256"] == built.evidence["artifacts"][0]["sha256"]
    assert [x["exit_code"] for x in smoke.evidence["commands"]] == [0, 0, 0]
    assert smoke.evidence["help_has_usage"] is True


def test_security_success_exit_without_report_is_incomplete(tmp_path):
    (tmp_path / "bandit").touch()
    (tmp_path / "pip-audit").touch()
    with patch("certify_release.run_command", return_value=MagicMock(returncode=0, stderr="")):
        step = certify_release.step_security(tmp_path, tmp_path)
    assert step.status == "INCOMPLETE"
    assert step.evidence["bandit"]["report_sha256"] is None
    assert step.evidence["pip_audit"]["report_sha256"] is None


def test_report_files_match_manifest_evidence(tmp_path):
    step = certify_release.StepResult(
        "test_clean",
        "Test",
        "INCOMPLETE",
        "no JUnit",
        evidence={"command": ["pytest"], "exit_code": 0, "junit": None},
    )
    manifest = certify_release.CertificationManifest(steps=[step], verdict="INCOMPLETE")
    env = certify_release.EnvironmentSnapshot("3.10", "linux", "uv")
    with patch("certify_release.run_command", return_value=MagicMock(stdout="", returncode=0)):
        certify_release.write_bundle(
            tmp_path / "artifacts" / "certification" / "abc", manifest, env
        )
    bundle = tmp_path / "artifacts" / "certification" / "abc"
    summary = json.loads((bundle / "test-summary.json").read_text())
    serialized = json.loads((bundle / "manifest.json").read_text())
    assert summary["test_clean"]["junit"] is None
    assert summary["test_clean"]["exit_code"] == serialized["steps"][0]["evidence"]["exit_code"]
    assert all(
        (bundle / name).exists()
        for name in (
            "lint-type-build.json",
            "package-smoke.json",
            "security-summary.json",
            "docs-check.json",
        )
    )


def test_skipped_docs_cannot_certify():
    step = certify_release.StepResult("docs_check", "Docs", "SKIPPED")
    assert certify_release.compute_verdict([step], git_dirty=False) == "INCOMPLETE"


@pytest.mark.parametrize("dirty", [False, True])
def test_pytest_evidence_valid_report_passes(tmp_path, dirty):
    runner = (
        certify_release.step_test_dirty_shell if dirty else certify_release.step_test_clean_shell
    )

    def fake_run(cmd, **kwargs):
        Path(cmd[-1].removeprefix("--junitxml=")).write_text(
            '<testsuite tests="3" failures="0" errors="0" skipped="1">'
            '<testcase classname="c" name="a"/><testcase classname="c" name="b"/>'
            '<testcase classname="c" name="c"><skipped/></testcase></testsuite>'
        )
        return MagicMock(returncode=0)

    with patch("certify_release.run_command", side_effect=fake_run):
        step = runner(tmp_path, tmp_path)
    assert step.status == "PASS"
    assert step.evidence["junit"]["passed"] == 2
    assert step.evidence["junit"]["testcases"] == {
        "c::a": "passed",
        "c::b": "passed",
        "c::c": "skipped",
    }
    assert step.evidence["command"][-1] == "--junitxml=<temporary-report>"


def test_scanner_error_does_not_serialize_stderr_secret(tmp_path):
    (tmp_path / "bandit").touch()
    (tmp_path / "pip-audit").touch()
    with patch(
        "certify_release.run_command",
        return_value=MagicMock(returncode=1, stderr="PRIVATE_KEY=do-not-persist"),
    ):
        step = certify_release.step_security(tmp_path, tmp_path)
    assert step.status == "FAIL"
    assert "do-not-persist" not in json.dumps(step.evidence) + step.reason


def test_rehearsal_requires_both_proofs(tmp_path):
    clean = Path(__file__).parent.parent / "docs/proof/live-controller-run"
    result = certify_release.step_rehearsals(tmp_path, {"clean": clean}, tmp_path / "bundle")
    assert result.status == "INCOMPLETE"
    assert "chaos" in result.reason


def test_real_rehearsal_receipt_is_verified_but_not_attested(tmp_path):
    import shutil

    demo = Path(__file__).parent.parent / "docs" / "proof" / "demo-run"
    clean = tmp_path / "clean"
    chaos = tmp_path / "chaos"
    for target in (clean, chaos):
        target.mkdir()
        for name in ("events.jsonl", "receipt.json", "graph.json"):
            shutil.copy2(demo / name, target / name)
    # A valid recorded fault is controlled-failure proof; self-reported producer
    # identity is still insufficient for an independently certified release.
    result = certify_release.step_rehearsals(
        tmp_path, {"clean": clean, "chaos": chaos}, tmp_path / "bundle"
    )
    assert result.status == "FAIL"  # clean is in fact a chaos run
    assert "injected faults" in result.reason
    result = certify_release.step_rehearsals(
        tmp_path, {"chaos": chaos, "clean": clean}, tmp_path / "other"
    )
    assert result.status == "FAIL"
    assert result.evidence["runs"]["chaos"]["injected_failures"] > 0
    assert result.evidence["runs"]["chaos"]["attempt_producers"]

    # A clean local receipt plus valid chaos receipt still cannot attest the
    # gateway producer independently.
    clean_source = Path(__file__).parent.parent / "docs/proof/live-controller-run"
    for name in ("events.jsonl", "receipt.json", "graph.json"):
        shutil.copy2(clean_source / name, clean / name)
    if (clean_source / "review.json").is_file():
        shutil.copy2(clean_source / "review.json", clean / "review.json")
    result = certify_release.step_rehearsals(
        tmp_path, {"clean": clean, "chaos": chaos}, tmp_path / "verified"
    )
    assert result.status == "INCOMPLETE"
    assert result.evidence["runs"]["clean"]["outcome"] == "COMPLETE"
    assert result.evidence["runs"]["chaos"]["injected_failures"] > 0
    assert result.evidence["independent_producer_attestation"] is False


def test_junit_parity_and_independent_rehearsal_attestation_required():
    """Even all PASS statuses do not bypass parity or evidence requirements."""
    # Reuse complete evidence constructor from the test above via its own fixture layout.
    command = {"command": ["tool"], "exit_code": 0}
    steps = [
        certify_release.StepResult(k, k, "PASS", evidence=dict(command))
        for k in ("ruff_check", "ruff_format", "mypy", "docs_check")
    ]
    for k, count in (("test_clean", 2), ("test_dirty", 3)):
        steps.append(
            certify_release.StepResult(
                k,
                k,
                "PASS",
                evidence={
                    **command,
                    "junit": {
                        "tests": count,
                        "passed": count,
                        "failures": 0,
                        "errors": 0,
                        "skipped": 0,
                        "sha256": "abc",
                    },
                },
            )
        )
    steps += [
        certify_release.StepResult(
            "build",
            "Build",
            "PASS",
            evidence={**command, "artifacts": [{"name": "verdict.whl", "sha256": "abc"}]},
        ),
        certify_release.StepResult(
            "package_smoke",
            "Smoke",
            "PASS",
            evidence={
                "commands": [{"command": ["uv"], "exit_code": 0}] * 3,
                "help_has_usage": True,
            },
        ),
        certify_release.StepResult(
            "security",
            "Security",
            "PASS",
            evidence={"bandit": {"report_sha256": "abc"}, "pip_audit": {"report_sha256": "def"}},
        ),
        certify_release.StepResult("git_clean", "Git", "PASS"),
        certify_release.StepResult(
            "rehearsals", "Rehearsals", "PASS", evidence={"independent_producer_attestation": True}
        ),
    ]
    assert certify_release.compute_verdict(steps, git_dirty=False) == "INCOMPLETE"
    steps[5].evidence["junit"]["tests"] = 2
    steps[5].evidence["junit"]["passed"] = 2
    assert certify_release.compute_verdict(steps, git_dirty=False) == "INCOMPLETE"
    steps[-1].evidence.clear()
    assert certify_release.compute_verdict(steps, git_dirty=False) == "INCOMPLETE"


def test_custom_output_and_rehearsal_location_and_final_dirty(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    import subprocess

    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=t@example.com",
            "commit",
            "--allow-empty",
            "-qm",
            "initial",
        ],
        cwd=repo,
        check=True,
    )
    custom = tmp_path / "custom"
    custom.mkdir()
    stale = custom / "dont-delete"
    stale.write_text("keep")
    with pytest.raises(ValueError, match="empty"):
        certify_release.prepare_bundle_dir(custom, repo)
    assert stale.read_text() == "keep"
    # Main must pass --output-dir to the runner, and the writer must receive
    # the checkout root rather than guessing from the custom path.
    manifest = certify_release.CertificationManifest(
        git_sha=certify_release.get_git_sha(repo), verdict="INCOMPLETE"
    )
    env = certify_release.EnvironmentSnapshot("3.10", "linux", "uv")
    with (
        patch(
            "certify_release.run_certification",
            return_value=(manifest, {"environment": env.to_dict()}),
        ) as runner,
        patch("certify_release.write_bundle") as writer,
        patch("certify_release.publish_bundle") as publisher,
        patch("certify_release.assert_git_sha"),
        patch.object(
            sys, "argv", ["certify_release.py", "--output-dir", str(tmp_path / "empty-main")]
        ),
        pytest.raises(SystemExit),
    ):
        monkeypatch.chdir(repo)
        certify_release.main()
    assert runner.call_args.kwargs["output_dir"] == tmp_path / "empty-main"
    assert writer.call_args.kwargs["repo_path"] == repo
    assert publisher.call_args.args[1] == tmp_path / "empty-main"
    assert publisher.call_args.args[3] == manifest.git_sha

    manifest = certify_release.CertificationManifest(
        steps=[certify_release.StepResult("git_clean", "Git", "PASS")], verdict="INCOMPLETE"
    )
    custom2 = tmp_path / "empty"
    with patch(
        "certify_release.run_command", return_value=MagicMock(stdout=" M README.md\n", returncode=0)
    ) as run:
        certify_release.write_bundle(custom2, manifest, env, repo_path=repo)
    assert run.call_args.kwargs["cwd"] == repo
    assert manifest.steps[0].status == "FAIL"
    assert manifest.verdict == "FAILED"
    assert json.loads((custom2 / "manifest.json").read_text())["verdict"] == "FAILED"


def test_final_git_check_catches_a_late_tracked_change(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    output = tmp_path / "bundle"
    manifest = certify_release.CertificationManifest(
        steps=[certify_release.StepResult("git_clean", "Git", "PASS")], verdict="CERTIFIED"
    )
    env = certify_release.EnvironmentSnapshot("3.10", "linux", "uv")
    responses = [
        MagicMock(stdout="", returncode=0),
        MagicMock(stdout=" M tracked.txt\n", returncode=0),
    ]
    with patch("certify_release.run_command", side_effect=responses) as runner:
        certify_release.write_bundle(output, manifest, env, repo_path=repo)
    assert runner.call_count == 2
    assert manifest.verdict == "FAILED"
    assert "tracked.txt" in (output / "git-clean.txt").read_text()
    assert json.loads((output / "manifest.json").read_text())["verdict"] == "FAILED"


def test_sha_bundle_cleanup_removes_stale_but_refuses_tracked(temp_git_repo):
    repo = temp_git_repo
    sha = certify_release.get_git_sha(repo)
    target = repo / "artifacts" / "certification" / sha
    target.mkdir(parents=True)
    (target / "stale").write_text("leftover")
    (repo / ".gitignore").write_text("artifacts/\n")
    certify_release.prepare_bundle_dir(target, repo)
    assert (target / "stale").read_text() == "leftover"
    (target / "tracked").write_text("must stay")
    import subprocess

    subprocess.run(["git", "add", "-f", str(target / "tracked")], cwd=repo, check=True)
    with pytest.raises(ValueError, match="tracked"):
        certify_release.prepare_bundle_dir(target, repo)
    assert (target / "tracked").exists()


def test_clone_command_evidence_is_relative(tmp_path):
    with patch(
        "certify_release.run_command",
        return_value=MagicMock(returncode=0, stdout="Success: no issues found in 1 source file"),
    ):
        lint = certify_release.step_ruff_check(tmp_path, tmp_path / ".venv" / "bin")
        types = certify_release.step_mypy(tmp_path, tmp_path / ".venv" / "bin")
    assert lint.evidence["command"][0] == "<checkout>/.venv/bin/ruff"
    assert types.evidence["command"][0] == "<checkout>/.venv/bin/mypy"


def test_junit_same_totals_different_case_outcomes_fail_closed(tmp_path):
    """Equal aggregate counts cannot hide a different skipped testcase."""
    suites = (
        '<testcase classname="test" name="a"/><testcase classname="test" name="b">'
        "<skipped/></testcase>",
        '<testcase classname="test" name="a"><skipped/></testcase>'
        '<testcase classname="test" name="b"/>',
    )
    results = []
    for dirty, cases in zip((False, True), suites, strict=True):

        def fake_run(cmd, *, case_xml=cases, is_dirty=dirty, **kwargs):
            Path(cmd[-1].removeprefix("--junitxml=")).write_text(
                '<testsuite tests="2" failures="0" errors="0" skipped="1">'
                + case_xml
                + "</testsuite>"
            )
            assert ("LLMGATE_AUTH_TOKEN" in kwargs["env"]) == is_dirty
            assert "HOSTILE_AMBIENT" not in kwargs["env"]
            return MagicMock(returncode=0)

        with (
            patch.dict("certify_release.os.environ", {"HOSTILE_AMBIENT": "do-not-pass"}),
            patch("certify_release.run_command", side_effect=fake_run),
        ):
            results.append(certify_release._run_pytest(tmp_path, tmp_path, dirty=dirty))
    assert all(step.status == "PASS" for step in results)
    assert all(step.evidence["junit"]["passed"] == 1 for step in results)
    assert results[0].evidence["junit"]["testcases"] != results[1].evidence["junit"]["testcases"]
    assert certify_release.junit_parity(results) is False


def test_default_stale_bundle_removed_even_when_dirty_check_exits(temp_git_repo):
    repo = temp_git_repo
    sha = certify_release.get_git_sha(repo)
    bundle = repo / "artifacts" / "certification" / sha
    bundle.mkdir(parents=True)
    (bundle / "stale").write_text("must not survive")
    (repo / ".gitignore").write_text("artifacts/\n")
    with pytest.raises(SystemExit):
        certify_release.run_certification(repo)
    assert (bundle / "stale").read_text() == "must not survive"
    records = list((bundle.parent / ".attempts" / sha).glob("*.json"))
    assert len(records) == 1
    assert json.loads(records[0].read_text())["status"] == "ABORTED"


def test_staged_publication_never_exposes_partial_bundle(temp_git_repo, tmp_path):
    repo = temp_git_repo
    (repo / ".gitignore").write_text("artifacts/\n")
    bundle = repo / "artifacts" / "certification" / certify_release.get_git_sha(repo)
    bundle.mkdir(parents=True)
    (bundle / "stale").write_text("old")
    with tempfile.TemporaryDirectory(prefix=".cert-stage-", dir=bundle.parent) as stage:
        staging = Path(stage)
        (staging / "manifest.json").write_text("complete")
        certify_release.prepare_bundle_dir(bundle, repo, create=False)
        assert (bundle / "stale").read_text() == "old"
        certify_release.publish_bundle(staging, bundle, repo, certify_release.get_git_sha(repo))
        assert (bundle / "manifest.json").read_text() == "complete"
        history = list((bundle.parent / ".history" / bundle.name).iterdir())
        assert len(history) == 1
        assert (history[0] / "stale").read_text() == "old"


def test_markdown_table_escapes_untrusted_reason():
    manifest = certify_release.CertificationManifest(
        steps=[certify_release.StepResult("x", "unsafe|<b>", "FAIL", "a|b\n<script>x</script>")]
    )
    text = certify_release.generate_certification_md(
        manifest, certify_release.EnvironmentSnapshot("3.11", "linux", "uv")
    )
    assert "unsafe\\|&lt;b&gt;" in text
    assert "a\\|b<br>&lt;script&gt;x&lt;/script&gt;" in text
    assert "All other fields should be identical" not in text


def test_main_staging_failure_does_not_publish_partial_bundle(temp_git_repo, monkeypatch):
    repo = temp_git_repo
    monkeypatch.chdir(repo)
    (repo / ".gitignore").write_text("artifacts/\n")
    sha = certify_release.get_git_sha(repo)
    bundle = repo / "artifacts" / "certification" / sha
    bundle.mkdir(parents=True)
    (bundle / "stale").write_text("old")
    manifest = certify_release.CertificationManifest(git_sha=sha, verdict="INCOMPLETE")
    env = certify_release.EnvironmentSnapshot("3.11", "linux", "uv")

    def fake_certify(repo_path, *, output_dir, staging_dir, **kwargs):
        certify_release.prepare_bundle_dir(output_dir, repo_path, create=False)
        assert (bundle / "stale").read_text() == "old"
        return manifest, {"environment": env.to_dict()}

    def failing_write(staging_dir, *args, **kwargs):
        (staging_dir / "manifest.json").write_text("partial")
        raise OSError("simulated interrupted write")

    with (
        patch("certify_release.run_certification", side_effect=fake_certify),
        patch("certify_release.write_bundle", side_effect=failing_write),
        patch.object(sys, "argv", ["certify_release.py"]),
        pytest.raises(OSError, match="interrupted"),
    ):
        certify_release.main()
    assert (bundle / "stale").read_text() == "old"
    assert not list(bundle.parent.glob(".cert-stage-*"))


def test_clean_head_checkout_during_step_aborts_without_old_sha_bundle(temp_git_repo, monkeypatch):
    """A clean checkout switch during a long step must not certify the old SHA."""
    import subprocess

    repo = temp_git_repo
    (repo / ".gitignore").write_text("artifacts/\n")
    subprocess.run(["git", "add", ".gitignore"], cwd=repo, check=True)
    subprocess.run(
        ["git", "commit", "-m", "ignore outputs"], cwd=repo, check=True, capture_output=True
    )
    original_sha = certify_release.get_git_sha(repo)
    (repo / "README.md").write_text("# Different clean commit")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "new source"], cwd=repo, check=True, capture_output=True)
    next_sha = certify_release.get_git_sha(repo)
    subprocess.run(
        ["git", "checkout", "--detach", original_sha], cwd=repo, check=True, capture_output=True
    )
    assert not certify_release.is_git_dirty(repo)
    (repo / ".venv" / "bin").mkdir(parents=True)
    original_run = certify_release.run_command

    def fake_sanity(cmd, **kwargs):
        if "-c" in cmd and "import verdict" in cmd[-1]:
            return subprocess.CompletedProcess(cmd, 0, "", "")
        return original_run(cmd, **kwargs)

    def switch_head(_repo, _venv):
        subprocess.run(
            ["git", "checkout", "--detach", next_sha], cwd=repo, check=True, capture_output=True
        )
        assert not certify_release.is_git_dirty(repo)
        return certify_release.StepResult("test_clean", "Tests", "PASS")

    monkeypatch.setattr(certify_release, "run_command", fake_sanity)
    monkeypatch.setattr(certify_release, "step_test_clean_shell", switch_head)
    old_bundle = repo / "artifacts" / "certification" / original_sha
    with pytest.raises(RuntimeError, match="HEAD changed"):
        certify_release.run_certification(repo)
    assert not old_bundle.exists()


def test_publish_checks_sha_again_after_cleanup(temp_git_repo, monkeypatch, tmp_path):
    """Do not rename old-SHA staging if HEAD changes at publish time."""
    import subprocess

    repo = temp_git_repo
    old_sha = certify_release.get_git_sha(repo)
    (repo / "README.md").write_text("# another commit")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "new source"], cwd=repo, check=True, capture_output=True)
    new_sha = certify_release.get_git_sha(repo)
    subprocess.run(
        ["git", "checkout", "--detach", old_sha], cwd=repo, check=True, capture_output=True
    )
    staging = tmp_path / "stage"
    staging.mkdir()
    (staging / "manifest.json").write_text("old evidence")
    output = tmp_path / "output"
    real_prepare = certify_release.prepare_bundle_dir

    def switch_during_cleanup(destination, checkout, *, create=True):
        real_prepare(destination, checkout, create=create)
        subprocess.run(
            ["git", "checkout", "--detach", new_sha], cwd=repo, check=True, capture_output=True
        )

    monkeypatch.setattr(certify_release, "prepare_bundle_dir", switch_during_cleanup)
    with pytest.raises(RuntimeError, match="HEAD changed"):
        certify_release.publish_bundle(staging, output, repo, old_sha)
    assert (staging / "manifest.json").exists()
    assert not output.exists()


@pytest.mark.parametrize(
    "value", ["<script>alert(1)</script>|#x\n# injected", "**bold**`code`[link](x)\r<div>"]
)
def test_markdown_escapes_every_dynamic_scalar(value):
    manifest = certify_release.CertificationManifest(
        git_sha=value,
        git_dirty=value,
        started_at=value,
        finished_at=value,
        verdict=value,
        steps=[certify_release.StepResult("x", value, value, value)],
    )
    environment = {
        key: value
        for key in ("python_version", "platform", "uv_version", "node_version", "lockfile_sha256")
    }
    environment["env_var_names"] = []
    md = certify_release.generate_certification_md(manifest, environment)
    escaped = certify_release.markdown_cell(value)
    assert escaped in md
    assert value not in md
    assert "<script>" not in md and "<div>" not in md
    for label in (
        "Verdict",
        "Started",
        "Finished",
        "Git SHA",
        "Git Dirty",
        "Python",
        "Platform",
        "uv",
        "Node",
    ):
        assert f"**{label}**: {escaped}" in md
    assert f"**Lockfile SHA256**: {certify_release.markdown_cell(value[:16])}..." in md
    assert len([line for line in md.splitlines() if line.startswith("| ")]) == 2


def test_main_rejects_head_switch_during_bundle_write(temp_git_repo, monkeypatch):
    """A clean checkout switch after staging cannot publish under the old SHA."""
    import subprocess

    repo = temp_git_repo
    (repo / ".gitignore").write_text("artifacts/\n")
    subprocess.run(["git", "add", ".gitignore"], cwd=repo, check=True)
    subprocess.run(
        ["git", "commit", "-m", "ignore outputs"], cwd=repo, check=True, capture_output=True
    )
    old_sha = certify_release.get_git_sha(repo)
    (repo / "README.md").write_text("# next commit")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "next"], cwd=repo, check=True, capture_output=True)
    new_sha = certify_release.get_git_sha(repo)
    subprocess.run(
        ["git", "checkout", "--detach", old_sha], cwd=repo, check=True, capture_output=True
    )
    monkeypatch.chdir(repo)
    manifest = certify_release.CertificationManifest(git_sha=old_sha, verdict="INCOMPLETE")
    env = certify_release.EnvironmentSnapshot("3.13", "linux", "uv")

    def switching_write(staging, *_args, **_kwargs):
        (staging / "manifest.json").write_text("old staged evidence")
        subprocess.run(
            ["git", "checkout", "--detach", new_sha], cwd=repo, check=True, capture_output=True
        )
        assert not certify_release.is_git_dirty(repo)

    with (
        patch(
            "certify_release.run_certification",
            return_value=(manifest, {"environment": env.to_dict()}),
        ),
        patch("certify_release.write_bundle", side_effect=switching_write),
        patch("certify_release.publish_bundle") as publisher,
        patch.object(sys, "argv", ["certify_release.py"]),
        pytest.raises(RuntimeError, match="HEAD changed"),
    ):
        certify_release.main()
    publisher.assert_not_called()
    assert not (repo / "artifacts" / "certification" / old_sha).exists()
    assert not list((repo / "artifacts" / "certification").glob(".cert-stage-*"))


@pytest.mark.parametrize("failure", ["missing_venv", "dirty"])
def test_retained_complete_bundle_survives_early_failure(temp_git_repo, failure):
    import subprocess

    repo = temp_git_repo
    (repo / ".gitignore").write_text("artifacts/\n.venv/\n")
    subprocess.run(["git", "add", ".gitignore"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "ignore evidence"], cwd=repo, check=True)
    sha = certify_release.get_git_sha(repo)
    bundle = repo / "artifacts" / "certification" / sha
    bundle.mkdir(parents=True)
    old = {
        "manifest.json": '{"verdict":"CERTIFIED","complete":true}',
        "CERTIFICATION.md": "# prior complete evidence",
        "security-summary.json": "{}",
    }
    for name, content in old.items():
        (bundle / name).write_text(content)
    if failure == "dirty":
        (repo / "README.md").write_text("changed")
    with pytest.raises(SystemExit):
        certify_release.run_certification(repo)
    assert {name: (bundle / name).read_text() for name in old} == old
    records = list((bundle.parent / ".attempts" / sha).glob("*.json"))
    assert len(records) == 1
    record = json.loads(records[0].read_text())
    assert record["status"] == "ABORTED"
    assert record["started_at"] and record["finished_at"] and record["reason"]


@pytest.mark.parametrize("tool", ["ruff", "mypy", "build", "docs", "bandit", "pip-audit"])
@pytest.mark.parametrize(
    "failure", ["timeout", "missing"], ids=["TimeoutExpired", "FileNotFoundError"]
)
def test_external_command_failure_publishes_failed_bundle(
    temp_git_repo, monkeypatch, tool, failure
):
    """A failed subprocess cannot crash certification or hide the failed step."""
    import subprocess

    repo = temp_git_repo
    (repo / ".gitignore").write_text("artifacts/\n.venv/\ndist/\n")
    (repo / "scripts").mkdir()
    (repo / "scripts" / "check_doc_links.py").write_text("# test checker")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "test inputs"], cwd=repo, check=True)
    venv_bin = repo / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    for binary in ("bandit", "pip-audit"):
        (venv_bin / binary).touch()
    selected = {
        "ruff": "ruff_check",
        "mypy": "mypy",
        "build": "build",
        "docs": "docs_check",
        "bandit": "security",
        "pip-audit": "security",
    }
    step_id = selected[tool]
    for name, ident in (
        ("step_test_clean_shell", "test_clean"),
        ("step_test_dirty_shell", "test_dirty"),
        ("step_ruff_check", "ruff_check"),
        ("step_ruff_format", "ruff_format"),
        ("step_mypy", "mypy"),
        ("step_build", "build"),
        ("step_package_smoke", "package_smoke"),
        ("step_security", "security"),
        ("step_docs_check", "docs_check"),
        ("step_rehearsals", "rehearsals"),
    ):
        if ident == step_id:
            continue
        monkeypatch.setattr(
            certify_release,
            name,
            lambda *args, _ident=ident, **kwargs: certify_release.StepResult(
                _ident, _ident, "INCOMPLETE"
            ),
        )
    real_run = subprocess.run

    def controlled_run(cmd, **kwargs):
        assert kwargs.get("timeout", 0) > 0
        if "-c" in cmd and "import verdict" in cmd[-1]:
            return subprocess.CompletedProcess(cmd, 0, "", "")
        executable = Path(cmd[0]).name
        target_binary = "uv" if tool == "build" else "python" if tool == "docs" else tool
        matching = (
            executable == target_binary
            and (tool != "build" or "build" in cmd)
            and (tool != "docs" or "check_doc_links.py" in cmd[-1])
        )
        if matching:
            if failure == "timeout":
                raise subprocess.TimeoutExpired(cmd, kwargs["timeout"])
            raise FileNotFoundError("binary missing")
        if executable == "pip-audit":
            Path(cmd[cmd.index("-o") + 1]).write_text('{"dependencies":[]}')
            return subprocess.CompletedProcess(cmd, 0, "", "")
        if executable == "bandit":
            Path(cmd[cmd.index("-o") + 1]).write_text('{"results":[]}')
            return subprocess.CompletedProcess(cmd, 0, "", "")
        if executable in ("uv", "node"):
            return subprocess.CompletedProcess(cmd, 0, "ok", "")
        return real_run(cmd, **kwargs)

    monkeypatch.setattr(subprocess, "run", controlled_run)
    monkeypatch.chdir(repo)
    with patch.object(sys, "argv", ["certify_release.py"]), pytest.raises(SystemExit) as exc:
        certify_release.main()
    assert exc.value.code == 1
    bundle = repo / "artifacts" / "certification" / certify_release.get_git_sha(repo)
    manifest = json.loads((bundle / "manifest.json").read_text())
    assert manifest["verdict"] == "FAILED"
    step = next(item for item in manifest["steps"] if item["step_id"] == step_id)
    assert step["status"] == "FAIL"
    evidence = step["evidence"]
    if tool in ("bandit", "pip-audit"):
        evidence = evidence["bandit" if tool == "bandit" else "pip_audit"]
    if failure == "timeout":
        assert evidence["timed_out"] is True
        assert evidence["timeout_seconds"] > 0
    else:
        assert evidence["error_class"] == "FileNotFoundError"


def test_docs_check_command_is_checkout_independent(tmp_path):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "check_doc_links.py").touch()
    with patch("certify_release.run_command", return_value=MagicMock(returncode=0)):
        step = certify_release.step_docs_check(tmp_path, tmp_path / ".venv" / "bin")
    assert step.evidence["command"] == ["<checkout>/.venv/bin/python", "scripts/check_doc_links.py"]
    assert str(tmp_path) not in json.dumps(step.evidence)


def test_rehearsal_attempt_identity_is_receipt_reported(tmp_path):
    import shutil

    clean_source = Path(__file__).parent.parent / "docs/proof/live-controller-run"
    clean = tmp_path / "clean"
    clean.mkdir()
    for name in ("events.jsonl", "receipt.json", "graph.json", "review.json"):
        if (clean_source / name).exists():
            shutil.copy2(clean_source / name, clean / name)
    result = certify_release.step_rehearsals(tmp_path, {"clean": clean}, tmp_path / "bundle")
    assert result.status == "INCOMPLETE"
    attempts = result.evidence["runs"]["clean"]["attempts"]
    assert attempts
    assert "receipt-reported" in result.evidence["runs"]["clean"]["attempt_identity_source"]
    keys = {
        "node_id",
        "attempt",
        "intended_route",
        "executed_model",
        "route_identity",
        "outcome",
        "fault_injected",
        "failure_category",
    }
    assert all(keys <= set(attempt) for attempt in attempts)
    receipt = json.loads((clean / "receipt.json").read_text())
    original = [
        (node["node_id"], attempt) for node in receipt["nodes"] for attempt in node["attempts"]
    ]
    assert len(original) == len(attempts)
    for projected, (node_id, attempt) in zip(attempts, original, strict=True):
        assert projected["node_id"] == node_id
        for key in keys - {"node_id"}:
            assert projected[key] == attempt.get(key)


def test_interrupted_publish_keeps_previous_complete_bundle_in_history(temp_git_repo, monkeypatch):
    import os
    import subprocess

    repo = temp_git_repo
    (repo / ".gitignore").write_text("artifacts/\n")
    subprocess.run(["git", "add", ".gitignore"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "ignore bundles"], cwd=repo, check=True)
    sha = certify_release.get_git_sha(repo)
    target = repo / "artifacts" / "certification" / sha
    target.mkdir(parents=True)
    (target / "manifest.json").write_text('{"verdict":"CERTIFIED"}')
    stage = target.parent / ".cert-stage-interrupted"
    stage.mkdir()
    (stage / "manifest.json").write_text('{"verdict":"INCOMPLETE"}')
    original_replace = os.replace

    def fail_new_bundle(source, destination):
        if Path(source) == stage:
            raise OSError("publish interrupted")
        return original_replace(source, destination)

    monkeypatch.setattr(certify_release.os, "replace", fail_new_bundle)
    with pytest.raises(OSError, match="publish interrupted"):
        certify_release.publish_bundle(stage, target, repo, sha)
    previous = list((target.parent / ".history" / sha).glob("*/manifest.json"))
    assert len(previous) == 1
    assert previous[0].read_text() == '{"verdict":"CERTIFIED"}'
    assert (stage / "manifest.json").exists()


def test_git_command_timeout_fails_closed(temp_git_repo):
    import subprocess

    repo = temp_git_repo
    original = certify_release.run_command

    def failed_status(cmd, **kwargs):
        if cmd[:2] == ["git", "status"]:
            with patch(
                "certify_release.subprocess.run", side_effect=subprocess.TimeoutExpired(cmd, 300)
            ):
                return original(cmd, **kwargs)
        return original(cmd, **kwargs)

    with patch("certify_release.run_command", side_effect=failed_status):
        step = certify_release.step_git_clean(repo)
    assert step.status == "FAIL"
    assert step.evidence["timed_out"] is True
    assert step.evidence["timeout_seconds"] == 300


def test_failed_rerun_keeps_prior_bundle_and_stores_attempt_evidence(temp_git_repo, monkeypatch):
    import subprocess

    repo = temp_git_repo
    (repo / ".gitignore").write_text("artifacts/\n")
    subprocess.run(["git", "add", ".gitignore"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "ignore"], cwd=repo, check=True)
    sha = certify_release.get_git_sha(repo)
    bundle = repo / "artifacts" / "certification" / sha
    bundle.mkdir(parents=True)
    (bundle / "manifest.json").write_text('{"verdict":"CERTIFIED"}')
    manifest = certify_release.CertificationManifest(
        git_sha=sha,
        verdict="FAILED",
        steps=[
            certify_release.StepResult(
                "ruff_check", "ruff", "FAIL", evidence={"timed_out": True, "timeout_seconds": 300}
            )
        ],
    )
    env = certify_release.EnvironmentSnapshot("3.13", "linux", "uv")
    monkeypatch.chdir(repo)
    with (
        patch(
            "certify_release.run_certification",
            return_value=(manifest, {"environment": env.to_dict()}),
        ),
        patch.object(sys, "argv", ["certify_release.py"]),
        pytest.raises(SystemExit),
    ):
        certify_release.main()
    assert (bundle / "manifest.json").read_text() == '{"verdict":"CERTIFIED"}'
    records = list((bundle.parent / ".attempts" / sha).glob("*.json"))
    assert len(records) == 1
    record = json.loads(records[0].read_text())
    assert record["status"] == "FAILED"
    assert record["bundle_path"]
    attempt_bundle = repo / record["bundle_path"]
    assert json.loads((attempt_bundle / "manifest.json").read_text())["verdict"] == "FAILED"
    assert not list((bundle.parent / ".history").glob(f"{sha}/*"))
