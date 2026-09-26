"""Tests for OpenSpec lifecycle binding."""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path
from unittest import mock

import pytest

from verdict.openspec_lifecycle import (
    OpenSpecChange,
    admit_significant_change,
    change_id_to_linear_issue,
    linear_issue_to_change_id,
    load_change,
    spec_revision_digest,
)


def test_linear_issue_to_change_id_lowercase():
    """Linear issue IDs are converted to lowercase change IDs."""
    assert linear_issue_to_change_id("BOD-205") == "bod-205"
    assert linear_issue_to_change_id("BOD-123") == "bod-123"


def test_change_id_to_linear_issue_reverse():
    """Change IDs can be reverse-mapped to Linear issues."""
    assert change_id_to_linear_issue("bod-205-openspec-lifecycle") == "BOD-205"
    assert change_id_to_linear_issue("bod-123-feature") == "BOD-123"
    assert change_id_to_linear_issue("invalid") is None


def test_linear_roundtrip():
    """Mapping roundtrip preserves the issue number."""
    issue = "BOD-205"
    change = linear_issue_to_change_id(issue)
    assert change == "bod-205"
    # The reverse lookup works on any bod-NNN prefix
    assert change_id_to_linear_issue(f"{change}-anything") == issue


def test_admit_valid_change_passes():
    """A valid verdict-change-v1 change is admitted."""
    fixtures = Path(__file__).parent / "fixtures" / "openspec" / "valid-change"
    assert fixtures.exists(), f"Fixture not found: {fixtures}"

    change = OpenSpecChange(
        change_id="bod-204-openspec-foundation",
        linear_issue="BOD-204",
        schema="verdict-change-v1",
        change_dir=fixtures,
        artifacts={},
    )

    result = admit_significant_change(change, fixtures.parent.parent.parent)

    assert result.admitted is True
    assert result.category == "ok"
    assert "validated and admitted" in result.reason.lower()


def test_admit_placeholder_content_fails():
    """Admission rejects changes with PLACEHOLDER-only required sections."""
    fixtures = Path(__file__).parent / "fixtures" / "openspec" / "placeholder-change"
    assert fixtures.exists(), f"Fixture not found: {fixtures}"

    change = OpenSpecChange(
        change_id="placeholder-change",
        linear_issue=None,
        schema="verdict-change-v1",
        change_dir=fixtures,
        artifacts={},
    )

    result = admit_significant_change(change, fixtures.parent.parent.parent)

    assert result.admitted is False
    assert result.category == "blocked"
    assert "PLACEHOLDER" in result.reason


def test_admit_missing_change_fails():
    """Admission fails when change is None."""
    with mock.patch("verdict.openspec_lifecycle._run_openspec_command") as mock_run:
        mock_run.return_value = mock.Mock(returncode=0, stdout="1.13.2")
        result = admit_significant_change(None, Path("/tmp"))

    assert result.admitted is False
    assert "not found" in result.reason.lower()
    assert result.category == "blocked"


def test_admit_openspec_unavailable():
    """Admission reports openspec_unavailable when CLI is missing."""
    with mock.patch("verdict.openspec_lifecycle._run_openspec_command") as mock_run:
        mock_run.return_value = None  # Returns None when subprocess fails
        result = admit_significant_change(None, Path("/tmp"))

    assert result.admitted is False
    assert result.category == "openspec_unavailable"
    assert "unavailable" in result.reason.lower()


def test_spec_revision_digest_deterministic():
    """The spec revision digest is deterministic."""
    fixtures = Path(__file__).parent / "fixtures" / "openspec" / "valid-change"

    change = OpenSpecChange(
        change_id="test",
        linear_issue=None,
        schema="verdict-change-v1",
        change_dir=fixtures,
        artifacts={},
    )

    digest1 = spec_revision_digest(change)
    digest2 = spec_revision_digest(change)

    assert digest1 == digest2
    assert len(digest1) == 64  # SHA256 hex


def test_spec_revision_digest_changes_on_edit():
    """The digest changes when a file is modified."""
    with tempfile.TemporaryDirectory() as tmpdir:
        change_dir = Path(tmpdir) / "change"
        change_dir.mkdir()
        (change_dir / "proposal.md").write_text("original", encoding="utf-8")

        change = OpenSpecChange(
            change_id="test", linear_issue=None, schema="test", change_dir=change_dir, artifacts={}
        )

        digest_before = spec_revision_digest(change)

        # Modify the file
        (change_dir / "proposal.md").write_text("modified", encoding="utf-8")

        digest_after = spec_revision_digest(change)

        assert digest_before != digest_after


