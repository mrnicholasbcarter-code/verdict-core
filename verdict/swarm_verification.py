"""
Swarm verification, merge gating, and artifact provenance (Issue #46 / Slice 37.4).

This module implements:
- Verification stage that runs declared commands in constrained environment
- Provenance tracking with content-addressed artifacts
- Merge gating with approval requirements for protected paths
- Evidence preservation for failed verifications
- Redaction of sensitive output before persistence
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any


class VerificationOutcome(str, Enum):
    """Outcome of verification stage."""
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    BLOCKED = "blocked"
    NEEDS_REVIEW = "needs_review"


class ArtifactType(str, Enum):
    """Types of artifacts produced by swarm tasks."""
    PATCH = "patch"
    TEST_OUTPUT = "test_output"
    DIFF_STATS = "diff_stats"
    MODEL_RUNTIME_ID = "model_runtime_id"
    ATTEMPT_COUNT = "attempt_count"
    TIMESTAMPS = "timestamps"
    POLICY_VERSION = "policy_version"
    SCHEMA_VERSION = "schema_version"


@dataclass(frozen=True)
class ArtifactRef:
    """Content-addressed reference to an artifact."""
    content_hash: str  # SHA256 of content
    artifact_type: ArtifactType
    size_bytes: int
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_content(cls, content: bytes, artifact_type: ArtifactType, **metadata: Any) -> ArtifactRef:
        """Create artifact reference from raw content."""
        content_hash = hashlib.sha256(content).hexdigest()
        return cls(
            content_hash=content_hash,
            artifact_type=artifact_type,
            size_bytes=len(content),
            metadata=metadata,
        )

    def storage_path(self, base: Path) -> Path:
        """Get storage path for this artifact."""
        return base / "artifacts" / self.artifact_type.value / f"{self.content_hash[:16]}.bin"


@dataclass(frozen=True)
class VerificationCommand:
    """A verification command to execute."""
    command: str
    description: str
    timeout_seconds: int = 60
    required: bool = True  # If false, failure is warning not block
    env: dict[str, str] = field(default_factory=dict)
    working_dir: str | None = None


@dataclass(frozen=True)
class VerificationResult:
    """Result of a single verification command."""
    command: VerificationCommand
    exit_code: int
    stdout: str
    stderr: str
    duration_ms: int
    passed: bool
    timed_out: bool = False
    artifact_refs: list[ArtifactRef] = field(default_factory=list)


@dataclass(frozen=True)
class VerificationReport:
    """Complete verification report for a swarm task attempt."""
    task_id: str
    attempt_id: str
    model_id: str
    runtime_id: str
    started_at: str
    completed_at: str
    duration_ms: int
    commands: list[VerificationResult]
    overall_outcome: VerificationOutcome
    artifacts: list[ArtifactRef] = field(default_factory=list)
    redaction_log: list[str] = field(default_factory=list)
    approvals_required: list[str] = field(default_factory=list)
    approvals_granted: list[str] = field(default_factory=list)


# Redaction patterns for sensitive content
_REDACTION_PATTERNS = [
    # OpenAI keys (sk-...) - must come before api_key
    (re.compile(r"sk-[\w-]{20,}", re.I), "openai_key"),
    # Bearer tokens - must come before authorization
    (re.compile(r"bearer\s+[\w.-]+", re.I), "bearer_token"),
    # API keys - various formats
    (re.compile(r"api[_-]?key\s*[:=]\s*[\w-]+", re.I), "api_key"),
    (re.compile(r"secret\s*[:=]\s*[\w-]+", re.I), "secret"),
    (re.compile(r"password\s*[:=]\s*\S+", re.I), "password"),
    (re.compile(r"token\s*[:=]\s*[\w.-]+", re.I), "token"),
    # Authorization headers - must come after bearer_token
    (re.compile(r"authorization\s*[:=]\s*[Bb]earer\s+[\w.-]+", re.I), "authorization"),
    (re.compile(r"gh[pousr]_[A-Za-z0-9_]{36,}", re.I), "github_token"),
    (re.compile(r"aws[_-]?access[_-]?key\s*[:=]\s*[A-Z0-9]{20}", re.I), "aws_key"),
    (re.compile(r"private[_-]?key\s*[:=]\s*[\w-]+", re.I), "private_key"),
]

# Protected paths requiring approval
_PROTECTED_PATHS = frozenset({
    ".github/workflows/",
    "docker/",
    "k8s/",
    "terraform/",
    "helm/",
    "scripts/deploy",
    "scripts/migrate",
    "requirements.txt",
    "pyproject.toml",
    "package.json",
    "Cargo.toml",
    "go.mod",
    ".env",
    ".env.*",
    "secrets/",
    "credentials/",
})


def redact_sensitive(content: str, log: list[str] | None = None) -> str:
    """Redact sensitive content from output."""
    redacted = content
    for pattern, label in _REDACTION_PATTERNS:
        matches = pattern.findall(redacted)
        if matches:
            redacted = pattern.sub(f"[REDACTED:{label}]", redacted)
            if log is not None:
                log.append(f"Redacted {len(matches)} {label}(s)")
    return redacted


def is_protected_path(path: str) -> bool:
    """Check if a path is protected and requires approval."""
    return any(path.startswith(p) or p in path for p in _PROTECTED_PATHS)


def run_verification_command(cmd: VerificationCommand) -> VerificationResult:
    """Execute a verification command and return result."""
    start = time.perf_counter()
    timed_out = False
    try:
        result = subprocess.run(
            cmd.command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=cmd.timeout_seconds,
            env={**os.environ, **cmd.env} if cmd.env else None,
            cwd=cmd.working_dir,
        )
        exit_code = result.returncode
        stdout = result.stdout
        stderr = result.stderr
    except subprocess.TimeoutExpired as e:
        timed_out = True
        exit_code = -1
        stdout = e.stdout.decode() if e.stdout else ""
        stderr = f"Timeout after {cmd.timeout_seconds}s: {e.stderr.decode() if e.stderr else ''}"
    except Exception as e:
        exit_code = -1
        stdout = ""
        stderr = str(e)

    duration_ms = int((time.perf_counter() - start) * 1000)
    passed = exit_code == 0 and not timed_out

    return VerificationResult(
        command=cmd,
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        duration_ms=duration_ms,
        passed=passed,
        timed_out=timed_out,
    )


class SwarmVerifier:
    """
    Runs verification stage for swarm task attempts.

    Captures artifacts, redacts sensitive output, tracks provenance,
    and enforces merge gating rules.
    """

    def __init__(
        self,
        artifact_store: Path,
        commands: list[VerificationCommand],
        require_approval_for_protected: bool = True,
    ):
        self.artifact_store = artifact_store
        self.commands = commands
        self.require_approval = require_approval_for_protected
        self.artifact_store.mkdir(parents=True, exist_ok=True)

    def verify_attempt(
        self,
        task_id: str,
        attempt_id: str,
        model_id: str,
        runtime_id: str,
        changed_files: list[str] | None = None,
    ) -> VerificationReport:
        """
        Run all verification commands for an attempt.

        Args:
            task_id: Task identifier
            attempt_id: Attempt identifier
            model_id: Model used for the attempt
            runtime_id: Runtime identifier
            changed_files: List of files modified by this attempt

        Returns:
            Complete verification report
        """
        started = datetime.now(timezone.utc)
        redaction_log: list[str] = []
        artifacts: list[ArtifactRef] = []
        command_results: list[VerificationResult] = []
        approvals_required: list[str] = []
        approvals_granted: list[str] = []

        # Check for protected paths requiring approval
        if changed_files:
            for f in changed_files:
                if is_protected_path(f) and self.require_approval:
                    approvals_required.append(f)

        # Run each verification command
        for cmd in self.commands:
            result = run_verification_command(cmd)

            # Redact stdout/stderr
            redacted_stdout = redact_sensitive(result.stdout, redaction_log)
            redacted_stderr = redact_sensitive(result.stderr, redaction_log)

            # Create new result with redacted output
            result = VerificationResult(
                command=result.command,
                exit_code=result.exit_code,
                stdout=redacted_stdout,
                stderr=redacted_stderr,
                duration_ms=result.duration_ms,
                passed=result.passed,
                timed_out=result.timed_out,
                artifact_refs=result.artifact_refs,
            )

            # Store output as artifacts
            if result.stdout:
                artifact = ArtifactRef.from_content(
                    result.stdout.encode(),
                    ArtifactType.TEST_OUTPUT,
                    task_id=task_id,
                    attempt_id=attempt_id,
                    command=cmd.description,
                )
                self._store_artifact(artifact, result.stdout.encode())
                result.artifact_refs.append(artifact)
                artifacts.append(artifact)

            if result.stderr:
                artifact = ArtifactRef.from_content(
                    result.stderr.encode(),
                    ArtifactType.TEST_OUTPUT,
                    task_id=task_id,
                    attempt_id=attempt_id,
                    command=f"{cmd.description} (stderr)",
                )
                self._store_artifact(artifact, result.stderr.encode())
                result.artifact_refs.append(artifact)
                artifacts.append(artifact)

            command_results.append(result)

        # Determine overall outcome
        failed_required = any(not r.passed and r.command.required for r in command_results)
        failed_any = any(not r.passed for r in command_results)

        if failed_required:
            outcome = VerificationOutcome.BLOCKED
        elif failed_any:
            outcome = VerificationOutcome.REJECTED
        elif approvals_required:
            outcome = VerificationOutcome.NEEDS_REVIEW
        else:
            outcome = VerificationOutcome.ACCEPTED

        completed = datetime.now(timezone.utc)

        return VerificationReport(
            task_id=task_id,
            attempt_id=attempt_id,
            model_id=model_id,
            runtime_id=runtime_id,
            started_at=started.isoformat(),
            completed_at=completed.isoformat(),
            duration_ms=int((completed - started).total_seconds() * 1000),
            commands=command_results,
            overall_outcome=outcome,
            artifacts=artifacts,
            redaction_log=redaction_log,
            approvals_required=approvals_required,
            approvals_granted=approvals_granted,
        )

    def verify(self, *args: Any, **kwargs: Any) -> VerificationReport:
        """Alias for verify_attempt to match test expectations."""
        return self.verify_attempt(*args, **kwargs)

    def _store_artifact(self, ref: ArtifactRef, content: bytes) -> None:
        """Store artifact content at its content-addressed path."""
        path = ref.storage_path(self.artifact_store)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)

    def load_artifact(self, ref: ArtifactRef) -> bytes | None:
        """Load artifact content by reference."""
        path = ref.storage_path(self.artifact_store)
        if path.exists():
            return path.read_bytes()
        return None

    def can_merge(self, report: VerificationReport) -> tuple[bool, str]:
        """
        Determine if a verification report allows merge.

        Returns:
            (can_merge, reason)
        """
        if report.overall_outcome == VerificationOutcome.BLOCKED:
            return False, "Required verification commands failed"
        if report.overall_outcome == VerificationOutcome.REJECTED:
            return False, "Verification commands failed"
        if report.overall_outcome == VerificationOutcome.NEEDS_REVIEW:
            pending = set(report.approvals_required) - set(report.approvals_granted)
            if pending:
                return False, f"Pending approvals for: {', '.join(sorted(pending))}"
        return True, "Verification passed"


# Default verification commands for common project types
DEFAULT_VERIFICATION_COMMANDS = [
    VerificationCommand(
        command="python -m pytest --tb=short -q",
        description="Run test suite",
        timeout_seconds=120,
        required=True,
    ),
    VerificationCommand(
        command="python -m ruff check .",
        description="Lint with ruff",
        timeout_seconds=30,
        required=True,
    ),
    VerificationCommand(
        command="python -m mypy --strict verdict/",
        description="Type check with mypy",
        timeout_seconds=60,
        required=True,
    ),
    VerificationCommand(
        command="code-review-graph detect-changes",
        description="Analyze code changes for risk",
        timeout_seconds=60,
        required=False,
    ),
]


def build_default_verifier(artifact_store: Path) -> SwarmVerifier:
    """Build a verifier with default commands for the verdict-core project."""
    return SwarmVerifier(artifact_store=artifact_store, commands=DEFAULT_VERIFICATION_COMMANDS)
