"""Architecture test: harness identity never implies provider/model identity.

Runs offline against the AST of ``verdict/orchestration/harness.py``.  This is
the enforcement test for BOD-286 acceptance criterion 7 and invariant 1.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from verdict.orchestration import DEFAULT_REGISTRY
from verdict.orchestration.contracts import OrchestrationError
from verdict.orchestration.harness import ExecutionTarget, HarnessCapability, HarnessRegistry

HARNESS_MODULE = Path(__file__).resolve().parents[1] / "verdict" / "orchestration" / "harness.py"


# --------------------------------------------------------------------------- ExecutionTarget fields


def _execution_target_fields() -> dict[str, ast.arg]:
    tree = ast.parse(HARNESS_MODULE.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "ExecutionTarget":
            # dataclass fields are ``annassign`` in the body
            fields: dict[str, ast.arg] = {}
            for item in node.body:
                if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
                    fields[item.target.id] = item  # type: ignore[assignment]
            return fields
    raise AssertionError("ExecutionTarget class not found")


def test_execution_target_has_disjoint_harness_provider_model_fields() -> None:
    fields = _execution_target_fields()
    for required in (
        "harness",
        "transport",
        "provider",
        "model",
        "capacity_class",
        "reasoning_effort",
        "compatibility_profile",
    ):
        assert required in fields, f"missing ExecutionTarget field {required!r}"
    # harness, provider, model, transport are distinct fields
    names = {f for f in fields if f in {"harness", "transport", "provider", "model"}}
    assert names == {"harness", "transport", "provider", "model"}


def test_execution_target_round_trips_all_dimensions() -> None:
    target = ExecutionTarget(
        harness="prime-headless",
        transport="openai-compat",
        provider="cc",
        model="cc/claude-sonnet-5",
        capacity_class=__import__(
            "verdict.orchestration.contracts", fromlist=["CapacityClass"]
        ).CapacityClass.FREE,
        reasoning_effort="high",
        compatibility_profile="prime-0.9.8",
    )
    restored = ExecutionTarget(
        **{f.name: getattr(target, f.name) for f in target.__dataclass_fields__.values()}
    )
    assert restored == target
    assert restored.harness != restored.provider
    assert restored.provider != restored.model


# --------------------------------------------------------------------------- registry is name-based only


def test_registry_lookup_is_by_adapter_name_only() -> None:
    assert DEFAULT_REGISTRY.resolve("prime-headless") is not DEFAULT_REGISTRY.resolve(
        "direct-gateway"
    )
    assert DEFAULT_REGISTRY.resolve("prime") is DEFAULT_REGISTRY.resolve("prime-headless")
    with pytest.raises(OrchestrationError, match="unknown harness adapter"):
        DEFAULT_REGISTRY.resolve("no-such-adapter")


def test_harness_registry_is_wired_into_production_callers() -> None:
    """S1 adds the module; S2 wires the single production callers through it.

    cli.py (executor construction + visibility), eligibility_report.py
    (build_selector visibility delegation) and run.py (receipt/executor labels)
    all resolve adapters via DEFAULT_REGISTRY. executors.py does not import
    harness -- harness imports executors, not the reverse.
    """
    import subprocess

    output = subprocess.run(
        [
            "git",
            "grep",
            "-l",
            "from verdict.orchestration.harness import",
            "--",
            "verdict/orchestration/",
        ],
        capture_output=True,
        text=True,
    ).stdout.split()
    files = {Path(f).name for f in output}
    assert files == {"cli.py", "eligibility_report.py", "run.py", "__init__.py"}, (
        f"unexpected production importers of harness: {files}"
    )


# --------------------------------------------------------------------------- HarnessRegistry contract


def test_registry_rejects_adapter_without_all_capability_facets() -> None:
    class Incomplete:
        name = "incomplete"
        executor_label = "incomplete"
        executor_class = None

        def capabilities(self):
            from verdict.orchestration.harness import (
                CapabilityLevel,
                HarnessCapabilities,
                HarnessCapability,
            )

            return HarnessCapabilities(
                harness="incomplete",
                facets={HarnessCapability.WORKER_LAUNCH: CapabilityLevel.SUPPORTED},
                notes=("missing facets",),
            )

        def worker_executor(self, *, gateway, api_key):
            raise NotImplementedError

        def harness_visibility(self, *, catalog_rows, sync_visibility):
            raise NotImplementedError

        def label(self):
            return self.executor_label

        def not_implemented(self, feature):
            raise OrchestrationError("nyi")

    registry = HarnessRegistry()
    with pytest.raises(OrchestrationError, match="omits capability facets"):
        registry.register(Incomplete())  # type: ignore[arg-type]


def test_harness_capability_enum_is_exhaustive() -> None:
    assert {
        HarnessCapability.WORKER_LAUNCH,
        HarnessCapability.INTERACTIVE_ATTACH,
        HarnessCapability.MODEL_SWITCHING,
        HarnessCapability.CONTEXT_INJECTION,
        HarnessCapability.RESUME_CHECKPOINT,
        HarnessCapability.MCP,
        HarnessCapability.HOOKS,
        HarnessCapability.TOOL_INTERCEPTION,
        HarnessCapability.EVENT_STREAM,
    } == set(HarnessCapability)
