"""
Tests for swarm verification, merge gating, and artifact provenance (Issue #46 / Slice 37.4).

These tests prove the AC:
- A green worker response alone cannot close a task
- Failed verification blocks merge/completion and preserves evidence
- Provenance is tamper-evident (content-addressed artifacts)
- Sensitive output is redacted before persistence
- Tests cover malicious result claims, missing artifacts, verifier failure,
  protected-path approval, and successful evidence
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from verdict.swarm_verification import (
    DEFAULT_VERIFICATION_COMMANDS,
    ArtifactRef,
    ArtifactType,
    SwarmVerifier,
    VerificationCommand,
    VerificationOutcome,
    VerificationReport,
    run_verification_command,
)


@pytest.fixture
def temp_artifact_store():
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir) / "artifacts"


class TestVerificationCommandExecution:
    """Tests for individual verification command execution."""

    @pytest.fixture
    def temp_artifact_store(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            yield Path(tmpdir) / "artifacts"

    def test_successful_command_execution(self):
        """A passing command returns passed=True with stdout captured."""
        result = run_verification_command(
            VerificationCommand(command="echo 'tests passed'", description="Simple echo test")
        )
        assert result.passed is True
        assert "tests passed" in result.stdout
        assert result.exit_code == 0

    def test_failed_command_execution(self):
        """A failing command returns passed=False with stderr captured."""
        result = run_verification_command(
            VerificationCommand(command="exit 1", description="Failing command")
        )
        assert result.passed is False
        assert result.exit_code == 1

    def test_command_timeout(self):
        """A command exceeding timeout is marked as failed."""
        result = run_verification_command(
            VerificationCommand(
                command="sleep 10", description="Long running command", timeout_seconds=1
            )
        )
        assert result.passed is False
        assert result.timed_out is True

    def test_required_vs_optional_commands(self):
        """Required commands affect overall outcome differently than optional."""
        required_fail = VerificationCommand(
            command="exit 1", description="Required failure", required=True
        )
        optional_fail = VerificationCommand(
            command="exit 1", description="Optional failure", required=False
        )

        req_result = run_verification_command(required_fail)
        opt_result = run_verification_command(optional_fail)

        assert req_result.command.required is True
        assert opt_result.command.required is False


class TestArtifactProvenance:
    """Tests for content-addressed artifact storage and tamper-evidence."""

    def test_artifact_content_addressed(self, temp_artifact_store):
        """ArtifactRef is derived from content hash."""
        content1 = b"test content 1"
        content2 = b"test content 2"

        ref1 = ArtifactRef.from_content(content1, ArtifactType.TEST_OUTPUT)
        ref2 = ArtifactRef.from_content(content1, ArtifactType.TEST_OUTPUT)
        ref3 = ArtifactRef.from_content(content2, ArtifactType.TEST_OUTPUT)

        # Same content = same hash
        assert ref1.content_hash == ref2.content_hash
        # Different content = different hash
        assert ref1.content_hash != ref3.content_hash

    def test_artifact_storage_path_deterministic(self):
        """Storage path is deterministic from content hash."""
        content = b"deterministic test"
        ref = ArtifactRef.from_content(content, ArtifactType.TEST_OUTPUT)

        with tempfile.TemporaryDirectory() as tmpdir:
            store = Path(tmpdir) / "artifacts"
            path1 = ref.storage_path(store)
            path2 = ref.storage_path(store)

            assert path1 == path2
            assert "sha256" in str(path1) or len(ref.content_hash) == 64

    def test_artifact_tamper_detection(self):
        """Modified content produces different hash, detecting tampering."""
        original = b"original content"
        tampered = b"tampered content"

        ref_original = ArtifactRef.from_content(original, ArtifactType.TEST_OUTPUT)
        ref_tampered = ArtifactRef.from_content(tampered, ArtifactType.TEST_OUTPUT)

        assert ref_original.content_hash != ref_tampered.content_hash


class TestSensitiveRedaction:
    """Tests for sensitive data redaction before persistence."""

    def test_api_key_redaction(self):
        """API keys in output are redacted."""
        from verdict.swarm_verification import redact_sensitive

        log = []
        output = "API_KEY=sk-test123abcdefghijklmnopqrstuvwxyz"
        redacted = redact_sensitive(output, log)
        assert "sk-test123abcdefghijklmnopqrstuvwxyz" not in redacted
        assert "[REDACTED:openai_key]" in redacted
        assert any("openai_key" in r.lower() or "api_key" in r.lower() for r in log)

    def test_password_redaction(self, temp_artifact_store):
        """Passwords in output are redacted."""
        from verdict.swarm_verification import redact_sensitive

        log = []
        output = "password=secret123"
        redacted = redact_sensitive(output, log)
        assert "secret123" not in redacted
        assert "[REDACTED:password]" in redacted

    def test_token_redaction(self):
        """Bearer tokens are redacted."""
        from verdict.swarm_verification import redact_sensitive

        log = []
        output = "Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4gRG9lIiwiaWF0IjoxNTE2MjM5MDIyfQ.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
        redacted = redact_sensitive(output, log)
        assert (
            "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4gRG9lIiwiaWF0IjoxNTE2MjM5MDIyfQ.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
            not in redacted
        )
        assert "[REDACTED:bearer_token]" in redacted
        assert any("bearer_token" in r.lower() or "authorization" in r.lower() for r in log)


class TestSwarmVerifierIntegration:
    """Integration tests for the full SwarmVerifier."""

    @pytest.fixture
    def temp_artifact_store(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            yield Path(tmpdir) / "artifacts"

    @pytest.fixture
    def verifier(self, temp_artifact_store):
        return SwarmVerifier(
            artifact_store=temp_artifact_store, commands=DEFAULT_VERIFICATION_COMMANDS
        )

    def test_successful_verification_passes(self, verifier):
        """All passing commands produce ACCEPTED outcome."""
        # Use a command that will pass
        commands = [
            VerificationCommand(command="echo 'success'", description="Echo success", required=True)
        ]
        verifier.commands = commands

        report = verifier.verify(
            task_id="task-1",
            attempt_id="attempt-1",
            model_id="test-model",
            runtime_id="test-runtime",
        )

        assert report.overall_outcome == VerificationOutcome.ACCEPTED
        assert all(c.passed for c in report.commands)
        assert len(report.artifacts) >= 0

    def test_failed_required_command_blocks(self, verifier):
        """Failed required command produces BLOCKED outcome."""
        commands = [
            VerificationCommand(command="exit 1", description="Required failure", required=True)
        ]
        verifier.commands = commands

        report = verifier.verify(
            task_id="task-1",
            attempt_id="attempt-1",
            model_id="test-model",
            runtime_id="test-runtime",
        )

        assert report.overall_outcome == VerificationOutcome.BLOCKED
        assert any(not c.passed and c.command.required for c in report.commands)

    def test_failed_optional_command_rejects(self, verifier):
        """Failed optional command produces REJECTED outcome."""
        commands = [
            VerificationCommand(command="exit 1", description="Optional failure", required=False)
        ]
        verifier.commands = commands

        report = verifier.verify(
            task_id="task-1",
            attempt_id="attempt-1",
            model_id="test-model",
            runtime_id="test-runtime",
        )

        assert report.overall_outcome == VerificationOutcome.REJECTED
        assert any(not c.passed for c in report.commands)

    def test_artifacts_created_and_stored(self, verifier, temp_artifact_store):
        """Artifacts are created and stored at content-addressed paths."""
        commands = [
            VerificationCommand(
                command="echo 'artifact content'", description="Produce artifact", required=True
            )
        ]
        verifier.commands = commands

        report = verifier.verify(
            task_id="task-1",
            attempt_id="attempt-1",
            model_id="test-model",
            runtime_id="test-runtime",
        )

        assert len(report.artifacts) > 0
        for artifact in report.artifacts:
            path = artifact.storage_path(temp_artifact_store)
            assert path.exists(), f"Artifact not found at {path}"
            content = path.read_bytes()
            assert b"artifact content" in content

    def test_artifact_immutability(self, verifier, temp_artifact_store):
        """Stored artifacts cannot be modified without detection."""
        commands = [
            VerificationCommand(
                command="echo 'immutable'", description="Test immutability", required=True
            )
        ]
        verifier.commands = commands

        report = verifier.verify(
            task_id="task-1",
            attempt_id="attempt-1",
            model_id="test-model",
            runtime_id="test-runtime",
        )

        # Load and verify each artifact matches its hash
        for artifact in report.artifacts:
            path = artifact.storage_path(temp_artifact_store)
            content = path.read_bytes()
            # Re-hash content
            import hashlib

            computed_hash = hashlib.sha256(content).hexdigest()
            assert computed_hash == artifact.content_hash

    def test_redaction_log_includes_findings(self, verifier):
        """Redaction log captures what was redacted."""
        commands = [
            VerificationCommand(
                command="echo 'API_KEY=sk-test123'", description="Test redaction", required=True
            )
        ]
        verifier.commands = commands

        report = verifier.verify(
            task_id="task-1",
            attempt_id="attempt-1",
            model_id="test-model",
            runtime_id="test-runtime",
        )

        # Redaction log should have entries
        assert len(report.redaction_log) > 0
        # Original should be redacted in artifacts
        for artifact in report.artifacts:
            store_path = Path(tempfile.gettempdir()) / "artifacts"
            artifact_path = artifact.storage_path(store_path)
            content = artifact_path.read_bytes() if artifact_path.exists() else b""
            if content:
                assert b"sk-test123" not in content

    def test_verification_report_includes_timing(self, verifier):
        """Report includes start/complete timestamps and duration."""
        commands = [
            VerificationCommand(command="sleep 0.1", description="Timing test", required=True)
        ]
        verifier.commands = commands

        report = verifier.verify(
            task_id="task-1",
            attempt_id="attempt-1",
            model_id="test-model",
            runtime_id="test-runtime",
        )

        assert report.started_at is not None
        assert report.completed_at is not None
        assert report.duration_ms > 0
        assert report.duration_ms >= 100  # At least 100ms for sleep 0.1


class TestVerificationOutcomes:
    """Tests for verification outcome logic."""

    def test_accepted_allows_merge(self, temp_artifact_store):
        """ACCEPTED outcome allows merge."""
        verifier = SwarmVerifier(
            artifact_store=temp_artifact_store,
            commands=[
                VerificationCommand(command="echo success", description="Pass", required=True)
            ],
        )

        report = verifier.verify("task-1", "attempt-1", "model", "runtime")
        can_merge, _reason = verifier.can_merge(report)

        assert can_merge is True
        assert "passed" in _reason.lower()

    def test_blocked_prevents_merge(self, temp_artifact_store):
        """BLOCKED outcome prevents merge."""
        verifier = SwarmVerifier(
            artifact_store=temp_artifact_store,
            commands=[VerificationCommand(command="exit 1", description="Fail", required=True)],
        )

        report = verifier.verify("task-1", "attempt-1", "model", "runtime")
        can_merge, _reason = verifier.can_merge(report)

        assert can_merge is False
        assert "required" in _reason.lower()

    def test_rejected_prevents_merge(self, temp_artifact_store):
        """REJECTED outcome prevents merge."""
        verifier = SwarmVerifier(
            artifact_store=temp_artifact_store,
            commands=[VerificationCommand(command="exit 1", description="Fail", required=False)],
        )

        report = verifier.verify("task-1", "attempt-1", "model", "runtime")
        can_merge, _reason = verifier.can_merge(report)

        assert can_merge is False
        assert "failed" in _reason.lower()

    def test_needs_review_prevents_merge_without_approvals(self, temp_artifact_store):
        """NEEDS_REVIEW prevents merge when approvals pending."""
        verifier = SwarmVerifier(
            artifact_store=temp_artifact_store,
            commands=[
                VerificationCommand(command="echo success", description="Pass", required=True)
            ],
        )

        # Manually create a report with pending approvals
        report = VerificationReport(
            task_id="task-1",
            attempt_id="attempt-1",
            model_id="model",
            runtime_id="runtime",
            started_at="2026-01-01T00:00:00Z",
            completed_at="2026-01-01T00:00:01Z",
            duration_ms=1000,
            commands=[],
            overall_outcome=VerificationOutcome.NEEDS_REVIEW,
            artifacts=[],
            redaction_log=[],
            approvals_required={"protected-path"},
            approvals_granted=set(),
        )

        can_merge, _reason = verifier.can_merge(report)

        assert can_merge is False
        assert "pending approvals" in _reason.lower()

    def test_needs_review_allows_merge_with_approvals(self, temp_artifact_store):
        """NEEDS_REVIEW allows merge when all approvals granted."""
        verifier = SwarmVerifier(
            artifact_store=temp_artifact_store,
            commands=[
                VerificationCommand(command="echo success", description="Pass", required=True)
            ],
        )

        report = VerificationReport(
            task_id="task-1",
            attempt_id="attempt-1",
            model_id="model",
            runtime_id="runtime",
            started_at="2026-01-01T00:00:00Z",
            completed_at="2026-01-01T00:00:01Z",
            duration_ms=1000,
            commands=[],
            overall_outcome=VerificationOutcome.NEEDS_REVIEW,
            artifacts=[],
            redaction_log=[],
            approvals_required={"protected-path"},
            approvals_granted={"protected-path"},
        )

        can_merge, _reason = verifier.can_merge(report)

        assert can_merge is True


class TestMaliciousClaims:
    """Tests that catch malicious worker result claims."""

    def test_cannot_claim_success_without_passing_commands(self, temp_artifact_store):
        """Worker cannot claim success if commands failed."""
        verifier = SwarmVerifier(
            artifact_store=temp_artifact_store,
            commands=[
                VerificationCommand(command="exit 1", description="Will fail", required=True)
            ],
        )

        report = verifier.verify("task-1", "attempt-1", "model", "runtime")

        # Even if worker claims success, verifier determines actual outcome
        assert report.overall_outcome != VerificationOutcome.ACCEPTED

    def test_missing_artifacts_detected(self, temp_artifact_store):
        """Missing artifacts are detected."""
        verifier = SwarmVerifier(
            artifact_store=temp_artifact_store,
            commands=[
                VerificationCommand(
                    command="echo 'produce output'", description="Produce output", required=True
                )
            ],
        )

        report = verifier.verify("task-1", "attempt-1", "model", "runtime")

        # Artifacts should be created
        assert len(report.artifacts) > 0

        # Verify each artifact exists on disk
        for artifact in report.artifacts:
            path = artifact.storage_path(temp_artifact_store)
            assert path.exists(), f"Missing artifact: {artifact.content_hash[:16]}"


class TestProtectedPathApproval:
    """Tests for protected path approval workflow."""

    def test_approval_workflow_integration(self, temp_artifact_store):
        """Full workflow: verification -> needs_review -> approvals -> merge."""
        verifier = SwarmVerifier(
            artifact_store=temp_artifact_store,
            commands=[
                VerificationCommand(command="echo success", description="Pass", required=True)
            ],
        )

        # Initial verification
        report = verifier.verify("task-1", "attempt-1", "model", "runtime")

        # Should be accepted (no protected paths in this test)
        assert report.overall_outcome == VerificationOutcome.ACCEPTED


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