def test_openspec_command_sets_telemetry_env():
    """All openspec subprocess calls set OPENSPEC_TELEMETRY=0."""
    from verdict.openspec_lifecycle import _run_openspec_command

    with mock.patch("subprocess.run") as mock_run:
        mock_run.return_value = mock.Mock(returncode=0, stdout="", stderr="")

        _run_openspec_command(["--version"], cwd=Path("/tmp"))

        # Check that the env dict contains OPENSPEC_TELEMETRY=0
        call_args = mock_run.call_args
        env = call_args.kwargs["env"]

        assert "OPENSPEC_TELEMETRY" in env
        assert env["OPENSPEC_TELEMETRY"] == "0"


def test_load_change_returns_none_when_missing():
    """load_change returns None when the change doesn't exist."""
    with tempfile.TemporaryDirectory() as tmpdir:
        repo = Path(tmpdir)
        (repo / "openspec" / "changes").mkdir(parents=True)

        with mock.patch("verdict.openspec_lifecycle._run_openspec_command") as mock_run:
            mock_run.return_value = mock.Mock(returncode=1, stdout="", stderr="not found")

            change = load_change(repo, "nonexistent")

        assert change is None


def test_load_change_reads_artifacts():
    """load_change reads artifact files when change exists."""
    fixtures_root = Path(__file__).parent / "fixtures" / "openspec"
    valid = fixtures_root / "valid-change"

    # Create a mock repo structure
    with tempfile.TemporaryDirectory() as tmpdir:
        repo = Path(tmpdir)
        changes_dir = repo / "openspec" / "changes"
        changes_dir.mkdir(parents=True)

        # Copy the valid fixture
        import shutil

        target = changes_dir / "bod-204-openspec-foundation"
        shutil.copytree(valid, target)

        # Mock the openspec command
        mock_json = json.dumps(
            {"id": "bod-204-openspec-foundation", "schema": "verdict-change-v1", "status": "active"}
        )

        with mock.patch("verdict.openspec_lifecycle._run_openspec_command") as mock_run:
            mock_run.return_value = mock.Mock(returncode=0, stdout=mock_json)

            change = load_change(repo, "bod-204-openspec-foundation")

        assert change is not None
        assert change.change_id == "bod-204-openspec-foundation"
        assert change.schema == "verdict-change-v1"
        assert "proposal.md" in change.artifacts


@pytest.mark.integration
@pytest.mark.skipif(
    subprocess.run(["which", "npx"], capture_output=True).returncode != 0,
    reason="npx not available",
)
def test_integration_openspec_cli_real():
    """Integration test: run the real pinned OpenSpec CLI."""
    # This test is skipped when npx is absent
    result = subprocess.run(
        ["npx", "-y", "@fission-ai/openspec@1.13.2", "--version"],
        capture_output=True,
        text=True,
        env={**subprocess.os.environ, "OPENSPEC_TELEMETRY": "0"},
    )

    assert result.returncode == 0
    assert "1.13.2" in result.stdout or "1.13.2" in result.stderr


def test_run_openspec_command_handles_file_not_found():
    """FileNotFoundError (npx missing) returns None."""
    from unittest import mock

    from verdict.openspec_lifecycle import _run_openspec_command

    with mock.patch("subprocess.run", side_effect=FileNotFoundError("npx not found")):
        result = _run_openspec_command(["--version"])
        assert result is None


def test_run_openspec_command_handles_os_error():
    """OSError returns None."""
    from unittest import mock

    from verdict.openspec_lifecycle import _run_openspec_command

    with mock.patch("subprocess.run", side_effect=OSError("execution failed")):
        result = _run_openspec_command(["--version"])
        assert result is None


def test_run_openspec_command_handles_timeout():
    """TimeoutExpired returns None."""
    import subprocess
    from unittest import mock

    from verdict.openspec_lifecycle import _run_openspec_command

    with mock.patch("subprocess.run", side_effect=subprocess.TimeoutExpired("cmd", 60)):
        result = _run_openspec_command(["--version"])
        assert result is None


def test_admit_reports_subprocess_errors_as_unavailable():
    """Subprocess errors (FileNotFoundError, OSError, timeout) map to openspec_unavailable."""
    from unittest import mock

    from verdict.openspec_lifecycle import admit_significant_change

    # Test FileNotFoundError
    with mock.patch("verdict.openspec_lifecycle._run_openspec_command", return_value=None):
        result = admit_significant_change(None, Path("/tmp"))
        assert result.admitted is False
        assert result.category == "openspec_unavailable"
        assert "FileNotFoundError" in result.reason or "unavailable" in result.reason.lower()
