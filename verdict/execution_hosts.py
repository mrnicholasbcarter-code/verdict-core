"""Typed, redacted contracts for safe execution-host integrations.

The contracts in this module describe the boundary between Verdict and an
external execution host. They do not detect binaries, invoke processes, or
authorize side effects. Host implementations must provide those operations
behind this boundary and remain subject to normal repository and CI gates.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol, runtime_checkable

PROTOCOL_VERSION = "execution-host/v1"
_SECRET_MARKERS = (
    "api_key",
    "apikey",
    "authorization",
    "bearer ",
    "password",
    "private_key",
    "secret",
    "token",
    "sk-",
)


def _text(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value.strip()


def _safe_text(value: str, field_name: str) -> str:
    result = _text(value, field_name)
    lowered = result.lower()
    if any(marker in lowered for marker in _SECRET_MARKERS):
        raise ValueError(f"{field_name} contains credential material")
    if any(character in result for character in ("\n", "\r", "\x00")):
        raise ValueError(f"{field_name} contains control characters")
    return result


class HostId(str, Enum):
    """Initially supported host identities."""

    CODEX = "codex"
    CLAUDE_CODE = "claude-code"
    PI = "pi"


class HostLifecycle(str, Enum):
    """Observable lifecycle states for one host execution."""

    PLANNED = "planned"
    ASSIGNED = "assigned"
    EXECUTING = "executing"
    VERIFYING = "verifying"
    FAILED = "failed"
    CANCELLED = "cancelled"
    COMPLETE = "complete"


class TerminationReason(str, Enum):
    """Bounded reasons an execution may stop."""

    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMEOUT = "timeout"
    OUTPUT_TRUNCATED = "output_truncated"
    PROCESS_EXITED = "process_exited"
    POLICY_REJECTED = "policy_rejected"


@dataclass(frozen=True)
class HostCapabilities:
    """Capabilities declared by a host adapter, not inferred by name."""

    supports_detection: bool = True
    supports_invocation: bool = False
    supports_cancellation: bool = False
    supports_configuration: bool = False
    declared_operations: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        operations = tuple(
            sorted({_safe_text(item, "declared operation") for item in self.declared_operations})
        )
        object.__setattr__(self, "declared_operations", operations)
        if self.supports_invocation and "invoke" not in operations:
            raise ValueError("invocation capability requires an invoke operation")
        if self.supports_cancellation and "cancel" not in operations:
            raise ValueError("cancellation capability requires a cancel operation")


@dataclass(frozen=True)
class HostDescriptor:
    """Redacted host identity and health observation."""

    host_id: HostId
    adapter_version: str
    available: bool
    version: str | None = None
    health: str = "unknown"
    limitations: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _text(self.adapter_version, "adapter_version")
        if self.version is not None:
            _safe_text(self.version, "version")
        if self.health not in {"healthy", "degraded", "unavailable", "unknown"}:
            raise ValueError("health must be healthy, degraded, unavailable, or unknown")
        object.__setattr__(
            self, "limitations", tuple(_safe_text(item, "limitation") for item in self.limitations)
        )


@dataclass(frozen=True)
class ExecutionBudget:
    """Hard execution limits shown before an invocation."""

    max_cost_usd: float | None = None
    max_tokens: int | None = None
    timeout_ms: int = 300_000
    max_output_bytes: int = 1_000_000
    max_fan_out: int = 1

    def __post_init__(self) -> None:
        if self.max_cost_usd is not None and self.max_cost_usd < 0:
            raise ValueError("max_cost_usd must be non-negative")
        if self.max_tokens is not None and self.max_tokens < 0:
            raise ValueError("max_tokens must be non-negative")
        if self.timeout_ms <= 0 or self.max_output_bytes <= 0 or self.max_fan_out <= 0:
            raise ValueError("execution limits must be positive")


@dataclass(frozen=True)
class ExecutionPreview:
    """Deterministic, secret-free preview required before execution."""

    host_id: HostId
    provider: str
    model: str
    repository: str
    worktree: str
    permissions: tuple[str, ...] = ()
    budget: ExecutionBudget = field(default_factory=ExecutionBudget)
    objective_digest: str = ""
    lifecycle: HostLifecycle = HostLifecycle.PLANNED
    consent_required: bool = True

    def __post_init__(self) -> None:
        for name, value in (
            ("provider", self.provider),
            ("model", self.model),
            ("repository", self.repository),
            ("worktree", self.worktree),
        ):
            _safe_text(value, name)
        if not self.objective_digest:
            raise ValueError("objective_digest is required; raw objectives are not preview data")
        if not self.objective_digest.startswith("sha256:") or len(self.objective_digest) != 71:
            raise ValueError("objective_digest must be a sha256 digest")
        object.__setattr__(
            self,
            "permissions",
            tuple(sorted({_safe_text(item, "permission") for item in self.permissions})),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": PROTOCOL_VERSION,
            "host": self.host_id.value,
            "provider": self.provider,
            "model": self.model,
            "repository": self.repository,
            "worktree": self.worktree,
            "permissions": list(self.permissions),
            "budget": {
                "max_cost_usd": self.budget.max_cost_usd,
                "max_tokens": self.budget.max_tokens,
                "timeout_ms": self.budget.timeout_ms,
                "max_output_bytes": self.budget.max_output_bytes,
                "max_fan_out": self.budget.max_fan_out,
            },
            "objective_digest": self.objective_digest,
            "lifecycle": self.lifecycle.value,
            "consent_required": self.consent_required,
        }


@dataclass(frozen=True)
class ExecutionResult:
    """Redacted result envelope returned by an execution host."""

    execution_id: str
    host_id: HostId
    lifecycle: HostLifecycle
    termination: TerminationReason
    success: bool
    output_digest: str | None = None
    output_bytes: int = 0
    error_class: str | None = None
    artifact_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _safe_text(self.execution_id, "execution_id")
        if self.output_bytes < 0:
            raise ValueError("output_bytes must be non-negative")
        if self.lifecycle not in {
            HostLifecycle.FAILED,
            HostLifecycle.CANCELLED,
            HostLifecycle.COMPLETE,
        }:
            raise ValueError("result lifecycle must be terminal")
        if self.success != (
            self.lifecycle is HostLifecycle.COMPLETE
            and self.termination is TerminationReason.COMPLETED
        ):
            raise ValueError("success must agree with terminal lifecycle and termination")
        if self.output_digest is not None and (
            not self.output_digest.startswith("sha256:") or len(self.output_digest) != 71
        ):
            raise ValueError("output_digest must be a sha256 digest")
        object.__setattr__(
            self,
            "artifact_refs",
            tuple(_safe_text(item, "artifact_ref") for item in self.artifact_refs),
        )


@runtime_checkable
class ExecutionHostAdapter(Protocol):
    """Side-effect boundary implemented by a concrete host adapter."""

    host_id: HostId
    adapter_version: str
    capabilities: HostCapabilities

    def detect(self) -> HostDescriptor:
        """Run only adapter-declared, bounded detection probes."""

    def preview(
        self,
        objective: str,
        *,
        repository: str,
        worktree: str,
        model: str,
        provider: str,
        budget: ExecutionBudget,
        permissions: Sequence[str] = (),
    ) -> ExecutionPreview:
        """Create a redacted preview without invoking the host."""

    def invoke(self, preview: ExecutionPreview) -> ExecutionResult:
        """Execute only after consent and policy validation."""

    def cancel(self, execution_id: str) -> ExecutionResult:
        """Request bounded cancellation and return a terminal/result envelope."""


def objective_digest(objective: str) -> str:
    """Return a stable digest suitable for a preview instead of raw prompt text."""

    normalized = _text(objective, "objective")
    return "sha256:" + hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def build_execution_preview(objective: str, **kwargs: Any) -> ExecutionPreview:
    """Build a canonical preview from an objective without retaining the objective."""

    return ExecutionPreview(objective_digest=objective_digest(objective), **kwargs)


def canonical_preview_digest(preview: ExecutionPreview) -> str:
    """Hash the canonical preview for evidence correlation."""

    payload = json.dumps(preview.to_dict(), sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def validate_adapter_capabilities(adapter: ExecutionHostAdapter) -> tuple[str, ...]:
    """Return deterministic capability issues before an adapter can run."""

    issues: list[str] = []
    if adapter.capabilities.supports_invocation and not callable(getattr(adapter, "invoke", None)):
        issues.append("invocation is declared but invoke is missing")
    if adapter.capabilities.supports_cancellation and not callable(
        getattr(adapter, "cancel", None)
    ):
        issues.append("cancellation is declared but cancel is missing")
    return tuple(issues)


__all__ = [
    "PROTOCOL_VERSION",
    "ExecutionBudget",
    "ExecutionHostAdapter",
    "ExecutionPreview",
    "ExecutionResult",
    "HostCapabilities",
    "HostDescriptor",
    "HostId",
    "HostLifecycle",
    "TerminationReason",
    "build_execution_preview",
    "canonical_preview_digest",
    "objective_digest",
    "validate_adapter_capabilities",
]
