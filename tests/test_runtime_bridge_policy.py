from __future__ import annotations

import runpy
from pathlib import Path


BRIDGE = Path(".prime/agent/skills/verdict-dispatch/scripts/runtime_bridge.py")


def test_runtime_bridge_injects_controller_exclusion_and_route_scope() -> None:
    module = runpy.run_path(str(BRIDGE))
    normalize = module.get("normalize_task_policy")
    assert callable(normalize)

    task = normalize(
        {"required_capabilities": ["tools"], "coding": True},
        controller_model="omniroute/cc/claude-fable-5-1",
        allowed_route_prefixes=["cc/", "kr/"],
    )

    assert task["allowed_route_prefixes"] == ["cc/", "kr/"]
    assert task["excluded_route_ids"] == ["cc/claude-fable-5-1"]
