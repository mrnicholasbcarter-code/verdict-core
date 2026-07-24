"""Strict version-1 shared JSON contracts for planning and routing."""

from __future__ import annotations

import json
import math
from dataclasses import MISSING, Field, dataclass, field, fields
from types import UnionType
from typing import Any, ClassVar, TypeVar, cast, get_args, get_origin, get_type_hints

from verdict.security import fingerprint_text, redact_text


class ContractValidationError(ValueError):
    """Raised for unknown fields, missing fields, or secret-bearing payloads."""


_SECRET_NAMES = {"api_key", "apikey", "authorization", "password", "secret", "token"}

# These are the safety-sensitive values that v1 intentionally freezes.  Task
# types, planner modes, provider identifiers, and metadata remain open strings
# so adding a new workflow integration does not require a contract version bump.
_TASK_EFFORTS = frozenset({"unknown", "low", "medium", "high"})
_TASK_REASONING_LEVELS = frozenset({"unknown", "low", "medium", "high"})
_TASK_PRIVACY_LEVELS = frozenset(
    {"unknown", "public", "internal", "trusted_upstream", "restricted"}
)
_TASK_RISK_LEVELS = frozenset({"unknown", "low", "medium", "high", "critical"})
_TASK_PARALLELISM = frozenset({"serial", "parallel", "bounded"})
_DEGRADED_MODE_POLICIES = frozenset({"deny", "allow", "allow_with_penalty"})
_POLICY_FLOORS = frozenset(
    {"none", "isolated", "protected", "standard", "best_effort", "medium", "high"}
)
_AVAILABILITY_STATES = frozenset(
    {
        "eligible",
        "ready",
        "healthy",
        "outage",
        "degraded",
        "unknown",
        "unavailable",
        "denied",
        "quota_exhausted",
        "rate_limited",
        "unauthorized",
        "locked_out",
        "circuit_open",
        "timeout",
        "malformed",
        "capability_mismatch",
        "policy_denied",
    }
)
_WORKFLOW_ACTIONS = frozenset(
    {
        "answer",
        "research",
        "implement",
        "review",
        "verify",
        "synthesis",
        "specialist",
        "human_approval",
        "execute",
    }
)
_OUTCOME_VALUES = frozenset(
    {
        "success",
        "failure",
        "partial",
        "denied",
        "unknown",
        "cancelled",
        "timeout",
        "error",
        "skipped",
    }
)
_REQUIRED_FIELDS: dict[str, frozenset[str]] = {
    "TaskSpec": frozenset({"objective", "task_type"}),
    "WorkflowPlan": frozenset({"steps"}),
    "RoutingDecisionContract": frozenset({"selected_route"}),
    "OutcomeEvent": frozenset({"event_type", "outcome", "occurred_at"}),
}
_BUDGET_FIELDS = frozenset(
    {
        "max_usd",
        "estimated_usd",
        "remaining_usd",
        "estimated_tokens",
        "estimated_latency_ms",
        "estimate_basis",
    }
)
_LATENCY_FIELDS = frozenset({"max_ms"})
_WORKFLOW_FIELDS = frozenset({"steps"})
_STEP_FIELDS = frozenset({"id", "action", "objective", "parallel", "required", "verification"})
T = TypeVar("T", bound="Contract")


class Contract:
    """Base for strict, JSON-compatible v1 contracts."""

    contract_version: ClassVar[str] = "1"

    @classmethod
    def from_dict(cls: type[T], payload: dict[str, Any]) -> T:
        if not isinstance(payload, dict):
            raise ContractValidationError("contract must be a JSON object")
        _reject_secrets(payload)
        declared_fields = fields(cast(Any, cls))
        allowed = {item.name for item in declared_fields}
        unknown = set(payload) - allowed
        if unknown:
            raise ContractValidationError(f"unknown field(s): {', '.join(sorted(unknown))}")
        _validate_version_field(payload, cls=cls, declared_fields=declared_fields)
        required = set(_REQUIRED_FIELDS.get(cls.__name__, frozenset()))
        required.update(
            item.name
            for item in declared_fields
            if item.default is MISSING and item.default_factory is MISSING
        )
        missing = [item for item in sorted(required) if item not in payload]
        if missing:
            raise ContractValidationError(f"missing required field(s): {', '.join(missing)}")
        type_hints = get_type_hints(cls)
        for item in declared_fields:
            if item.name in payload:
                _validate_field_value(
                    type_hints.get(item.name, item.type), payload[item.name], item.name
                )
        _validate_contract_semantics(cls, payload)
        coerced = {
            item.name: _coerce_field_value(type_hints.get(item.name, item.type), payload[item.name])
            for item in declared_fields
            if item.name in payload
        }
        return cls(**coerced)

    def to_dict(self) -> dict[str, Any]:
        return {
            item.name: _serialize_value(getattr(self, item.name))
            for item in fields(cast(Any, self))
        }


