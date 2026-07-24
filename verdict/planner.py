"""Structured task intake and bounded workflow planning."""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from enum import Enum
from typing import Any

from verdict.availability import CandidateRequirements, canonical_capability
from verdict.contracts import ContractValidationError, TaskSpec, VerificationPlan, WorkflowPlan


class PlanningUnavailableError(RuntimeError):
    """Raised when the optional planning provider cannot be reached."""


class PlanRejectedError(ValueError):
    """Raised when a plan violates deterministic planner policy."""


class WorkflowKind(str, Enum):
    SINGLE = "single_model"
    RESEARCH_IMPLEMENT = "research_then_implementation"
    IMPLEMENT_REVIEW = "implementation_then_review"
    IMPLEMENT_TEST = "implementation_then_test"
    PARALLEL = "parallel_specialists"
    SEQUENTIAL = "sequential_specialist_pipeline"
    APPROVAL = "human_approval_workflow"
    HUMAN_APPROVAL = "human_approval_workflow"


class FailureClass(str, Enum):
    PROVIDER_FAILURE = "provider_failure"
    QUOTA_EXHAUSTION = "quota_exhaustion"
    TOOL_FAILURE = "tool_failure"
    PERMISSION_DENIAL = "permission_denial"
    TEST_FAILURE = "test_failure"
    PLAN_FAILURE = "plan_failure"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class PlannerPolicy:
    max_cost_usd: float | None = 20.0
    max_replans: int = 1
    protected_keywords: tuple[str, ...] = (
        "production",
        "deploy",
        "delete",
        "credential",
        "secret",
        "security",
    )


@dataclass(frozen=True)
class PlanResult:
    task_spec: TaskSpec
    workflow_plan: WorkflowPlan
    metadata: dict[str, Any]


PlannerCallable = Callable[[dict[str, Any]], dict[str, Any]]
_EFFORT_TOKEN_RESERVATIONS = {"low": 1_024, "medium": 8_192, "high": 32_768}
_EFFORT_COST_RESERVATIONS = {"low": 0.05, "medium": 1.0, "high": 5.0}
_EFFORT_LATENCY_RESERVATIONS = {"low": 2_000, "medium": 15_000, "high": 60_000}
_ESTIMATE_BASIS = "deterministic_effort_reservation_v1"

# Compatibility names used by the public planner API and fixtures.
PlanningUnavailable = PlanningUnavailableError
PlanRejected = PlanRejectedError
PlanningResult = PlanResult


