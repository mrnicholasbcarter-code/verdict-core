"""Credential-free, deterministic flagship quickstart fixture.

The fixture uses only in-memory catalog and runtime observations. It never
calls a provider, reads credentials, accesses the network, or writes state.
That makes it safe to run from an installed wheel in an otherwise empty
environment.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from verdict.availability import (
    CandidateRequirements,
    RuntimeObservation,
    explain_candidates,
    normalize_observation,
    select_capable_candidates,
)
from verdict.contracts import RoutingDecisionContract, TaskSpec
from verdict.models import ModelInfo

NOW = datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc)


def build_demo_result() -> dict[str, Any]:
    """Return the stable, JSON-compatible result used by the quickstart."""

    task_spec = TaskSpec(
        objective="Add structured output to the invoice parser",
        task_type="coding",
        effort="medium",
        reasoning="medium",
        required_capabilities=["tools", "structured_output"],
        tools=["repository", "test_runner"],
        privacy="trusted_upstream",
        risk="high",
        production_impact=False,
        verification={"checks": ["unit_tests", "schema_validation"]},
        metadata={"fixture": "issue-35"},
    )
    candidates = [
        ModelInfo(
            id="demo/frontier-tools",
            provider="demo",
            capability_tier=1,
            capabilities=frozenset({"tools", "structured_output"}),
        ),
        ModelInfo(
            id="demo/no-tools",
            provider="demo",
            capability_tier=1,
            capabilities=frozenset({"structured_output"}),
        ),
        ModelInfo(
            id="demo/quota-empty",
            provider="demo",
            capability_tier=0,
            capabilities=frozenset({"tools", "structured_output"}),
        ),
        ModelInfo(
            id="demo/unverified",
            provider="demo",
            capability_tier=0,
            capabilities=frozenset({"tools", "structured_output"}),
        ),
    ]
    observations = {
        "demo/frontier-tools": RuntimeObservation(
            observed_at=NOW, source="fixture", health="healthy", quota_remaining_pct=80
        ),
        "demo/no-tools": RuntimeObservation(
            observed_at=NOW, source="fixture", health="healthy", quota_remaining_pct=80
        ),
        "demo/quota-empty": RuntimeObservation(
            observed_at=NOW, source="fixture", health="healthy", quota_remaining_pct=0
        ),
        "demo/unverified": RuntimeObservation(observed_at=NOW, source="fixture", health="unknown"),
    }
    states = [normalize_observation(model, observations[model.id], now=NOW) for model in candidates]
    requirements = CandidateRequirements(
        required=frozenset(task_spec.required_capabilities), protected=True
    )
    eligible = select_capable_candidates(states, requirements)
    explanation = explain_candidates(states, requirements)
    selected = eligible[0].model.id if eligible else None
    decision = RoutingDecisionContract(
        selected_route={"runtime_id": selected, "provider": "demo"},
        task_spec=task_spec.to_dict(),
        candidate_snapshot="fixture:issue-35",
        exclusions=[row for row in explanation if row["rejected"]],
        policy_floor="high",
        planner_mode="deterministic_fixture",
        explanation=(
            "Selected the only candidate satisfying required capabilities and "
            "fresh healthy availability; excluded all hard-gate failures."
        ),
        fallback_plan=[],
        policy_version="demo-policy-1",
    )
    return {
        "task_spec": task_spec.to_dict(),
        "requirements": {
            "required": sorted(requirements.required),
            "protected": requirements.protected,
        },
        "eligible": [item.model.id for item in eligible],
        "candidates": explanation,
        "decision": decision.to_dict(),
    }


def validate_demo_result(result: dict[str, Any]) -> None:
    """Fail closed if the quickstart fixture no longer proves its contract."""

    if result.get("eligible") != ["demo/frontier-tools"]:
        raise ValueError("quickstart fixture selected an unexpected route")
    selected = result.get("decision", {}).get("selected_route", {}).get("runtime_id")
    if selected != "demo/frontier-tools":
        raise ValueError("quickstart fixture decision is inconsistent")
    exclusions = result.get("decision", {}).get("exclusions")
    if not isinstance(exclusions, list) or len(exclusions) != 3:
        raise ValueError("quickstart fixture did not record all exclusions")


def run_demo() -> dict[str, Any]:
    """Build and validate one deterministic quickstart result."""

    result = build_demo_result()
    validate_demo_result(result)
    return result


def render_report(result: dict[str, Any]) -> str:
    """Render a stable human-readable report without terminal-specific markup."""

    decision = result["decision"]
    lines = [
        "Verdict credential-free quickstart",
        "===================================",
        f"Task: {result['task_spec']['objective']}",
        f"Required capabilities: {', '.join(result['requirements']['required'])}",
        f"Selected route: {decision['selected_route']['runtime_id']}",
        f"Excluded candidates: {len(decision['exclusions'])}",
        "Status: PASS",
    ]
    for exclusion in decision["exclusions"]:
        lines.append(f"- {exclusion['model']}: {exclusion['reason']}")
    return "\n".join(lines) + "\n"


__all__ = ["build_demo_result", "render_report", "run_demo", "validate_demo_result"]