@dataclass(frozen=True)
class CapabilityRequirement(Contract):
    capability: str
    required: bool = True
    minimum_level: str | None = None
    reason: str | None = None
    schema_version: str = "1"

    @classmethod
    def from_legacy(cls, payload: dict[str, Any], /, **overrides: Any) -> CapabilityRequirement:
        return cls.from_dict({**payload, **overrides})


@dataclass(frozen=True)
class TaskSpec(Contract):
    objective: str
    task_type: str
    effort: str = "unknown"
    reasoning: str = "unknown"
    capabilities: list[str] = field(default_factory=list)
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

    @classmethod
    def from_legacy(cls, payload: dict[str, Any], /, **overrides: Any) -> TaskSpec:
        if not isinstance(payload, dict):
            raise ContractValidationError("legacy contract must be a JSON object")
        legacy = dict(payload)
        objective = legacy.pop("task", legacy.pop("objective", None))
        if objective is None:
            raise ContractValidationError("legacy task contract requires 'task' or 'objective'")
        metadata = legacy.pop("metadata", {})
        mapped = {
            "objective": objective,
            "task_type": legacy.pop("task_type", "unknown"),
            "criticality": legacy.pop("criticality", "unknown"),
            "context": legacy.pop("context", None),
            "metadata": metadata,
        }
        if legacy:
            mapped["metadata"] = {**dict(metadata), "legacy": legacy}
        return cls.from_dict({**mapped, **overrides})


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
    context_window: int | None = None
    max_output_tokens: int | None = None
    schema_version: str = "1"

    @classmethod
    def from_legacy(cls, payload: dict[str, Any], /, **overrides: Any) -> RuntimeCandidate:
        if not isinstance(payload, dict):
            raise ContractValidationError("legacy contract must be a JSON object")
        legacy = dict(payload)
        provider = legacy.pop("provider", None)
        model = legacy.pop("model", None)
        runtime_id = legacy.pop("runtime_id", None) or _runtime_id(provider, model)
        if runtime_id is None:
            raise ContractValidationError(
                "legacy runtime candidate requires runtime_id or provider/model"
            )
        signals = legacy.pop("signals", {})
        mapped = {
            "runtime_id": runtime_id,
            "catalog_present": bool(legacy.pop("catalog_present", model is not None)),
            "live_eligible": bool(legacy.pop("live_eligible", True)),
            "availability": legacy.pop("availability", "unknown"),
            "signals": signals,
            "capabilities": legacy.pop("capabilities", []),
            "provider": provider,
            "model": model,
            "model_version": legacy.pop("model_version", None),
            "context_window": legacy.pop("context_window", None),
            "max_output_tokens": legacy.pop("max_output_tokens", None),
        }
        if legacy:
            mapped["signals"] = {**dict(signals), "legacy": legacy}
        return cls.from_dict({**mapped, **overrides})


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

    @classmethod
    def from_legacy(cls, payload: dict[str, Any], /, **overrides: Any) -> AvailabilitySnapshot:
        if not isinstance(payload, dict):
            raise ContractValidationError("legacy contract must be a JSON object")
        legacy = dict(payload)
        signals = legacy.pop("signals", {})
        mapped = {
            "observed_at": legacy.pop("observed_at", "unknown"),
            "state": legacy.pop("state", "unknown"),
            "signals": signals,
            "candidates": [
                RuntimeCandidate.from_legacy(item) if isinstance(item, dict) else item
                for item in legacy.pop("candidates", [])
            ],
            "source": legacy.pop("source", "legacy"),
            "ttl_seconds": legacy.pop("ttl_seconds", 60),
            "expires_at": legacy.pop("expires_at", None),
        }
        if legacy:
            mapped["signals"] = {**dict(signals), "legacy": legacy}
        return cls.from_dict({**mapped, **overrides})


