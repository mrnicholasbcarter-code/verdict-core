"""Strict version-1 shared JSON contracts for planning and routing."""

from __future__ import annotations

from dataclasses import MISSING, asdict, dataclass, field, fields
from typing import Any, ClassVar, TypeVar, cast


class ContractValidationError(ValueError):
    """Raised for unknown fields, missing fields, or secret-bearing payloads."""


_SECRET_NAMES = {"api_key", "apikey", "authorization", "password", "secret", "token"}
T = TypeVar("T", bound="Contract")


class Contract:
    """Base for strict, JSON-compatible v1 contracts."""

    contract_version: ClassVar[str] = "1"

    @classmethod
    def from_dict(cls: type[T], payload: dict[str, Any]) -> T:
        if not isinstance(payload, dict):
            raise ContractValidationError("contract must be a JSON object")
        _reject_secrets(payload)
        allowed = {item.name for item in fields(cast(Any, cls))}
        unknown = set(payload) - allowed
        if unknown:
            raise ContractValidationError(f"unknown field(s): {', '.join(sorted(unknown))}")
        missing = [
            item.name
            for item in fields(cast(Any, cls))
            if item.default is MISSING
            and item.default_factory is MISSING
            and item.name not in payload
        ]
        if missing:
            raise ContractValidationError(f"missing required field(s): {', '.join(missing)}")
        return cls(**payload)

    def to_dict(self) -> dict[str, Any]:
        return asdict(cast(Any, self))


@dataclass(frozen=True)
class CapabilityRequirement(Contract):
    capability: str
    required: bool = True
    minimum_level: str | None = None
    reason: str | None = None
    schema_version: str = "1"


@dataclass(frozen=True)
class TaskSpec(Contract):
    objective: str
    task_type: str
    effort: str = "unknown"
    reasoning: str = "unknown"
    capabilities: list[Any] = field(default_factory=list)
    required_capabilities: list[str] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)
    context: dict[str, Any] | None = None
    context_requirements: dict[str, Any] = field(default_factory=dict)
    tool_requirements: dict[str, bool] = field(default_factory=dict)
    privacy: str = "unknown"
    risk: str = "unknown"
    budget: dict[str, Any] = field(default_factory=dict)
    latency: dict[str, Any] | None = None
    latency_limit_ms: int | None = None
    workflow: dict[str, Any] | None = None
    approvals: list[str] = field(default_factory=list)
    criticality: str = "unknown"
    verification: dict[str, Any] | None = None
    parallelism: str = "serial"
    destructive_operation: bool = False
    production_impact: bool = False
    degraded_mode_policy: str = "deny"
    metadata: dict[str, Any] = field(default_factory=dict)
    schema_version: str = "1"


@dataclass(frozen=True)
class RuntimeCandidate(Contract):
    runtime_id: str
    catalog_present: bool
    live_eligible: bool
    availability: str
    signals: dict[str, dict[str, Any]]
    capabilities: list[str] = field(default_factory=list)
    provider: str | None = None
    model: str | None = None
    model_version: str | None = None
    schema_version: str = "1"


@dataclass(frozen=True)
class AvailabilitySnapshot(Contract):
    observed_at: str
    state: str = "unknown"
    signals: dict[str, dict[str, Any]] = field(default_factory=dict)
    candidates: list[RuntimeCandidate | dict[str, Any]] = field(default_factory=list)
    source: str = "unknown"
    ttl_seconds: int = 60
    expires_at: str | None = None
    schema_version: str = "1"


@dataclass(frozen=True)
class VerificationPlan(Contract):
    checks: list[Any] = field(default_factory=list)
    on_failure: str = "deny"
    schema_version: str = "1"


