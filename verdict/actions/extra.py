"""Additional bounded read-only actions for commands reclassified from other buckets.

BOD-275 Lane E: Commands reclassified to ACTION get their implementations here.
"""

from __future__ import annotations

from typing import Any

from verdict.actions.base import ActionResult


def _action_inspect(**kwargs: Any) -> ActionResult:
    """Inspect one model's catalog record and any stored passport evidence."""
    from verdict.actions.helpers import default_model_catalog

    model_id: str = kwargs["model_id"]
    catalog = kwargs.get("catalog") or default_model_catalog()

    matches = [
        m for m in catalog
        if m.id == model_id or f"{m.provider}/{m.id}" == model_id
    ]

    if not matches:
        return ActionResult(
            data={"error": f"model not found in catalog: {model_id}"},
            ok=False,
            exit_code=1,
        )

    model = matches[0]

    payload: dict[str, Any] = {
        "id": model.id,
        "provider": model.provider,
        "tier": model.capability_tier,
        "context_window": model.context_window,
        "cost_per_1k": model.cost_per_1k,
        "capabilities": sorted(model.capabilities),
        "availability_state": model.availability_state,
    }

    return ActionResult(data=payload)