@dataclass(frozen=True)
class VerificationPlan(Contract):
    checks: list[Any] = field(default_factory=list)
    on_failure: str = "deny"
    schema_version: str = "1"

    @classmethod
    def from_legacy(cls, payload: dict[str, Any], /, **overrides: Any) -> VerificationPlan:
        return cls.from_dict({**payload, **overrides})


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

    @classmethod
    def from_legacy(cls, payload: dict[str, Any], /, **overrides: Any) -> WorkflowPlan:
        if not isinstance(payload, dict):
            raise ContractValidationError("legacy contract must be a JSON object")
        legacy = dict(payload)
        metadata = legacy.pop("metadata", {})
        steps = legacy.pop("steps", [])
        fallback_plan = legacy.pop("fallback_plan", [])
        steps = [
            {"action": "answer", **step}
            if isinstance(step, dict) and "action" not in step and "objective" in step
            else step
            for step in steps
        ]
        fallback_plan = [
            {"action": "answer", **step}
            if isinstance(step, dict) and "action" not in step and "objective" in step
            else step
            for step in fallback_plan
        ]
        mapped = {
            "steps": steps,
            "plan_id": legacy.pop("plan_id", None),
            "verification": legacy.pop("verification_plan", legacy.pop("verification", {})),
            "verification_plan_id": legacy.pop("verification_plan_id", None),
            "fallback_allowed": legacy.pop("fallback_allowed", False),
            "fallback_plan": fallback_plan,
            "policy_version": str(legacy.pop("policy_version", "1")),
            "metadata": metadata,
        }
        if legacy:
            mapped["metadata"] = {**dict(metadata), "legacy": legacy}
        return cls.from_dict({**mapped, **overrides})


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

    @classmethod
    def from_legacy(cls, payload: dict[str, Any], /, **overrides: Any) -> RoutingDecisionContract:
        if not isinstance(payload, dict):
            raise ContractValidationError("legacy contract must be a JSON object")
        legacy = dict(payload)
        provider = legacy.pop("provider", None)
        model = legacy.pop("model", None)
        runtime_id = legacy.pop("runtime_id", None) or _runtime_id(provider, model)
        alternatives = legacy.pop("alternatives", [])
        adaptive_influence = legacy.pop("adaptive_influence", {})
        mapped = {
            "selected_route": _compact_dict(
                {
                    "runtime_id": runtime_id,
                    "provider": provider,
                    "model": model,
                    "headroom_pct": legacy.pop("headroom_pct", None),
                    "latency_ms": legacy.pop("latency_ms", None),
                    "decision": legacy.pop("decision", None),
                    "escalated": legacy.pop("escalated", None),
                    "escalation_reason": legacy.pop("escalation_reason", None),
                    "logged": legacy.pop("logged", None),
                    "task_class": legacy.pop("task_class", None),
                }
            ),
            "task_spec": legacy.pop("task_spec", {}),
            "candidate_snapshot": legacy.pop("candidate_snapshot", None),
            "exclusions": [
                item
                if isinstance(item, dict)
                else {"model": str(item), "reason": "legacy alternative"}
                for item in alternatives
            ],
            "policy_floor": legacy.pop(
                "policy_floor", _policy_floor_from_tier(legacy.pop("tier", None))
            ),
            "planner_mode": legacy.pop("planner_mode", "legacy-routing-decision"),
            "explanation": legacy.pop("reason", ""),
            "adaptive_influence": adaptive_influence,
            "fallback_plan": legacy.pop("fallback_plan", []),
            "correlation_id": legacy.pop("correlation_id", None),
            "request_id": legacy.pop("request_id", None),
            "policy_version": str(legacy.pop("policy_version", "1")),
        }
        if legacy:
            mapped["adaptive_influence"] = {**dict(adaptive_influence), "legacy": legacy}
        return cls.from_dict({**mapped, **overrides})


RoutingDecision = RoutingDecisionContract


@dataclass(frozen=True)
class FallbackAttempt(Contract):
    runtime_id: str
    reason: str
    legal: bool = True
    schema_version: str = "1"

    @classmethod
    def from_legacy(cls, payload: dict[str, Any], /, **overrides: Any) -> FallbackAttempt:
        return cls.from_dict({**payload, **overrides})


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

    @classmethod
    def from_dict(cls: type[T], payload: dict[str, Any]) -> T:
        """Accept diagnostic details only after deterministic secret redaction."""
        if not isinstance(payload, dict):
            raise ContractValidationError("contract must be a JSON object")
        normalized = dict(payload)
        if "details" in normalized:
            normalized["details"] = redact_contract_secrets(normalized["details"])
        return super().from_dict(normalized)

    @classmethod
    def from_legacy(cls, payload: dict[str, Any], /, **overrides: Any) -> OutcomeEvent:
        if not isinstance(payload, dict):
            raise ContractValidationError("legacy contract must be a JSON object")
        legacy = dict(payload)
        details = legacy.pop("details", None)
        mapped = {
            "event_id": legacy.pop("event_id", None),
            "event_type": legacy.pop("event_type", "unknown"),
            "correlation_id": legacy.pop("correlation_id", None),
            "outcome": legacy.pop("outcome", "unknown"),
            "occurred_at": legacy.pop("occurred_at", "unknown"),
            "request_id": legacy.pop("request_id", None),
            "verification": legacy.pop("verification", {}),
            "quality": legacy.pop("quality", {}),
            "latency_ms": legacy.pop("latency_ms", None),
            "cost": legacy.pop("cost", {}),
            "retries": legacy.pop("retries", 0),
            "fallbacks": legacy.pop("fallbacks", []),
            "provider_version": legacy.pop("provider_version", None),
            "model_version": legacy.pop("model_version", None),
            "details": details,
        }
        if legacy:
            mapped["details"] = {**dict(details or {}), "legacy": legacy}
        return cls.from_dict({**mapped, **overrides})


