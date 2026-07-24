from __future__ import annotations

import pytest

from verdict.availability import CandidateRequirements
from verdict.contracts import TaskSpec, WorkflowPlan
from verdict.planner import (
    FailureClass,
    PlannerPolicy,
    PlanningUnavailable,
    PlanRejected,
    StructuredPlanner,
    WorkflowKind,
)


def test_planner_returns_valid_typed_task_and_workflow() -> None:
    result = StructuredPlanner().plan("Research the API and implement it with tests")

    assert isinstance(result.task_spec, TaskSpec)
    assert isinstance(result.workflow_plan, WorkflowPlan)
    assert result.task_spec.effort == "high"
    assert result.workflow_plan.metadata["workflow"] == WorkflowKind.RESEARCH_IMPLEMENT.value
    assert result.workflow_plan.verification.checks


def test_unavailable_planner_uses_deterministic_fallback() -> None:
    def unavailable(_: dict[str, object]) -> dict[str, object]:
        raise PlanningUnavailable("planner offline")

    planner = StructuredPlanner(unavailable)
    first = planner.plan("Format a JSON document", criticality="low")
    second = planner.plan("Format a JSON document", criticality="low")

    assert first == second
    assert first.metadata["planner_mode"] == "deterministic-fallback"
    assert first.task_spec.to_dict()["criticality"] == "low"


def test_criticality_alone_does_not_select_a_model() -> None:
    low = StructuredPlanner().plan("Summarize a document", criticality="low")
    critical = StructuredPlanner().plan("Summarize a document", criticality="critical")

    assert low.task_spec.required_capabilities == critical.task_spec.required_capabilities
    assert low.task_spec.task_type == critical.task_spec.task_type
    assert low.workflow_plan.metadata["model"] is None
    assert critical.workflow_plan.metadata["model"] is None


def test_capability_requirements_uses_current_task_contract_fields() -> None:
    task = TaskSpec(
        objective="Implement a tested API client",
        task_type="implementation_then_test",
        effort="high",
        reasoning="high",
        tools=["shell", "editor"],
        required_capabilities=["tool-calling"],
        budget={"estimated_tokens": 32_768, "estimated_usd": 5.0, "max_usd": 8.0},
        verification={"checks": ["pytest -q"]},
        production_impact=True,
    )

    requirements = StructuredPlanner.capability_requirements(task)

    assert requirements == {
        "reasoning": "high",
        "tools": ["editor", "shell"],
        "required_capabilities": ["tool-calling"],
        "effort": "high",
        "verification": {"checks": ["pytest -q"]},
        "production_impact": True,
        "estimated_tokens": 32_768,
        "estimated_cost": 5.0,
        "budget_remaining": 8.0,
    }


def test_deterministic_planner_reserves_tokens_with_an_explicit_basis() -> None:
    result = StructuredPlanner().plan("Research the API and implement it with tests")

    assert result.task_spec.budget["estimated_tokens"] == 32_768
    assert result.task_spec.budget["estimated_usd"] == 5.0
    assert result.task_spec.budget["estimate_basis"] == "deterministic_effort_reservation_v1"


def test_planner_compiles_capacity_estimates_into_availability_requirements() -> None:
    planner = StructuredPlanner()
    task = planner.plan(
        "Research the API and implement it with tests", budget={"max_usd": 8.0}
    ).task_spec

    requirements = planner.availability_requirements(task)

    assert isinstance(requirements, CandidateRequirements)
    assert requirements.required == frozenset({"tools"})
    assert requirements.estimated_tokens == 32_768
    assert requirements.estimated_cost == 5.0
    assert requirements.budget_remaining == 8.0
    assert requirements.allow_degraded is True
    assert requirements.protected is False


def test_protected_plan_cannot_opt_into_unknown_capacity() -> None:
    planner = StructuredPlanner()
    task = planner.plan("Deploy the production API", budget={"max_usd": 8.0}).task_spec

    requirements = planner.availability_requirements(task)

    assert requirements.protected is True
    assert requirements.allow_degraded is False


def test_structured_proposal_cannot_understate_deterministic_capacity_floor() -> None:
    def understated(_: dict[str, object]) -> dict[str, object]:
        return {
            "task_spec": {
                "objective": "Research the API and implement it with tests",
                "task_type": "research_then_implementation",
                "effort": "high",
                "budget": {"estimated_tokens": 1, "estimated_usd": 0.001},
            }
        }

    result = StructuredPlanner(understated).plan("Research the API and implement it with tests")

    assert result.task_spec.budget["estimated_tokens"] == 32_768
    assert result.task_spec.budget["estimated_usd"] == 5.0
    assert result.task_spec.budget["estimate_basis"] == "deterministic_effort_reservation_v1"


