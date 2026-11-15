"""Offline tests for the generic harness contracts layer (BOD-286 slice S1).

These tests exercise ``verdict/orchestration/harness.py`` with no provider
calls, no network, and an isolated ``VERDICT_HOME``.  They prove:

* the registry resolves production adapters (prime-headless, direct-gateway)
  and the legacy ``prime`` alias;
* PrimeHeadlessExecutor/DirectGatewayExecutor construction is byte-identical
  to before the refactor;
* capabilities are explicitly enumerated, never inferred;
* harness identity never implies provider/model identity (architecture test).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from verdict.orchestration import DEFAULT_REGISTRY, HarnessRegistry
from verdict.orchestration.contracts import CapacityClass, OrchestrationError, WorkerTerminal
from verdict.orchestration.executors import (
    DirectGatewayExecutor,
    FaultInjectingExecutor,
    MixedExecutor,
    PrimeHeadlessExecutor,
    ScriptedExecutor,
)
from verdict.orchestration.harness import (
    CapabilityLevel,
    ExecutionTarget,
    HarnessCapabilities,
    HarnessCapability,
    OmittedCapabilityError,
)


# --------------------------------------------------------------------------- fixtures


class _FakeAdapter:
    """A minimal adapter missing one capability facet, for rejection tests."""

    name = "fake-missing"
    executor_label = "fake-missing"
    executor_class = None

    def capabilities(self) -> HarnessCapabilities:
        return HarnessCapabilities(
            harness="fake-missing",
            facets={
                HarnessCapability.WORKER_LAUNCH: CapabilityLevel.SUPPORTED,
                # every other facet deliberately omitted
            },
            notes=("incomplete adapter, must be rejected at registration",),
        )

    def worker_executor(self, *, gateway: str, api_key: str):
        raise NotImplementedError

    def harness_visibility(self, *, catalog_rows: Any, sync_visibility: bool):
        from verdict.orchestration.eligibility import HarnessVisibility

        return HarnessVisibility(None, source="none")

    def label(self) -> str:
        return self.executor_label

    def not_implemented(self, feature: str) -> None:
        raise OrchestrationError(f"fake-missing does not implement {feature}")


# --------------------------------------------------------------------------- registry resolution


def test_registry_resolves_production_adapters_and_aliases() -> None:
    ph = DEFAULT_REGISTRY.resolve("prime-headless")
    dg = DEFAULT_REGISTRY.resolve("direct-gateway")
    assert ph.name == "prime-headless"
    assert dg.name == "direct-gateway"
    # legacy ``prime`` alias resolves to the same PrimeHeadlessAdapter
    assert DEFAULT_REGISTRY.resolve("prime") is ph
    assert "prime-headless" in DEFAULT_REGISTRY.names()
    assert "direct-gateway" in DEFAULT_REGISTRY.names()
    assert "prime" in DEFAULT_REGISTRY.names()
    assert DEFAULT_REGISTRY.default_name() == "prime-headless"


def test_unknown_adapter_raises() -> None:
    with pytest.raises(OrchestrationError, match="unknown harness adapter"):
        DEFAULT_REGISTRY.resolve("bogus")


def test_prime_headless_adapter_builder_reuses_constructor() -> None:
    executor = DEFAULT_REGISTRY.resolve("prime-headless").worker_executor(
        gateway="http://x:20128", api_key=""
    )
    assert isinstance(executor, PrimeHeadlessExecutor)
    assert executor.prime_bin == "prime-agent"
    assert executor.provider == "omniroute"
    assert executor.extra_args == ("-nc", "-ns")
    assert executor.thinking is None
    assert executor.env is None


def test_direct_gateway_adapter_builder() -> None:
    executor = DEFAULT_REGISTRY.resolve("direct-gateway").worker_executor(
        gateway="http://x:20128/v1", api_key="k"
    )
    assert isinstance(executor, DirectGatewayExecutor)
    assert str(executor.base_url).rstrip("/") == "http://x:20128/v1"


def test_capabilities_explicit_on_both_adapters() -> None:
    for name in ("prime-headless", "direct-gateway", "prime"):
        caps = DEFAULT_REGISTRY.resolve(name).capabilities()
        assert caps.harness == ("prime-headless" if name != "direct-gateway" else "direct-gateway")
        assert set(caps.facets) == set(HarnessCapability)
        for level in caps.facets.values():
            assert level in CapabilityLevel


def test_prime_capabilities_match_certify_facets() -> None:
    """Prime's explicit facets mirror ``harness_prime.certify()`` today."""
    caps = DEFAULT_REGISTRY.resolve("prime-headless").capabilities()
    assert caps.facets[HarnessCapability.WORKER_LAUNCH] == CapabilityLevel.SUPPORTED
    assert caps.facets[HarnessCapability.INTERACTIVE_ATTACH] == CapabilityLevel.UNSUPPORTED
    assert caps.facets[HarnessCapability.MODEL_SWITCHING] == CapabilityLevel.PARTIAL
    assert caps.facets[HarnessCapability.CONTEXT_INJECTION] == CapabilityLevel.PARTIAL
    assert caps.facets[HarnessCapability.RESUME_CHECKPOINT] == CapabilityLevel.PARTIAL
    assert caps.facets[HarnessCapability.MCP] == CapabilityLevel.PARTIAL
    assert caps.facets[HarnessCapability.HOOKS] == CapabilityLevel.UNSUPPORTED
    assert caps.facets[HarnessCapability.TOOL_INTERCEPTION] == CapabilityLevel.UNSUPPORTED
    assert caps.facets[HarnessCapability.EVENT_STREAM] == CapabilityLevel.PARTIAL