@dataclass(frozen=True)
class TaskEpisode(Contract):
    task_fingerprint: str
    objective_preview: str
    task_type: str = "unknown"
    privacy: str = "unknown"
    risk: str = "unknown"
    required_capabilities: list[str] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)
    approvals_count: int = 0
    context_keys: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    schema_version: str = "1"

    @classmethod
    def from_task_spec(cls, task_spec: TaskSpec | dict[str, Any]) -> TaskEpisode:
        task = task_spec if isinstance(task_spec, TaskSpec) else TaskSpec.from_dict(task_spec)
        fingerprint_payload = {
            "objective": task.objective,
            "task_type": task.task_type,
            "required_capabilities": task.required_capabilities,
            "tools": task.tools,
            "privacy": task.privacy,
            "risk": task.risk,
            "approvals": task.approvals,
            "parallelism": task.parallelism,
            "workflow": task.workflow,
            "verification": task.verification,
        }
        return cls(
            task_fingerprint=_fingerprint_payload(fingerprint_payload),
            objective_preview=_privacy_safe_preview(task.objective),
            task_type=task.task_type,
            privacy=task.privacy,
            risk=task.risk,
            required_capabilities=list(task.required_capabilities),
            tools=list(task.tools),
            approvals_count=len(task.approvals),
            context_keys=sorted((task.context or {}).keys()),
            metadata=cast(dict[str, Any], redact_contract_secrets(task.metadata)),
        )


@dataclass(frozen=True)
class WorkflowEpisode(Contract):
    workflow_fingerprint: str
    plan_id: str | None = None
    step_count: int = 0
    fallback_allowed: bool = False
    fallback_step_count: int = 0
    verification_checks: list[str] = field(default_factory=list)
    verification_plan_id: str | None = None
    policy_version: str = "1"
    metadata: dict[str, Any] = field(default_factory=dict)
    schema_version: str = "1"

    @classmethod
    def from_workflow_plan(cls, workflow_plan: WorkflowPlan | dict[str, Any]) -> WorkflowEpisode:
        workflow = (
            workflow_plan
            if isinstance(workflow_plan, WorkflowPlan)
            else WorkflowPlan.from_dict(workflow_plan)
        )
        verification = workflow.verification
        verification_checks = []
        if isinstance(verification, VerificationPlan):
            verification_checks = [str(check) for check in verification.checks]
        elif isinstance(verification, dict):
            verification_checks = [str(check) for check in verification.get("checks", [])]
        fingerprint_payload = {
            "steps": workflow.steps,
            "verification": _serialize_value(verification),
            "fallback_allowed": workflow.fallback_allowed,
            "fallback_plan": workflow.fallback_plan,
            "policy_version": workflow.policy_version,
        }
        return cls(
            workflow_fingerprint=_fingerprint_payload(fingerprint_payload),
            plan_id=workflow.plan_id,
            step_count=len(workflow.steps),
            fallback_allowed=workflow.fallback_allowed,
            fallback_step_count=len(workflow.fallback_plan),
            verification_checks=verification_checks,
            verification_plan_id=workflow.verification_plan_id,
            policy_version=workflow.policy_version,
            metadata=cast(dict[str, Any], redact_contract_secrets(workflow.metadata)),
        )