class StructuredPlanner:
    """Produce validated task/workflow contracts without selecting a model."""

    def __init__(
        self, planner: PlannerCallable | None = None, *, policy: PlannerPolicy | None = None
    ) -> None:
        self.planner = planner
        self.policy = policy or PlannerPolicy()

    def plan(
        self,
        objective: str,
        *,
        criticality: str = "unknown",
        budget: dict[str, Any] | None = None,
        context: dict[str, Any] | None = None,
    ) -> PlanResult:
        if not objective.strip():
            raise PlanRejected("objective is required")
        request: dict[str, Any] = {
            "objective": objective,
            "criticality": criticality,
            "budget": budget or {},
            "context": context or {},
        }
        try:
            proposed = self.planner(request) if self.planner else None
            if proposed is None:
                result = self._deterministic(request)
                mode = "deterministic"
            else:
                result = self._sanitize_proposal(request, proposed)
                mode = "structured"
        except PlanningUnavailable:
            result = self._deterministic(request)
            mode = "deterministic-fallback"
        except ContractValidationError as exc:
            raise PlanRejected(str(exc)) from exc
        self._enforce_budget(result.task_spec, budget)
        return replace(result, metadata={**result.metadata, "planner_mode": mode})

    def intake(
        self,
        objective: str,
        *,
        criticality: str = "unknown",
        budget: dict[str, Any] | None = None,
        context: dict[str, Any] | None = None,
    ) -> PlanResult:
        """Alias for the structured intake boundary."""
        return self.plan(objective, criticality=criticality, budget=budget, context=context)

    def select_workflow(self, task_spec: TaskSpec) -> WorkflowPlan:
        """Return a policy-safe workflow for an already validated task."""
        return self._deterministic(
            {
                "objective": task_spec.objective,
                "criticality": task_spec.criticality,
                "budget": task_spec.budget,
                "context": task_spec.context or {},
            }
        ).workflow_plan

    def estimate(self, objective: str, **kwargs: Any) -> dict[str, Any]:
        """Expose deterministic effort/capability estimates without model routing."""
        result = self.plan(objective, **kwargs)
        return {
            "effort": result.task_spec.effort,
            "reasoning": result.task_spec.reasoning,
            "workflow": result.workflow_plan.metadata.get("workflow"),
            "verification": result.task_spec.verification,
        }

    def validate(self, result: PlanResult) -> PlanResult:
        """Re-validate a plan and ensure it still cannot weaken policy floors."""
        return self._sanitize_proposal(
            {
                "objective": result.task_spec.objective,
                "criticality": result.task_spec.criticality,
                "budget": result.task_spec.budget,
                "context": result.task_spec.context or {},
            },
            {
                "task_spec": result.task_spec.to_dict(),
                "workflow_plan": result.workflow_plan.to_dict(),
            },
        )

    @staticmethod
    def capability_requirements(task_spec: TaskSpec) -> dict[str, Any]:
        """Describe required capabilities for a dispatcher, not a model choice."""
        budget = task_spec.budget
        return {
            "reasoning": task_spec.reasoning,
            "tools": sorted(task_spec.tools),
            "required_capabilities": sorted(task_spec.required_capabilities),
            "effort": task_spec.effort,
            "verification": task_spec.verification,
            "production_impact": task_spec.production_impact,
            "estimated_tokens": budget.get("estimated_tokens"),
            "estimated_cost": budget.get("estimated_usd"),
            "budget_remaining": budget.get("remaining_usd", budget.get("max_usd")),
        }

    def estimate_requirements(self, task_spec: TaskSpec) -> dict[str, Any]:
        """Compatibility alias for capability and effort estimation."""
        return self.capability_requirements(task_spec)

    @staticmethod
    def availability_requirements(task_spec: TaskSpec) -> CandidateRequirements:
        """Compile a planned task into hard pre-ranking availability gates."""
        budget = task_spec.budget
        protected = (
            task_spec.production_impact
            or task_spec.destructive_operation
            or task_spec.degraded_mode_policy == "deny"
        )
        return CandidateRequirements(
            required=frozenset(
                canonical_capability(value) for value in task_spec.required_capabilities
            ),
            protected=protected,
            budget_remaining=budget.get("remaining_usd", budget.get("max_usd")),
            estimated_tokens=budget.get("estimated_tokens"),
            estimated_cost=budget.get("estimated_usd"),
            allow_degraded=(
                task_spec.degraded_mode_policy == "allow_with_penalty" and not protected
            ),
        )

    def route(self, objective: str, **kwargs: Any) -> PlanResult:
        """Compatibility alias; routing remains separate from model selection."""
        return self.plan(objective, **kwargs)

    def replan(self, result: PlanResult, failure: str, *, attempt: int) -> PlanResult:
        if attempt < 1 or attempt > self.policy.max_replans:
            raise PlanRejected("replan limit exceeded")
        failure_class = self.classify_failure(failure)
        workflow = result.workflow_plan
        metadata = {
            **workflow.metadata,
            "replan_reason": failure_class.value,
            "replan_attempt": attempt,
            "alternate_route_required": failure_class
            in {FailureClass.PROVIDER_FAILURE, FailureClass.QUOTA_EXHAUSTION},
            "human_escalation": failure_class is FailureClass.PERMISSION_DENIAL,
            "cancellation_allowed": failure_class
            in {FailureClass.PERMISSION_DENIAL, FailureClass.PLAN_FAILURE},
            "rollback_required": result.task_spec.destructive_operation,
        }
        return replace(
            result,
            workflow_plan=replace(workflow, metadata=metadata),
            metadata={
                **result.metadata,
                "replan_reason": failure_class.value,
                "replan_attempt": attempt,
            },
        )

    def fallback_for(self, result: PlanResult, failure: str, *, attempt: int = 1) -> PlanResult:
        """Return a bounded, policy-preserving fallback after a classified failure."""
        return self.replan(result, failure, attempt=attempt)

    @staticmethod
    def classify_failure_class(message: str) -> FailureClass:
        """Compatibility alias for failure classification."""
        return StructuredPlanner.classify_failure(message)

    def can_replan(self, attempt: int) -> bool:
        return 0 <= attempt <= self.policy.max_replans

    @staticmethod
    def classify_failure(message: str) -> FailureClass:
        value = message.lower()
        if any(token in value for token in ("429", "quota", "rate limit", "headroom")):
            return FailureClass.QUOTA_EXHAUSTION
        if any(token in value for token in ("permission", "forbidden", "unauthorized")):
            return FailureClass.PERMISSION_DENIAL
        if any(token in value for token in ("pytest", "test failed", "verification failed")):
            return FailureClass.TEST_FAILURE
        if any(token in value for token in ("tool", "command failed")):
            return FailureClass.TOOL_FAILURE
        if any(token in value for token in ("timeout", "provider", "upstream", "connection")):
            return FailureClass.PROVIDER_FAILURE
        if any(token in value for token in ("plan", "assumption")):
            return FailureClass.PLAN_FAILURE
        return FailureClass.UNKNOWN

    def _deterministic(self, request: dict[str, object]) -> PlanResult:
        objective = str(request["objective"])
        lower = objective.lower()
        criticality = str(request["criticality"])
        context = request.get("context")
        context_text = " ".join(
            str(value) for value in (context.values() if isinstance(context, dict) else ())
        ).lower()
        combined = f"{lower} {context_text}"
        protected = any(keyword in combined for keyword in self.policy.protected_keywords)
        steps: list[dict[str, Any]]
        if any(word in lower for word in ("parallel", "independent specialists")):
            kind = WorkflowKind.PARALLEL
            steps = [{"action": "specialist", "parallel": True}, {"action": "synthesis"}]
            effort = "high"
        elif any(word in lower for word in ("pipeline", "sequential", "decompose")):
            kind = WorkflowKind.SEQUENTIAL
            steps = [{"action": "specialist"}, {"action": "synthesis"}]
            effort = "high"
        elif any(word in combined for word in ("research", "investigate")) and any(
            word in combined for word in ("implement", "build", "write", "code")
        ):
            kind = WorkflowKind.RESEARCH_IMPLEMENT
            steps = [
                {"action": "research", "verification": "sources"},
                {"action": "implement", "verification": "tests"},
            ]
            effort = "high"
        elif any(word in combined for word in ("review", "audit")):
            kind = WorkflowKind.IMPLEMENT_REVIEW
            steps = [{"action": "review"}, {"action": "verify"}]
            effort = "medium"
        elif any(word in combined for word in ("test", "fix", "implement", "build")):
            kind = WorkflowKind.IMPLEMENT_TEST
            steps = [{"action": "implement"}, {"action": "verify"}]
            effort = "medium"
        else:
            kind = WorkflowKind.SINGLE
            steps = [{"action": "answer"}]
            effort = "low"
        if protected:
            steps.insert(0, {"action": "human_approval", "required": True})
        verification = VerificationPlan(
            checks=["tests" if kind != WorkflowKind.SINGLE else "response_schema"],
            on_failure="replan_or_deny",
        )
        raw_budget = request.get("budget")
        budget = dict(raw_budget) if isinstance(raw_budget, Mapping) else {}
        raw_context = request.get("context")
        context_data = dict(raw_context) if isinstance(raw_context, Mapping) else None
        task = TaskSpec(
            objective=objective,
            task_type=kind.value,
            effort=effort,
            reasoning="high" if protected or effort == "high" else "medium",
            criticality=criticality,
            required_capabilities=(
                ["tool-calling"]
                if any(word in lower for word in ("implement", "build", "code", "api", "test"))
                else []
            ),
            tools=(
                ["tool-calling"]
                if any(word in lower for word in ("implement", "build", "code", "api", "test"))
                else []
            ),
            budget={
                **budget,
                "estimated_tokens": _EFFORT_TOKEN_RESERVATIONS[effort],
                "estimated_usd": _EFFORT_COST_RESERVATIONS[effort],
                "estimated_latency_ms": _EFFORT_LATENCY_RESERVATIONS[effort],
                "estimate_basis": _ESTIMATE_BASIS,
            },
            context=context_data,
            production_impact=any(word in lower for word in ("production", "deploy")),
            destructive_operation=any(word in lower for word in ("delete", "destroy", "drop")),
            degraded_mode_policy="deny" if protected else "allow_with_penalty",
            verification=verification.to_dict(),
        )
        workflow = WorkflowPlan(
            steps=steps,
            verification=verification,
            fallback_allowed=False,
            metadata={
                "workflow": kind.value,
                "model": None,
                "policy_floor": "protected" if protected else "standard",
            },
        )
        return PlanResult(task, workflow, {"workflow": kind.value, "model": None})

    def _sanitize_proposal(self, request: dict[str, Any], proposed: dict[str, Any]) -> PlanResult:
        fallback = self._deterministic(request)
        task_payload = proposed.get("task_spec")
        workflow_payload = proposed.get("workflow_plan")
        task = (
            TaskSpec.from_dict(task_payload)
            if isinstance(task_payload, dict)
            else fallback.task_spec
        )
        try:
            workflow = (
                WorkflowPlan.from_dict(workflow_payload)
                if isinstance(workflow_payload, dict)
                else fallback.workflow_plan
            )
        except ContractValidationError:
            # A structured planner is advisory.  Invalid or unsafe workflow
            # proposals are discarded and the deterministic plan wins.
            workflow = fallback.workflow_plan
        protected = fallback.task_spec.degraded_mode_policy == "deny"
        effort_ranks = {"low": 0, "medium": 1, "high": 2}
        proposed_effort = task.effort if task.effort in effort_ranks else fallback.task_spec.effort
        effort = (
            fallback.task_spec.effort
            if effort_ranks[proposed_effort] < effort_ranks[fallback.task_spec.effort]
            else proposed_effort
        )
        capacity_floor = {
            **fallback.task_spec.budget,
            "estimated_tokens": _EFFORT_TOKEN_RESERVATIONS[effort],
            "estimated_usd": _EFFORT_COST_RESERVATIONS[effort],
            "estimated_latency_ms": _EFFORT_LATENCY_RESERVATIONS[effort],
            "estimate_basis": _ESTIMATE_BASIS,
        }
        task = replace(
            task,
            objective=fallback.task_spec.objective,
            criticality=fallback.task_spec.criticality,
            production_impact=fallback.task_spec.production_impact or task.production_impact,
            destructive_operation=fallback.task_spec.destructive_operation
            or task.destructive_operation,
            degraded_mode_policy="deny" if protected else task.degraded_mode_policy,
            effort=effort,
            required_capabilities=list(
                dict.fromkeys(
                    [*fallback.task_spec.required_capabilities, *task.required_capabilities]
                )
            ),
            budget=self._apply_capacity_floor(task.budget, capacity_floor),
        )
        if protected:
            workflow = replace(
                workflow,
                fallback_allowed=False,
                metadata={**workflow.metadata, "model": None, "policy_floor": "protected"},
                steps=[{"action": "human_approval", "required": True}, *workflow.steps],
            )
        else:
            workflow = replace(workflow, metadata={**workflow.metadata, "model": None})
        return PlanResult(
            task, workflow, {"workflow": workflow.metadata.get("workflow"), "model": None}
        )

    def _enforce_budget(self, task: TaskSpec, budget: dict[str, Any] | None) -> None:
        try:
            estimated = _finite_non_negative_number(
                task.budget.get("estimated_usd", task.budget.get("max_usd", 0)), "estimated budget"
            )
            requested = (
                None
                if (budget or {}).get("max_usd") is None
                else _finite_non_negative_number((budget or {})["max_usd"], "requested budget")
            )
            limit = (
                None
                if self.policy.max_cost_usd is None
                else _finite_non_negative_number(self.policy.max_cost_usd, "planner budget")
            )
        except (OverflowError, TypeError, ValueError) as exc:
            raise PlanRejected("budget must be a finite non-negative number") from exc
        if requested is not None and estimated > requested:
            raise PlanRejected("plan exceeds requested budget")
        if limit is not None and estimated > limit:
            raise PlanRejected("budget exceeds planner policy")

    @staticmethod
    def _apply_capacity_floor(
        proposed_budget: Mapping[str, Any], fallback_budget: Mapping[str, Any]
    ) -> dict[str, Any]:
        try:
            proposed_tokens = _finite_non_negative_integer(
                proposed_budget.get("estimated_tokens", 0), "estimated_tokens"
            )
            proposed_cost = _finite_non_negative_number(
                proposed_budget.get("estimated_usd", 0), "estimated_usd"
            )
            proposed_latency = _finite_non_negative_integer(
                proposed_budget.get("estimated_latency_ms", 0), "estimated_latency_ms"
            )
        except (OverflowError, TypeError, ValueError) as exc:
            raise PlanRejected("planner estimate must be finite and non-negative") from exc
        result = dict(proposed_budget)
        result["estimated_tokens"] = max(proposed_tokens, int(fallback_budget["estimated_tokens"]))
        result["estimated_usd"] = max(proposed_cost, float(fallback_budget["estimated_usd"]))
        result["estimated_latency_ms"] = max(
            proposed_latency, int(fallback_budget["estimated_latency_ms"])
        )
        result["estimate_basis"] = fallback_budget["estimate_basis"]
        for key in ("max_usd", "remaining_usd"):
            if key in fallback_budget:
                result[key] = fallback_budget[key]
        return result


def _finite_non_negative_number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{label} must be numeric")
    parsed = float(value)
    if not math.isfinite(parsed) or parsed < 0:
        raise ValueError(f"{label} must be finite and non-negative")
    return parsed


def _finite_non_negative_integer(value: Any, label: str) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not math.isfinite(float(value))
        or value < 0
    ):
        raise ValueError(f"{label} must be a finite non-negative integer")
    return value


# Compatibility names used by integrations that prefer explicit boundaries.
IntakePlanner = StructuredPlanner
WorkflowSelector = StructuredPlanner
