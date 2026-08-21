"""Tests for the redacted execution-host boundary contract."""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from verdict.execution_hosts import (
    PROTOCOL_VERSION,
    ExecutionBudget,
    ExecutionHostAdapter,
    ExecutionPreview,
    ExecutionResult,
    HostCapabilities,
    HostDescriptor,
    HostId,
    HostLifecycle,
    TerminationReason,
    build_execution_preview,
    canonical_preview_digest,
    objective_digest,
    validate_adapter_capabilities,
)


def test_preview_is_redacted_deterministic_and_bounded() -> None:
    kwargs = dict(
        host_id=HostId.CODEX,
        provider="omniroute",
        model="cx/gpt-5.6-sol",
        repository="verdict-core",
        worktree="feature/worktree",
        permissions=("read_repo", "write_worktree"),
        budget=ExecutionBudget(timeout_ms=10_000, max_fan_out=2),
    )
    first = build_execution_preview("do not publish this objective", **kwargs)
    second = build_execution_preview("do not publish this objective", **kwargs)

    assert first == second
    assert first.lifecycle is HostLifecycle.PLANNED
    assert "do not publish" not in str(first.to_dict())
    assert first.to_dict()["schema_version"] == PROTOCOL_VERSION
    assert canonical_preview_digest(first) == canonical_preview_digest(second)


def test_preview_rejects_raw_credentials_and_unhashed_objective() -> None:
    with pytest.raises(ValueError, match="credential"):
        build_execution_preview(
            "safe objective",
            host_id=HostId.PI,
            provider="api_key=secret",
            model="model",
            repository="repo",
            worktree="worktree",
        )
    with pytest.raises(ValueError, match="objective_digest"):
        ExecutionPreview(
            host_id=HostId.PI,
            provider="provider",
            model="model",
            repository="repo",
            worktree="worktree",
        )


def test_capabilities_require_matching_declared_operations() -> None:
    with pytest.raises(ValueError, match="invoke operation"):
        HostCapabilities(supports_invocation=True)
    with pytest.raises(ValueError, match="cancel operation"):
        HostCapabilities(supports_cancellation=True)


def test_descriptor_and_budget_fail_closed() -> None:
    with pytest.raises(ValueError, match="health"):
        HostDescriptor(HostId.CLAUDE_CODE, "adapter/v1", True, health="ready")
    with pytest.raises(ValueError, match="positive"):
        ExecutionBudget(timeout_ms=0)


def test_result_requires_terminal_truthful_state() -> None:
    with pytest.raises(ValueError, match="terminal"):
        ExecutionResult(
            "execution-1", HostId.CODEX, HostLifecycle.EXECUTING, TerminationReason.COMPLETED, True
        )
    with pytest.raises(ValueError, match="agree"):
        ExecutionResult(
            "execution-1", HostId.CODEX, HostLifecycle.FAILED, TerminationReason.FAILED, True
        )


@dataclass
class _FixtureAdapter:
    host_id: HostId = HostId.CODEX
    adapter_version: str = "fixture/v1"
    capabilities: HostCapabilities = field(
        default_factory=lambda: HostCapabilities(
            supports_invocation=True,
            supports_cancellation=True,
            declared_operations=("cancel", "invoke"),
        )
    )

    def detect(self) -> HostDescriptor:
        return HostDescriptor(self.host_id, self.adapter_version, True, health="healthy")

    def preview(self, *args: object, **kwargs: object) -> ExecutionPreview:
        raise NotImplementedError

    def invoke(self, preview: ExecutionPreview) -> ExecutionResult:
        raise NotImplementedError

    def cancel(self, execution_id: str) -> ExecutionResult:
        raise NotImplementedError


def test_adapter_protocol_and_capability_validation() -> None:
    adapter = _FixtureAdapter()
    assert isinstance(adapter, ExecutionHostAdapter)
    assert adapter.detect().health == "healthy"
    assert validate_adapter_capabilities(adapter) == ()


def test_objective_digest_is_stable_without_plaintext() -> None:
    digest = objective_digest("private objective")
    assert digest.startswith("sha256:")
    assert "private objective" not in digest