@dataclass(frozen=True)
class OutcomeEpisode(Contract):
    outcome_fingerprint: str
    event_type: str | None = None
    outcome: str | None = None
    occurred_at: str | None = None
    request_id: str | None = None
    correlation_id: str | None = None
    decision: str | None = None
    policy_floor: str | None = None
    selected_route: dict[str, Any] = field(default_factory=dict)
    verification: dict[str, Any] = field(default_factory=dict)
    quality: dict[str, Any] = field(default_factory=dict)
    latency_ms: float | None = None
    cost: dict[str, Any] = field(default_factory=dict)
    retries: int = 0
    fallback_count: int = 0
    provider_version: str | None = None
    model_version: str | None = None
    details: dict[str, Any] | None = None
    schema_version: str = "1"

    @classmethod
    def from_outcome_event(
        cls,
        outcome_event: OutcomeEvent | dict[str, Any],
        *,
        routing_decision: RoutingDecisionContract | dict[str, Any] | None = None,
    ) -> OutcomeEpisode:
        outcome = (
            outcome_event
            if isinstance(outcome_event, OutcomeEvent)
            else OutcomeEvent.from_dict(outcome_event)
        )
        decision = None
        policy_floor = None
        selected_route: dict[str, Any] = {}
        if routing_decision is not None:
            route = (
                routing_decision
                if isinstance(routing_decision, RoutingDecisionContract)
                else RoutingDecisionContract.from_dict(routing_decision)
            )
            decision = route.selected_route.get("decision") or route.planner_mode
            policy_floor = route.policy_floor
            selected_route = cast(dict[str, Any], redact_contract_secrets(route.selected_route))
        verification = cast(dict[str, Any], redact_contract_secrets(outcome.verification))
        quality = cast(dict[str, Any], redact_contract_secrets(outcome.quality))
        cost = cast(dict[str, Any], redact_contract_secrets(outcome.cost))
        details = cast(dict[str, Any] | None, redact_contract_secrets(outcome.details))
        fingerprint_payload = {
            "event_type": outcome.event_type,
            "outcome": outcome.outcome,
            "verification": verification,
            "quality": quality,
            "latency_ms": outcome.latency_ms,
            "cost": cost,
            "retries": outcome.retries,
            "fallbacks": outcome.fallbacks,
            "selected_route": selected_route,
            "details": details,
        }
        return cls(
            outcome_fingerprint=_fingerprint_payload(fingerprint_payload),
            event_type=outcome.event_type,
            outcome=outcome.outcome,
            occurred_at=outcome.occurred_at,
            request_id=outcome.request_id,
            correlation_id=outcome.correlation_id,
            decision=decision,
            policy_floor=policy_floor,
            selected_route=selected_route,
            verification=verification,
            quality=quality,
            latency_ms=outcome.latency_ms,
            cost=cost,
            retries=outcome.retries,
            fallback_count=len(outcome.fallbacks),
            provider_version=outcome.provider_version,
            model_version=outcome.model_version,
            details=details,
        )


@dataclass(frozen=True)
class TaskWorkflowOutcomeEpisode(Contract):
    episode_id: str | None = None
    request_id: str | None = None
    correlation_id: str | None = None
    policy_version: str = "1"
    task: TaskEpisode | dict[str, Any] = field(default_factory=dict)
    workflow: WorkflowEpisode | dict[str, Any] = field(default_factory=dict)
    outcome: OutcomeEpisode | dict[str, Any] = field(default_factory=dict)
    schema_version: str = "1"

    @classmethod
    def from_contracts(
        cls,
        *,
        task_spec: TaskSpec | dict[str, Any],
        workflow_plan: WorkflowPlan | dict[str, Any],
        outcome_event: OutcomeEvent | dict[str, Any],
        routing_decision: RoutingDecisionContract | dict[str, Any] | None = None,
        episode_id: str | None = None,
    ) -> TaskWorkflowOutcomeEpisode:
        task_episode = TaskEpisode.from_task_spec(task_spec)
        workflow_episode = WorkflowEpisode.from_workflow_plan(workflow_plan)
        outcome_episode = OutcomeEpisode.from_outcome_event(
            outcome_event,
            routing_decision=routing_decision,
        )
        route = (
            routing_decision
            if isinstance(routing_decision, RoutingDecisionContract) or routing_decision is None
            else RoutingDecisionContract.from_dict(routing_decision)
        )
        return cls(
            episode_id=episode_id,
            request_id=outcome_episode.request_id
            or (route.request_id if route is not None else None),
            correlation_id=outcome_episode.correlation_id
            or (route.correlation_id if route is not None else None),
            policy_version=(
                route.policy_version if route is not None else workflow_episode.policy_version
            ),
            task=task_episode,
            workflow=workflow_episode,
            outcome=outcome_episode,
        )


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

    @classmethod
    def from_legacy(cls, payload: dict[str, Any], /, **overrides: Any) -> LearningEvent:
        return cls.from_dict({**payload, **overrides})


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
        "task_episode": TaskEpisode,
        "TaskEpisode": TaskEpisode,
        "workflow_episode": WorkflowEpisode,
        "WorkflowEpisode": WorkflowEpisode,
        "outcome_episode": OutcomeEpisode,
        "OutcomeEpisode": OutcomeEpisode,
        "task_workflow_outcome_episode": TaskWorkflowOutcomeEpisode,
        "TaskWorkflowOutcomeEpisode": TaskWorkflowOutcomeEpisode,
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


