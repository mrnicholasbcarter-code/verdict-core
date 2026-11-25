"""Offline tests for the generic harness contracts layer (BOD-286 slices S1 and S3).

These tests exercise ``verdict/orchestration/harness.py`` with no provider
calls, no network, and an isolated ``VERDICT_HOME``.  They prove:

* the registry resolves production adapters (prime-headless, direct-gateway)
  and the legacy ``prime`` alias;
* PrimeHeadlessExecutor/DirectGatewayExecutor construction is byte-identical
  to before the refactor;
* capabilities are explicitly enumerated, never inferred;
* harness identity never implies provider/model identity (architecture test);
* a fake (non-Prime) harness registered through the same registry drives a
  bounded node, proving the registry is not Prime-specific (slice S3).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from verdict.orchestration.contracts import CapacityClass, OrchestrationError, WorkerTerminal
from verdict.orchestration.executors import (
    DirectGatewayExecutor,
    FaultInjectingExecutor,
    MixedExecutor,
    PrimeHeadlessExecutor,
    ScriptedExecutor,
)
from verdict.orchestration.harness import (
    DEFAULT_REGISTRY,
    CapabilityLevel,
    ExecutionTarget,
    HarnessCapabilities,
    HarnessCapability,
    HarnessRegistry,
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
                HarnessCapability.WORKER_LAUNCH: CapabilityLevel.SUPPORTED
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
    # ADR-053 Decision 2 (amended 2026-11-25): OpenCode is the default; direct-gateway and
    # Prime stay registered and are selected by name.
    assert DEFAULT_REGISTRY.default_name() == "opencode"


def test_opencode_is_the_default_and_direct_gateway_stays_selectable() -> None:
    """ADR-053 Decision 2, amended 2026-11-25 (#1328): OpenCode is the default worker executor.

    The previous default (``direct-gateway``) and Prime stay registered and resolvable by name
    (``--executor direct-gateway``); naming one builds that adapter's executor, and the default
    builds OpenCode's.
    """
    from verdict.orchestration.executor_opencode import OpenCodeExecutor

    assert DEFAULT_REGISTRY.default_name() == "opencode"
    assert DEFAULT_REGISTRY.resolve(DEFAULT_REGISTRY.default_name()).name == "opencode"
    for name in ("direct-gateway", "prime-headless", "prime"):
        assert name in DEFAULT_REGISTRY.names()
        assert DEFAULT_REGISTRY.resolve(name).name != DEFAULT_REGISTRY.default_name()
    default_target = ExecutionTarget(
        harness=DEFAULT_REGISTRY.default_name(), capacity_class=CapacityClass.UNKNOWN
    )
    assert isinstance(
        DEFAULT_REGISTRY.executor_for(default_target, gateway="http://x:20128", api_key=""),
        OpenCodeExecutor,
    )
    named = ExecutionTarget(harness="direct-gateway", capacity_class=CapacityClass.UNKNOWN)
    assert isinstance(
        DEFAULT_REGISTRY.executor_for(named, gateway="http://x:20128/v1", api_key="k"),
        DirectGatewayExecutor,
    )


def _default_opencode_executor() -> Any:
    target = ExecutionTarget(
        harness=DEFAULT_REGISTRY.default_name(), capacity_class=CapacityClass.UNKNOWN
    )
    return DEFAULT_REGISTRY.executor_for(
        target, gateway="http://x:20128", api_key="", serve_url="http://127.0.0.1:9"
    )


def _run_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, inner: WorkerTerminal
) -> tuple[WorkerTerminal, list[dict[str, Any]]]:
    """Run the default executor with the OpenCode executor's own ``run`` replaced by ``inner``."""
    from verdict.orchestration.executor_opencode import OpenCodeExecutor

    calls: list[dict[str, Any]] = []

    async def fake_run(self: Any, prompt: str, **kwargs: Any) -> WorkerTerminal:
        calls.append({"prompt": prompt, **kwargs})
        return inner

    monkeypatch.setattr(OpenCodeExecutor, "run", fake_run)
    executor = _default_opencode_executor()
    terminal = asyncio.run(
        executor.run(
            "task", route_id="cc/x", cwd=tmp_path, timeout_seconds=5.0, run_id="r1", node_id="n1"
        )
    )
    return terminal, calls


@pytest.mark.parametrize(
    "reason",
    [
        "opencode binary not found or not executable at 'opencode'",
        "verdict serve is unreachable at http://127.0.0.1:8000",
        "the worker assignment signing key is unavailable (key_unavailable); "
        "run 'verdict setup --apply --allow host.serve' to create it",
    ],
)
def test_a_configuration_block_names_the_way_out_of_the_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, reason: str
) -> None:
    """With OpenCode as the default, every ``harness_configuration:`` block of its worker also
    names what the default needs and ``--executor direct-gateway``; its own reason stays first."""
    from verdict.orchestration.executor_opencode import OPENCODE_BIN_ENV, PINNED_VERSION
    from verdict.orchestration.recovery import FailureIntelligence

    blocked = WorkerTerminal(
        ok=False, model="cc/x", harness="opencode", error=f"harness_configuration: {reason}"
    )
    terminal, calls = _run_default(monkeypatch, tmp_path, blocked)
    assert terminal.error.startswith(f"harness_configuration: {reason}")
    assert "--executor direct-gateway" in terminal.error
    assert terminal.error.count("--executor direct-gateway") == 1
    # What the default needs, so the operator can fix the machine instead of leaving the default.
    assert f"opencode {PINNED_VERSION}" in terminal.error
    assert OPENCODE_BIN_ENV in terminal.error
    assert "verdict serve" in terminal.error
    assert "verdict setup --apply --allow host.serve" in terminal.error
    # The rest of the terminal is the executor's own, untouched; recovery still blocks, once.
    assert (terminal.ok, terminal.model, terminal.harness) == (False, "cc/x", "opencode")
    failure = FailureIntelligence().classify(terminal, now=datetime.now(timezone.utc))
    assert (failure.category, failure.action) == ("harness_configuration", "BLOCK")
    # The run id and node id reach the OpenCode executor (they sign the worker's assignment).
    assert calls == [
        {
            "prompt": "task",
            "route_id": "cc/x",
            "cwd": tmp_path,
            "timeout_seconds": 5.0,
            "run_id": "r1",
            "node_id": "n1",
        }
    ]