def test_structured_proposal_that_raises_effort_also_raises_capacity_floor() -> None:
    def high_effort(_: dict[str, object]) -> dict[str, object]:
        return {
            "task_spec": {
                "objective": "Answer a question",
                "task_type": "single_model",
                "effort": "high",
                "budget": {"estimated_tokens": 1_024, "estimated_usd": 0.05},
            }
        }

    result = StructuredPlanner(high_effort).plan("Answer a question")

    assert result.task_spec.effort == "high"
    assert result.task_spec.budget["estimated_tokens"] == 32_768
    assert result.task_spec.budget["estimated_usd"] == 5.0


def test_structured_proposal_can_raise_latency_reservation() -> None:
    def slower(_: dict[str, object]) -> dict[str, object]:
        return {
            "task_spec": {
                "objective": "Answer a question",
                "task_type": "single_model",
                "effort": "low",
                "budget": {"estimated_latency_ms": 100_000},
            }
        }

    result = StructuredPlanner(slower).plan("Answer a question")

    assert result.task_spec.budget["estimated_latency_ms"] == 100_000


@pytest.mark.parametrize(
    "budget",
    [
        {"estimated_tokens": True},
        {"estimated_tokens": -1},
        {"estimated_tokens": 10**400},
        {"estimated_usd": False},
        {"estimated_usd": -0.01},
        {"estimated_usd": float("nan")},
        {"estimated_latency_ms": 1.5},
    ],
)
def test_invalid_structured_planner_estimate_is_rejected(budget) -> None:
    def invalid(_: dict[str, object]) -> dict[str, object]:
        return {
            "task_spec": {
                "objective": "Implement a feature",
                "task_type": "implementation_then_test",
                "budget": budget,
            }
        }

    with pytest.raises(PlanRejected, match="estimate"):
        StructuredPlanner(invalid).plan("Implement a feature")


@pytest.mark.parametrize("max_usd", [False, -1, float("nan"), 10**400])
def test_invalid_requested_budget_is_rejected(max_usd) -> None:
    with pytest.raises(PlanRejected, match="budget"):
        StructuredPlanner().plan("Implement a feature", budget={"max_usd": max_usd})


def test_planner_output_cannot_weaken_policy() -> None:
    def malicious(_: dict[str, object]) -> dict[str, object]:
        return {
            "task_spec": {
                "objective": "deploy production",
                "task_type": "deployment",
                "criticality": "low",
                "production_impact": False,
                "degraded_mode_policy": "allow",
                "budget": {"max_usd": 0.01},
            },
            "workflow_plan": {
                "steps": [{"action": "execute", "model": "cheap/unverified"}],
                "fallback_allowed": True,
                "metadata": {"policy_floor": "none", "model": "cheap/unverified"},
            },
        }

    result = StructuredPlanner(malicious).plan("deploy production", criticality="critical")

    assert result.task_spec.production_impact is True
    assert result.task_spec.criticality == "critical"
    assert result.task_spec.degraded_mode_policy == "deny"
    assert result.workflow_plan.fallback_allowed is False
    assert result.workflow_plan.metadata["model"] is None
    assert result.workflow_plan.metadata["policy_floor"] == "protected"
    assert "human_approval" in [step["action"] for step in result.workflow_plan.steps]


def test_over_budget_plan_is_rejected() -> None:
    planner = StructuredPlanner(policy=PlannerPolicy(max_cost_usd=1.0))
    with pytest.raises(PlanRejected, match="budget"):
        planner.plan("Implement a large distributed system", budget={"max_usd": 0.01})


def test_failure_classification_and_bounded_replanning() -> None:
    planner = StructuredPlanner(policy=PlannerPolicy(max_replans=1))
    assert planner.classify_failure("429 quota exceeded") is FailureClass.QUOTA_EXHAUSTION
    assert planner.classify_failure("permission denied by tool") is FailureClass.PERMISSION_DENIAL
    assert planner.classify_failure("pytest failed") is FailureClass.TEST_FAILURE

    result = planner.replan(planner.plan("Implement a feature"), "provider timeout", attempt=1)
    assert result.workflow_plan.metadata["replan_reason"] == FailureClass.PROVIDER_FAILURE.value
    with pytest.raises(PlanRejected, match="replan"):
        planner.replan(result, "provider timeout", attempt=2)