def contract_from_legacy_dict(name: str, payload: dict[str, Any]) -> Contract:
    try:
        contract_cls = cast(Any, _CONTRACTS[name])
    except KeyError as exc:
        raise ContractValidationError(f"unknown contract: {name}") from exc
    return cast(Contract, contract_cls.from_legacy(payload))


def redact_contract_secrets(value: Any) -> Any:
    if isinstance(value, Contract):
        return redact_contract_secrets(value.to_dict())
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, child in value.items():
            normalized = key.lower().replace("-", "_")
            if (
                normalized in _SECRET_NAMES
                or normalized.endswith("_token")
                or normalized.endswith("_secret")
                or normalized.endswith("_password")
                or normalized.endswith("_api_key")
            ):
                redacted[key] = "[redacted]"
            else:
                redacted[key] = redact_contract_secrets(child)
        return redacted
    if isinstance(value, list):
        return [redact_contract_secrets(child) for child in value]
    if isinstance(value, tuple):
        return [redact_contract_secrets(child) for child in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


def _privacy_safe_preview(value: object) -> str:
    text = str(value)
    return f"[redacted:{fingerprint_text(text, length=12)} len={len(text)}]"


def _fingerprint_payload(value: Any) -> str:
    canonical = json.dumps(_serialize_value(value), sort_keys=True, separators=(",", ":"))
    return fingerprint_text(canonical)


def _version_field(
    cls: type[Contract], declared_fields: tuple[Field[Any], ...]
) -> Field[Any] | None:
    for item in declared_fields:
        if item.name == "schema_version":
            return item
    return None


def _validate_version_field(
    payload: dict[str, Any], *, cls: type[Contract], declared_fields: tuple[Field[Any], ...]
) -> None:
    version_field = _version_field(cls, declared_fields)
    if version_field is None:
        return
    expected = version_field.default
    actual = payload.get(version_field.name, expected)
    if actual != expected:
        raise ContractValidationError(
            f"{cls.__name__}.{version_field.name} must be {expected!r}, got {actual!r}"
        )


def _reject_secrets(value: Any) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = key.lower().replace("-", "_")
            if (
                normalized in _SECRET_NAMES
                or normalized.endswith("_token")
                or normalized.endswith("_secret")
                or normalized.endswith("_password")
                or normalized.endswith("_api_key")
            ) and child != "[redacted]":
                raise ContractValidationError(f"secret-bearing field rejected: {key}")
            _reject_secrets(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            _reject_secrets(child)


def _serialize_value(value: Any) -> Any:
    if isinstance(value, Contract):
        return value.to_dict()
    if isinstance(value, list):
        return [_serialize_value(item) for item in value]
    if isinstance(value, tuple):
        return [_serialize_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _serialize_value(child) for key, child in value.items()}
    return value


def _coerce_field_value(annotation: Any, value: Any) -> Any:
    if value is None:
        return None
    if _is_contract_type(annotation) and isinstance(value, dict):
        return annotation.from_dict(value)
    origin = get_origin(annotation)
    if origin in (list, tuple):
        args = get_args(annotation)
        item_type = args[0] if args else Any
        return [_coerce_field_value(item_type, item) for item in value]
    if origin is dict:
        value_type = get_args(annotation)[1] if len(get_args(annotation)) > 1 else Any
        return {key: _coerce_field_value(value_type, child) for key, child in value.items()}
    if origin in (_union_origin(), UnionType):
        for option in get_args(annotation):
            if option is type(None):
                continue
            if _is_contract_type(option) and isinstance(value, dict):
                return option.from_dict(value)
        return value
    return value


def _validate_field_value(annotation: Any, value: Any, field_name: str) -> None:
    """Reject values that dataclass construction would otherwise accept silently."""
    if value is None:
        if _allows_none(annotation):
            return
        raise ContractValidationError(f"{field_name} must not be null")
    origin = get_origin(annotation)
    if origin in (_union_origin(), UnionType):
        if any(_value_matches(option, value) for option in get_args(annotation)):
            return
        raise ContractValidationError(f"{field_name} has invalid type")
    if origin in (list, tuple):
        if not isinstance(value, list):
            raise ContractValidationError(f"{field_name} must be an array")
        item_type = get_args(annotation)[0] if get_args(annotation) else Any
        for child in value:
            _validate_field_value(item_type, child, field_name)
        return
    if origin is dict:
        if not isinstance(value, dict):
            raise ContractValidationError(f"{field_name} must be an object")
        args = get_args(annotation)
        value_type = args[1] if len(args) > 1 else Any
        for child in value.values():
            _validate_field_value(value_type, child, field_name)
        return
    if annotation is Any or annotation is object:
        return
    if isinstance(annotation, type) and not isinstance(value, annotation):
        raise ContractValidationError(f"{field_name} has invalid type")


def _value_matches(annotation: Any, value: Any) -> bool:
    if annotation is type(None):
        return value is None
    if annotation is Any or annotation is object:
        return True
    if annotation is float:
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    origin = get_origin(annotation)
    if origin in (list, tuple):
        return isinstance(value, list)
    if origin is dict:
        return isinstance(value, dict)
    return isinstance(annotation, type) and isinstance(value, annotation)


def _allows_none(annotation: Any) -> bool:
    return type(None) in get_args(annotation)


def _validate_enum(value: Any, allowed: frozenset[str], field_name: str) -> None:
    if not isinstance(value, str) or value not in allowed:
        choices = ", ".join(sorted(allowed))
        raise ContractValidationError(f"{field_name} must be one of: {choices}")


def _validate_non_negative(value: Any, field_name: str, *, integer: bool = False) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ContractValidationError(f"{field_name} must be a non-negative number")
    if integer and type(value) is not int:
        raise ContractValidationError(f"{field_name} must be a non-negative integer")
    try:
        if not math.isfinite(float(value)):
            raise ContractValidationError(f"{field_name} must be finite")
    except (OverflowError, ValueError) as exc:
        raise ContractValidationError(f"{field_name} must be finite") from exc
    if value < 0:
        raise ContractValidationError(f"{field_name} must be a non-negative number")


def _validate_contract_semantics(cls: type[Contract], payload: dict[str, Any]) -> None:
    """Apply v1 safety rules shared by Python producers and JSON Schema consumers."""
    if cls is TaskSpec:
        if not payload["objective"].strip():
            raise ContractValidationError("objective must not be empty")
        if not payload["task_type"].strip():
            raise ContractValidationError("task_type must not be empty")
        _validate_enum(payload.get("effort", "unknown"), _TASK_EFFORTS, "effort")
        _validate_enum(payload.get("reasoning", "unknown"), _TASK_REASONING_LEVELS, "reasoning")
        for name, allowed in (
            ("privacy", _TASK_PRIVACY_LEVELS),
            ("risk", _TASK_RISK_LEVELS),
            ("parallelism", _TASK_PARALLELISM),
            ("degraded_mode_policy", _DEGRADED_MODE_POLICIES),
        ):
            defaults = {
                "privacy": "unknown",
                "risk": "unknown",
                "parallelism": "serial",
                "degraded_mode_policy": "deny",
            }
            _validate_enum(payload.get(name, defaults.get(name)), allowed, name)
        for name in ("capabilities", "required_capabilities", "tools", "approvals"):
            if any(not isinstance(item, str) or not item.strip() for item in payload.get(name, [])):
                raise ContractValidationError(f"{name} must contain non-empty strings")
        budget = payload.get("budget", {})
        unknown_budget = set(budget) - _BUDGET_FIELDS
        if unknown_budget:
            raise ContractValidationError(
                f"unknown budget field(s): {', '.join(sorted(unknown_budget))}"
            )
        for key in ("max_usd", "estimated_usd", "remaining_usd"):
            if key in budget:
                _validate_non_negative(budget[key], f"budget.{key}")
        for key in ("estimated_tokens", "estimated_latency_ms"):
            if key in budget:
                _validate_non_negative(budget[key], f"budget.{key}", integer=True)
        if payload.get("latency_limit_ms") is not None:
            _validate_non_negative(payload["latency_limit_ms"], "latency_limit_ms", integer=True)
        latency = payload.get("latency")
        if latency is not None:
            unknown_latency = set(latency) - _LATENCY_FIELDS
            if unknown_latency:
                raise ContractValidationError(
                    f"unknown latency field(s): {', '.join(sorted(unknown_latency))}"
                )
            if "max_ms" in latency:
                _validate_non_negative(latency["max_ms"], "latency.max_ms", integer=True)
        workflow = payload.get("workflow")
        if workflow is not None:
            if not isinstance(workflow, dict) or set(workflow) - _WORKFLOW_FIELDS:
                unknown = (
                    set(workflow) - _WORKFLOW_FIELDS
                    if isinstance(workflow, dict)
                    else {"<non-object>"}
                )
                raise ContractValidationError(
                    f"workflow has unknown or invalid field(s): {', '.join(sorted(unknown))}"
                )
            if "steps" not in workflow:
                raise ContractValidationError("workflow requires 'steps'")
            _validate_workflow_steps(workflow.get("steps", []), "workflow.steps")
    elif cls is WorkflowPlan:
        _validate_workflow_steps(payload["steps"], "steps", allow_legacy_shape=True)
        if payload.get("fallback_plan"):
            _validate_workflow_steps(
                payload["fallback_plan"], "fallback_plan", allow_legacy_shape=True
            )
        verification = payload.get("verification")
        if verification is not None and isinstance(verification, dict):
            _validate_enum(
                verification.get("on_failure", "deny"),
                frozenset({"deny", "replan_or_deny"}),
                "verification.on_failure",
            )
    elif cls is RuntimeCandidate:
        _validate_enum(payload["availability"], _AVAILABILITY_STATES, "availability")
    elif cls is AvailabilitySnapshot:
        _validate_enum(payload.get("state", "unknown"), _AVAILABILITY_STATES, "state")
        _validate_non_negative(payload.get("ttl_seconds", 60), "ttl_seconds", integer=True)
    elif cls is RoutingDecisionContract:
        _validate_enum(payload.get("policy_floor", "none"), _POLICY_FLOORS, "policy_floor")
        if payload.get("fallback_plan"):
            _validate_workflow_steps(payload["fallback_plan"], "fallback_plan")
    elif cls is OutcomeEvent:
        for name in ("event_type", "occurred_at"):
            value = payload[name]
            if not isinstance(value, str) or not value.strip():
                raise ContractValidationError(f"{name} must be a non-empty string")
        _validate_enum(payload["outcome"], _OUTCOME_VALUES, "outcome")
        _validate_non_negative(payload.get("latency_ms"), "latency_ms") if payload.get(
            "latency_ms"
        ) is not None else None
        _validate_non_negative(payload.get("retries", 0), "retries", integer=True)


def _validate_workflow_steps(
    value: Any, field_name: str, *, allow_legacy_shape: bool = False
) -> None:
    if not isinstance(value, list) or not value:
        raise ContractValidationError(f"{field_name} must contain at least one step")
    for index, step in enumerate(value):
        if not isinstance(step, dict):
            raise ContractValidationError(f"{field_name}[{index}] must be an object")
        unknown = set(step) - _STEP_FIELDS
        if unknown:
            raise ContractValidationError(
                f"{field_name}[{index}] has unknown field(s): {', '.join(sorted(unknown))}"
            )
        action = step.get("action")
        if action is None and allow_legacy_shape and isinstance(step.get("objective"), str):
            action = "answer"
        if not isinstance(action, str) or action not in _WORKFLOW_ACTIONS:
            raise ContractValidationError(f"{field_name}[{index}].action is unsafe or unknown")
        if "parallel" in step and type(step["parallel"]) is not bool:
            raise ContractValidationError(f"{field_name}[{index}].parallel must be boolean")
        if "required" in step and type(step["required"]) is not bool:
            raise ContractValidationError(f"{field_name}[{index}].required must be boolean")
        for name in ("id", "objective", "verification"):
            if name in step and (not isinstance(step[name], str) or not step[name].strip()):
                raise ContractValidationError(
                    f"{field_name}[{index}].{name} must be a non-empty string"
                )


def _is_contract_type(annotation: Any) -> bool:
    return isinstance(annotation, type) and issubclass(annotation, Contract)


def _union_origin() -> Any:
    try:
        from typing import Union

        return Union
    except ImportError:  # pragma: no cover
        return None


def _policy_floor_from_tier(tier: Any) -> str:
    mapping = {0: "isolated", 1: "protected", 2: "standard", 3: "best_effort"}
    return mapping.get(tier, "none")


def _runtime_id(provider: Any, model: Any) -> str | None:
    if provider and model:
        return f"{provider}/{model}"
    return None


def _compact_dict(value: dict[str, Any]) -> dict[str, Any]:
    return {key: child for key, child in value.items() if child is not None}
