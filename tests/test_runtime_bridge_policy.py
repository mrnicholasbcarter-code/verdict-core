from __future__ import annotations

import inspect
import runpy
from pathlib import Path


BRIDGE = Path(".prime/agent/skills/verdict-dispatch/scripts/runtime_bridge.py")


def test_runtime_bridge_requires_controller_identity_and_scopes_workers_to_cc_kr() -> None:
    module = runpy.run_path(str(BRIDGE))
    operation = module["PrimeWorkerOperation"]
    signature = inspect.signature(operation.__init__)

    assert signature.parameters["controller_model"].default is inspect.Parameter.empty
    assert module["DEFAULT_ALLOWED_ROUTE_PREFIXES"] == ("cc/", "kr/")
    assert module["_route_id"]("omniroute/cc/claude-fable-5-1") == "cc/claude-fable-5-1"


def test_runtime_bridge_keeps_legacy_controllers_out_of_worker_pool() -> None:
    module = runpy.run_path(str(BRIDGE))
    assert module["LEGACY_CONTROLLER_MODELS"] == frozenset(
        {"cx/gpt-5.6-sol", "cx/gpt-6-astra"}
    )