@pytest.mark.parametrize(
    "inner",
    [
        WorkerTerminal(ok=True, output="RESULT: DONE", model="cc/x", harness="opencode"),
        WorkerTerminal(ok=False, model="cc/x", harness="opencode", error="timeout"),
        WorkerTerminal(
            ok=False, model="cc/x", harness="opencode", error="worker_error: RuntimeError"
        ),
        WorkerTerminal(
            ok=False,
            model="cc/x",
            harness="opencode",
            error="provider said: harness_configuration: not a block",
        ),
    ],
)
def test_the_way_out_is_added_to_configuration_blocks_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, inner: WorkerTerminal
) -> None:
    terminal, _ = _run_default(monkeypatch, tmp_path, inner)
    assert terminal == inner


def test_the_default_executor_takes_the_ids_and_keeps_the_opencode_label() -> None:
    """The runtime offers ``run_id`` and ``node_id`` only to an executor whose ``run`` takes
    them; the receipt label for the default's executor is ``opencode``."""
    from verdict.orchestration.contracts import _accepts_keyword
    from verdict.orchestration.executor_opencode import OpenCodeExecutor

    executor = _default_opencode_executor()
    assert isinstance(executor, OpenCodeExecutor)
    assert _accepts_keyword(executor, "run_id") and _accepts_keyword(executor, "node_id")
    assert DEFAULT_REGISTRY.label_for(executor) == "opencode"
    assert DEFAULT_REGISTRY.label_for(OpenCodeExecutor(serve_base_url="http://127.0.0.1:9")) == (
        "opencode"
    )


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
        {"id": "auto/reasoning:pro", "owned_by": "combo"},  # opaque, excluded
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
    assert (
        DEFAULT_REGISTRY.label_for(DirectGatewayExecutor(base_url="http://x", api_key=""))
        == "direct-gateway"
    )
    assert (
        DEFAULT_REGISTRY.label_for(FaultInjectingExecutor(PrimeHeadlessExecutor(), {}))
        == "fault-injecting"
    )
    assert (
        DEFAULT_REGISTRY.label_for(
            ScriptedExecutor(lambda prompt, route_id, cwd: WorkerTerminal(ok=True, model=""))
        )
        == "scripted"
    )
    assert DEFAULT_REGISTRY.label_for(MixedExecutor({}, PrimeHeadlessExecutor())) == "mixed"
    # unknown executor falls back to class name, never crashes
    assert DEFAULT_REGISTRY.label_for(object()) == "object"


