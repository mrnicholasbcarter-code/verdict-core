"""Strict version-1 shared JSON contracts for planning and routing."""

from __future__ import annotations

import json
from dataclasses import MISSING, Field, dataclass, field, fields
from types import UnionType
from typing import Any, ClassVar, TypeVar, cast, get_args, get_origin, get_type_hints

from llm_gate.security import fingerprint_text, redact_text


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
        declared_fields = fields(cast(Any, cls))
        allowed = {item.name for item in declared_fields}
        unknown = set(payload) - allowed
        if unknown:
            raise ContractValidationError(f"unknown field(s): {', '.join(sorted(unknown))}")
        _validate_version_field(payload, cls=cls, declared_fields=declared_fields)
        missing = [
            item.name
            for item in declared_fields
            if item.default is MISSING
            and item.default_factory is MISSING
            and item.name not in payload
        ]
        if missing:
            raise ContractValidationError(f"missing required field(s): {', '.join(missing)}")
        type_hints = get_type_hints(cls)
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
            raise ContractValidationError("legacy runtime candidate requires runtime_id or provider/model")
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
                RuntimeCandidate.from_legacy(item)
                if isinstance(item, dict)
                else item
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
        mapped = {
            "steps": legacy.pop("steps", []),
            "plan_id": legacy.pop("plan_id", None),
            "verification": legacy.pop("verification_plan", legacy.pop("verification", {})),
            "verification_plan_id": legacy.pop("verification_plan_id", None),
            "fallback_allowed": legacy.pop("fallback_allowed", False),
            "fallback_plan": legacy.pop("fallback_plan", []),
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
                item if isinstance(item, dict) else {"model": str(item), "reason": "legacy alternative"}
                for item in alternatives
            ],
            "policy_floor": legacy.pop("policy_floor", _policy_floor_from_tier(legacy.pop("tier", None))),
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
        return cast(T, super().from_dict(normalized))

    @classmethod
    def from_legacy(cls, payload: dict[str, Any], /, **overrides: Any) -> OutcomeEvent:
        if not isinstance(payload, dict):
            raise ContractValidationError("legacy contract must be a JSON object")
        legacy = dict(payload)
        details = legacy.pop("details", None)
        mapped = {
            "event_id": legacy.pop("event_id", None),
            "event_type": legacy.pop("event_type", None),
            "correlation_id": legacy.pop("correlation_id", None),
            "outcome": legacy.pop("outcome", None),
            "occurred_at": legacy.pop("occurred_at", None),
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
        workflow = workflow_plan if isinstance(workflow_plan, WorkflowPlan) else WorkflowPlan.from_dict(workflow_plan)
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
        outcome = outcome_event if isinstance(outcome_event, OutcomeEvent) else OutcomeEvent.from_dict(outcome_event)
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
            request_id=outcome_episode.request_id or (route.request_id if route is not None else None),
            correlation_id=outcome_episode.correlation_id or (route.correlation_id if route is not None else None),
            policy_version=(route.policy_version if route is not None else workflow_episode.policy_version),
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


def _version_field(cls: type[Contract], declared_fields: tuple[Field[Any], ...]) -> Field[Any] | None:
    for item in declared_fields:
        if item.name == "schema_version":
            return cast(Field[Any], item)
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
            ):
                # A previously redacted diagnostic payload is safe to deserialize.
                if child != "[redacted]":
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