def test_registered_adapter_must_not_omit_capabilities() -> None:
    registry = HarnessRegistry()
    with pytest.raises(OmittedCapabilityError, match="omits capability facets"):
        registry.register(_FakeAdapter())


# --------------------------------------------------------------------------- visibility delegation


def test_direct_gateway_visibility_comes_from_live_catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    catalog_rows = [
        {"id": "cc/claude-sonnet-5", "owned_by": "anthropic"},
        {"id": "auto/reasoning:pro", "owned_by": "combo"},   # opaque, excluded
        {"id": "kg/model", "owned_by": "x"},
    ]
    vis = DEFAULT_REGISTRY.resolve("direct-gateway").harness_visibility(
        catalog_rows=catalog_rows, sync_visibility=False
    )
    assert vis.available is True
    assert vis.source == "live-gateway"
    assert vis.ids is not None
    ids = vis.ids
    assert "cc/claude-sonnet-5" in ids
    assert "kg/model" in ids
    assert "auto/reasoning:pro" not in ids  # opaque excluded


def test_prime_visibility_is_none_without_registry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("VERDICT_HOME", str(tmp_path))
    vis = DEFAULT_REGISTRY.resolve("prime-headless").harness_visibility(
        catalog_rows=[], sync_visibility=False
    )
    assert vis.available is False
    assert vis.source == "none"
    assert vis.ids is None


# --------------------------------------------------------------------------- ExecutionTarget


def test_execution_target_fields_are_disjoint() -> None:
    target = ExecutionTarget(
        harness="prime-headless",
        transport="openai-compat",
        provider="cc",
        model="cc/claude-sonnet-5",
        capacity_class=CapacityClass.SUBSCRIPTION,
        reasoning_effort="high",
        compatibility_profile="prime-0.9.8",
    )
    assert target.harness != target.provider
    assert target.provider != target.model
    assert target.model != target.harness
    assert target.transport == "openai-compat"
    assert target.capacity_class == CapacityClass.SUBSCRIPTION


def test_registry_label_map_covers_production_executors() -> None:
    assert DEFAULT_REGISTRY.label_for(PrimeHeadlessExecutor()) == "prime-headless"
    assert DEFAULT_REGISTRY.label_for(DirectGatewayExecutor(base_url="http://x", api_key="")) == "direct-gateway"
    assert DEFAULT_REGISTRY.label_for(FaultInjectingExecutor(PrimeHeadlessExecutor(), {})) == "fault-injecting"
    assert (
        DEFAULT_REGISTRY.label_for(
            ScriptedExecutor(lambda prompt, route_id, cwd: WorkerTerminal(ok=True, model=""))
        )
        == "scripted"
    )
    assert DEFAULT_REGISTRY.label_for(MixedExecutor({}, PrimeHeadlessExecutor())) == "mixed"
    # unknown executor falls back to class name, never crashes
    assert DEFAULT_REGISTRY.label_for(object()) == "object"