# --------------------------------------------------------------------------- slice S3: a fake (non-Prime) harness through the registry


class FakeHarnessAdapter:
    name = "fake-harness"
    executor_label = "scripted"
    executor_class = None

    def capabilities(self) -> HarnessCapabilities:
        return HarnessCapabilities(
            harness="fake-harness",
            facets={
                HarnessCapability.WORKER_LAUNCH: CapabilityLevel.SUPPORTED,
                HarnessCapability.INTERACTIVE_ATTACH: CapabilityLevel.UNSUPPORTED,
                HarnessCapability.MODEL_SWITCHING: CapabilityLevel.UNSUPPORTED,
                HarnessCapability.CONTEXT_INJECTION: CapabilityLevel.UNSUPPORTED,
                HarnessCapability.RESUME_CHECKPOINT: CapabilityLevel.NOT_INSTALLED,
                HarnessCapability.MCP: CapabilityLevel.NOT_INSTALLED,
                HarnessCapability.HOOKS: CapabilityLevel.NOT_INSTALLED,
                HarnessCapability.TOOL_INTERCEPTION: CapabilityLevel.UNSUPPORTED,
                HarnessCapability.EVENT_STREAM: CapabilityLevel.NOT_INSTALLED,
            },
            notes=("offline proof: no Prime, no gateway, no identity inference",),
        )

    def worker_executor(self, *, gateway: str, api_key: str):
        # The delegate is a ScriptedExecutor: it never touches the network and
        # self-stamps harness="scripted", distinct from any live harness name.
        return ScriptedExecutor(
            lambda prompt, route_id, cwd: WorkerTerminal(
                ok=True,
                output=f"fake:{route_id}",
                model=route_id,
                executor_kind="offline",
                harness="scripted",
            )
        )

    def harness_visibility(self, *, catalog_rows: Any, sync_visibility: bool):
        from verdict.orchestration.eligibility import HarnessVisibility

        return HarnessVisibility(None, source="none")

    def label(self) -> str:
        return self.executor_label

    def not_implemented(self, feature: str) -> None:
        raise OrchestrationError(f"fake-harness does not implement {feature}")


def test_fake_harness_adapter_registered_and_resolvable() -> None:
    # A fresh registry (never DEFAULT_REGISTRY, to avoid polluting production).
    registry = HarnessRegistry()
    adapter = FakeHarnessAdapter()
    registry.register(adapter)
    assert registry.resolve("fake-harness") is adapter
    assert "fake-harness" in registry.names()
    with pytest.raises(OrchestrationError, match="unknown harness adapter"):
        registry.resolve("bogus")


def test_fake_harness_capabilities_explicit() -> None:
    registry = HarnessRegistry()
    registry.register(FakeHarnessAdapter())
    caps = registry.resolve("fake-harness").capabilities()
    assert caps.harness == "fake-harness"
    assert set(caps.facets) == set(HarnessCapability)
    assert caps.facets[HarnessCapability.WORKER_LAUNCH] == CapabilityLevel.SUPPORTED
    # everything else explicitly unsupported/not-installed, never inferred
    for cap in (
        HarnessCapability.INTERACTIVE_ATTACH,
        HarnessCapability.MODEL_SWITCHING,
        HarnessCapability.CONTEXT_INJECTION,
        HarnessCapability.TOOL_INTERCEPTION,
    ):
        assert caps.facets[cap] in (CapabilityLevel.UNSUPPORTED, CapabilityLevel.NOT_INSTALLED)


def test_fake_harness_adapter_runs_offline_node(tmp_path: Path) -> None:
    """A non-Prime harness registered through the same registry drives a
    bounded node; the terminal carries harness="scripted", not prime-headless."""
    registry = HarnessRegistry()
    registry.register(FakeHarnessAdapter())
    executor = registry.resolve("fake-harness").worker_executor(gateway="http://unused", api_key="")
    terminal = asyncio.run(
        executor.run("prompt", route_id="fake-cc/claude-sonnet-5", cwd=tmp_path, timeout_seconds=5)
    )
    assert terminal.ok
    assert terminal.model == "fake-cc/claude-sonnet-5"
    assert terminal.harness == "scripted"
    assert terminal.executor_kind == "scripted"