@dataclass(frozen=True)
class WorkflowPlan(Contract):
    steps: list[dict[str, Any]] = field(default_factory=list)
    plan_id: str | None = None
    verification: VerificationPlan | dict[str, Any] = field(default_factory=VerificationPlan)
    verification_plan_id: str | None = None
    fallback_allowed: bool = False
    fallback_plan: list[dict[str, Any]] = field(default_factory=list)
    policy_version: str = "1"
    metadata: dict[str, Any] = field(default_factory=dict)
    schema_version: str = "1"


@dataclass(frozen=True)
class RoutingDecisionContract(Contract):
    selected_route: dict[str, Any] = field(default_factory=dict)
    task_spec: dict[str, Any] = field(default_factory=dict)
    candidate_snapshot: str | dict[str, Any] | None = None
    exclusions: list[dict[str, Any]] = field(default_factory=list)
    policy_floor: str = "none"
    planner_mode: str = "default"
    explanation: str = ""
    adaptive_influence: dict[str, Any] = field(default_factory=dict)
    fallback_plan: list[dict[str, Any]] = field(default_factory=list)
    correlation_id: str | None = None
    request_id: str | None = None
    policy_version: str = "1"
    schema_version: str = "1"


RoutingDecision = RoutingDecisionContract


@dataclass(frozen=True)
class FallbackAttempt(Contract):
    runtime_id: str
    reason: str
    legal: bool = True
    schema_version: str = "1"


@dataclass(frozen=True)
class OutcomeEvent(Contract):
    event_id: str | None = None
    event_type: str | None = None
    correlation_id: str | None = None
    outcome: str | None = None
    occurred_at: str | None = None
    request_id: str | None = None
    verification: dict[str, Any] = field(default_factory=dict)
    quality: dict[str, Any] = field(default_factory=dict)
    latency_ms: float | None = None
    cost: dict[str, Any] = field(default_factory=dict)
    retries: int = 0
    fallbacks: list[Any] = field(default_factory=list)
    provider_version: str | None = None
    model_version: str | None = None
    details: dict[str, Any] | None = None
    schema_version: str = "1"


@dataclass(frozen=True)
class LearningEvent(Contract):
    event_id: str | None = None
    signal: str = ""
    correlation_id: str | None = None
    value: Any = None
    occurred_at: str | None = None
    evidence: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    request_id: str | None = None
    schema_version: str = "1"


_CONTRACTS: dict[str, type[Contract]] = {
    name: cls
    for name, cls in {
        "task_spec": TaskSpec,
        "TaskSpec": TaskSpec,
        "capability_requirement": CapabilityRequirement,
        "CapabilityRequirement": CapabilityRequirement,
        "runtime_candidate": RuntimeCandidate,
        "RuntimeCandidate": RuntimeCandidate,
        "availability_snapshot": AvailabilitySnapshot,
        "AvailabilitySnapshot": AvailabilitySnapshot,
        "workflow_plan": WorkflowPlan,
        "WorkflowPlan": WorkflowPlan,
        "routing_decision": RoutingDecisionContract,
        "RoutingDecision": RoutingDecisionContract,
        "RoutingDecisionContract": RoutingDecisionContract,
        "fallback_attempt": FallbackAttempt,
        "FallbackAttempt": FallbackAttempt,
        "verification_plan": VerificationPlan,
        "VerificationPlan": VerificationPlan,
        "outcome_event": OutcomeEvent,
        "OutcomeEvent": OutcomeEvent,
        "learning_event": LearningEvent,
        "LearningEvent": LearningEvent,
    }.items()
}


def contract_from_dict(name: str, payload: dict[str, Any]) -> Contract:
    try:
        return _CONTRACTS[name].from_dict(payload)
    except KeyError as exc:
        raise ContractValidationError(f"unknown contract: {name}") from exc


def _reject_secrets(value: Any) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = key.lower().replace("-", "_")
            if (
                normalized in _SECRET_NAMES
                or normalized.endswith("_token")
                or normalized.endswith("_secret")
            ):
                raise ContractValidationError(f"secret-bearing field rejected: {key}")
            _reject_secrets(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            _reject_secrets(child)
