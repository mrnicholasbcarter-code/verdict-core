"""Credential-free, deterministic TaskSpec and eligibility walkthrough.

Run from the repository root with ``python scripts/flagship_demo.py``. The
fixture uses only in-memory catalog/runtime observations; it never calls a
provider, reads credentials, or writes a decision log.

For a clean-environment smoke check, install the project into a fresh virtual
environment and run this file from the repository root, or invoke it by absolute
path after installing the wheel. The output remains deterministic because the
fixture does not depend on ambient credentials, network access, or mutable local
state.

When running directly from a source checkout without installing the package, set
``PYTHONPATH=.`` first.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from llm_gate.availability import (
    CandidateRequirements,
    RuntimeObservation,
    explain_candidates,
    normalize_observation,
    select_capable_candidates,
)
from llm_gate.contracts import RoutingDecisionContract, TaskSpec
from llm_gate.models import ModelInfo

NOW = datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc)


def build_demo_result() -> dict[str, Any]:
    """Return the stable, JSON-compatible result used by the public demo."""
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


def main() -> None:
    print(json.dumps(build_demo_result(), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
