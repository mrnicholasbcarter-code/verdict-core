"""Generic harness contracts for Verdict orchestration.

Adapter/registry layer that separates *harness identity* (which coding-agent
harness runs a bounded work node) from transport/provider/model/capacity
identity.  Harness adapters translate Verdict decisions into launches; they
never rank models, broaden admission, or own cross-harness orchestration.

This module is intentionally small and versioned.  Every symbol it defines has
a production caller from day one so the reachability baseline does not grow.
"""

from __future__ import annotations

import inspect
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path
from typing import Any, Protocol, cast

from verdict.orchestration.contracts import (
    CapacityClass,
    OrchestrationError,
    WorkerExecutor,
    WorkerTerminal,
)
from verdict.orchestration.eligibility import HarnessVisibility

SCHEMA_VERSION = "verdict.harness/v1"


class HarnessCapability(str, Enum):
    """Explicit capability facets an adapter declares.  Omitted = unsafe."""

    WORKER_LAUNCH = "worker_launch"
    INTERACTIVE_ATTACH = "interactive_attach"
    MODEL_SWITCHING = "model_switching"
    CONTEXT_INJECTION = "context_injection"
    RESUME_CHECKPOINT = "resume_checkpoint"
    MCP = "mcp"
    HOOKS = "hooks"
    TOOL_INTERCEPTION = "tool_interception"
    EVENT_STREAM = "event_stream"


class CapabilityLevel(str, Enum):
    SUPPORTED = "supported"
    PARTIAL = "partial"
    UNSUPPORTED = "unsupported"
    NOT_INSTALLED = "not-installed"


class OmittedCapabilityError(OrchestrationError):
    """An adapter declares capabilities but omits a required facet."""


@dataclass(frozen=True)
class ExecutionTarget:
    """One bounded execution request: harness + identity + capacity.

    Harness identity never implies provider/model/transport identity; those
    are independent fields.  Harness adapters translate this target into a
    launch; they do not rewrite it.
    """

    harness: str  # adapter name registered in HarnessRegistry
    transport: str = ""  # "openai-compat" | "anthropic-messages" | "local" | custom
    provider: str = ""  # lower-case OmniRoute provider prefix
    model: str = ""  # bare OmniRoute route id, e.g. "cc/claude-sonnet-5"
    capacity_class: CapacityClass = CapacityClass.UNKNOWN
    reasoning_effort: str | None = None
    compatibility_profile: str = ""  # e.g. "prime-0.9.8", "claude-messages-1"


@dataclass(frozen=True)
class HarnessCapabilities:
    schema: str = SCHEMA_VERSION
    harness: str = ""
    facets: Mapping[HarnessCapability, CapabilityLevel] = field(default_factory=dict)
    notes: tuple[str, ...] = ()


class HarnessAdapter(Protocol):
    """Boundary between Verdict's control plane and one coding-agent harness."""

    name: str  # registry key, e.g. "prime-headless"

    def capabilities(self) -> HarnessCapabilities: ...
    def worker_executor(self, *, gateway: str, api_key: str) -> WorkerExecutor: ...
    def harness_visibility(
        self, *, catalog_rows: Any, sync_visibility: bool
    ) -> HarnessVisibility: ...
    def label(self) -> str: ...  # receipt/event executor_kind label fallback
    def not_implemented(self, feature: str) -> None: ...  # raises OrchestrationError


class HarnessRegistry:
    """Resolves adapters by name; the single place that maps harness -> adapter."""

    def __init__(self) -> None:
        self._adapters: dict[str, HarnessAdapter] = {}
        self._executor_labels: dict[type, str] = {}
        self._default: str | None = None

    def register(
        self,
        adapter: HarnessAdapter,
        *,
        default: bool = False,
        aliases: tuple[str, ...] = (),
        executor_class: type | None = None,
        executor_label: str | None = None,
    ) -> None:
        required = frozenset(HarnessCapability)
        missing = required - frozenset(adapter.capabilities().facets)
        if missing:
            raise OmittedCapabilityError(
                f"adapter {adapter.name!r} omits capability facets: {sorted(missing)}"
            )
        self._adapters[adapter.name] = adapter
        for alias in aliases:
            self._adapters[alias] = adapter
        label = executor_label if executor_label is not None else adapter.label()
        if executor_class is not None:
            self._executor_labels[executor_class] = label
        if default or self._default is None:
            self._default = adapter.name

    def resolve(self, name: str) -> HarnessAdapter:
        try:
            return self._adapters[name]
        except KeyError as exc:
            raise OrchestrationError(
                f"unknown harness adapter {name!r}; known: {sorted(self._adapters)}"
            ) from exc

    def names(self) -> tuple[str, ...]:
        """Registered adapter names plus aliases, sorted."""
        return tuple(sorted(self._adapters))

    def default_name(self) -> str:
        if self._default is None:
            raise OrchestrationError("no default harness adapter registered")
        return self._default

    def label_for(self, executor: object) -> str:
        """Receipt/event label for an executor instance, falling back to its class name."""
        cls = type(executor)
        if cls in self._executor_labels:
            return self._executor_labels[cls]
        return getattr(cls, "__name__", str(cls))

    def executor_for(
        self, target: ExecutionTarget, *, gateway: str, api_key: str, serve_url: str | None = None
    ) -> WorkerExecutor:
        """Build the worker executor for an ``ExecutionTarget``'s harness.

        Dispatch selects the adapter by ``target.harness`` alone; the adapter
        translates the target into a launch.  ``gateway``/``api_key`` are the
        dispatch credentials (Verdict key precedence applies inside the
        executor).  ``serve_url`` is the configured ``verdict serve`` address; only adapters
        whose ``worker_executor`` takes a ``serve_url`` keyword (workers that route through
        serve) receive it.
        """
        adapter = self.resolve(target.harness)
        if (
            serve_url is not None
            and "serve_url" in inspect.signature(adapter.worker_executor).parameters
        ):
            return adapter.worker_executor(gateway=gateway, api_key=api_key, serve_url=serve_url)  # type: ignore[call-arg]
        return adapter.worker_executor(gateway=gateway, api_key=api_key)


DEFAULT_REGISTRY = HarnessRegistry()


class PrimeHeadlessAdapter:
    name = "prime-headless"
    executor_label = "prime-headless"

    def capabilities(self) -> HarnessCapabilities:
        return HarnessCapabilities(
            harness="prime-headless",
            facets={
                HarnessCapability.WORKER_LAUNCH: CapabilityLevel.SUPPORTED,
                HarnessCapability.INTERACTIVE_ATTACH: CapabilityLevel.UNSUPPORTED,
                HarnessCapability.MODEL_SWITCHING: CapabilityLevel.PARTIAL,
                HarnessCapability.CONTEXT_INJECTION: CapabilityLevel.PARTIAL,
                HarnessCapability.RESUME_CHECKPOINT: CapabilityLevel.PARTIAL,
                HarnessCapability.MCP: CapabilityLevel.PARTIAL,
                HarnessCapability.HOOKS: CapabilityLevel.UNSUPPORTED,
                HarnessCapability.TOOL_INTERCEPTION: CapabilityLevel.UNSUPPORTED,
                HarnessCapability.EVENT_STREAM: CapabilityLevel.PARTIAL,
            },
            notes=(
                "OpenAI-compatible provider via ~/.prime/agent/models.json → Verdict :8000",
                "apiKey stored as env var name only; never writes secret values",
            ),
        )

    def worker_executor(self, *, gateway: str, api_key: str) -> WorkerExecutor:
        # gateway/api_key unused: PrimeHeadlessExecutor reads its own launch config
        # and applies Verdict key precedence internally.
        from verdict.orchestration.executors import PrimeHeadlessExecutor

        return PrimeHeadlessExecutor()

    def harness_visibility(self, *, catalog_rows: Any, sync_visibility: bool) -> HarnessVisibility:
        from verdict.orchestration.eligibility_report import prime_visibility

        return cast(
            HarnessVisibility, prime_visibility(live_rows=catalog_rows, sync=sync_visibility)
        )

    def label(self) -> str:
        return self.executor_label

    def not_implemented(self, feature: str) -> None:
        raise OrchestrationError(f"prime-headless does not implement {feature}")


class DirectGatewayAdapter:
    name = "direct-gateway"
    executor_label = "direct-gateway"

    def capabilities(self) -> HarnessCapabilities:
        return HarnessCapabilities(
            harness="direct-gateway",
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
            notes=("direct OmniRoute HTTP send; no agent harness",),
        )

    def worker_executor(self, *, gateway: str, api_key: str) -> WorkerExecutor:
        from verdict.orchestration.executors import DirectGatewayExecutor

        return DirectGatewayExecutor(base_url=gateway, api_key=api_key)

    def harness_visibility(self, *, catalog_rows: Any, sync_visibility: bool) -> HarnessVisibility:
        from verdict.orchestration.eligibility import HarnessVisibility
        from verdict.prime_inventory import concrete_rows

        live_concrete, _ = concrete_rows(catalog_rows)
        return HarnessVisibility(
            frozenset(str(r["id"]) for r in live_concrete), source="live-gateway"
        )

    def label(self) -> str:
        return self.executor_label

    def not_implemented(self, feature: str) -> None:
        raise OrchestrationError(f"direct-gateway does not implement {feature}")


class OpenCodeAdapter:
    """Harness adapter for OpenCode agentic coding workers (ADR-053).

    OpenCode runs a full edit-test-fix loop inside a repository before
    returning.  Each attempt gets an isolated per-attempt config
    (``OPENCODE_CONFIG_DIR`` → temp dir) and routes all model calls through
    ``verdict serve`` (:8000), never directly to OmniRoute.

    OpenCode is the DEFAULT worker executor of ``verdict orchestrate`` (ADR-053 Decision 2,
    amended 2026-11-25, #1328).  ``--executor direct-gateway`` selects the previous default and
    ``--executor prime-headless`` Prime.  A machine without what OpenCode needs ends each
    attempt in a ``harness_configuration:`` block (see :class:`OpenCodeWorkerExecutor`), never in
    a silent fallback to another executor.  The planner is a separate, answer-only role
    (``--planner-executor``) and never uses this adapter.
    """

    name = "opencode"
    executor_label = "opencode"

    def capabilities(self) -> HarnessCapabilities:
        return HarnessCapabilities(
            harness="opencode",
            facets={
                HarnessCapability.WORKER_LAUNCH: CapabilityLevel.SUPPORTED,
                HarnessCapability.INTERACTIVE_ATTACH: CapabilityLevel.UNSUPPORTED,
                HarnessCapability.MODEL_SWITCHING: CapabilityLevel.SUPPORTED,
                HarnessCapability.CONTEXT_INJECTION: CapabilityLevel.SUPPORTED,
                HarnessCapability.RESUME_CHECKPOINT: CapabilityLevel.UNSUPPORTED,
                HarnessCapability.MCP: CapabilityLevel.UNSUPPORTED,
                HarnessCapability.HOOKS: CapabilityLevel.UNSUPPORTED,
                HarnessCapability.TOOL_INTERCEPTION: CapabilityLevel.UNSUPPORTED,
                HarnessCapability.EVENT_STREAM: CapabilityLevel.PARTIAL,
            },
            notes=(
                "Full edit-test-fix loop via opencode run --standalone --auto --format json",
                "Per-attempt OPENCODE_CONFIG_DIR isolation; API key referenced by env var only",
                "MODEL_SWITCHING: per-attempt config pins the exact admitted route",
                "CONTEXT_INJECTION: instructions file written to worktree before each attempt",
                "EVENT_STREAM: NDJSON collected after completion, not streamed live",
            ),
        )

    def worker_executor(
        self, *, gateway: str, api_key: str, serve_url: str | None = None
    ) -> WorkerExecutor:
        # gateway/api_key unused: they are the OmniRoute address and key, and OpenCodeExecutor
        # routes through verdict serve instead.  It resolves its own serve credential
        # (Verdict's code, not an operator variable) and takes the serve address from
        # ``serve_url``, else VERDICT_SERVE_URL, else the serve unit's default.
        return OpenCodeWorkerExecutor(serve_base_url=serve_url)

    def harness_visibility(self, *, catalog_rows: Any, sync_visibility: bool) -> HarnessVisibility:
        # OpenCode routes all calls through Verdict serve, which forwards to
        # OmniRoute.  The same concrete-route visibility as direct-gateway
        # applies: all concrete (non-combo, non-auto) live routes are visible.
        from verdict.orchestration.eligibility import HarnessVisibility
        from verdict.prime_inventory import concrete_rows

        live_concrete, _ = concrete_rows(catalog_rows)
        return HarnessVisibility(
            frozenset(str(r["id"]) for r in live_concrete), source="live-gateway"
        )

    def label(self) -> str:
        return self.executor_label

    def not_implemented(self, feature: str) -> None:
        raise OrchestrationError(f"opencode does not implement {feature}")


# Wire up executor classes after import so the module has no import cycle.
from verdict.orchestration.executor_opencode import (  # noqa: E402
    OPENCODE_BIN_ENV,
    PINNED_VERSION,
    OpenCodeExecutor,
)
from verdict.orchestration.executors import (  # noqa: E402
    DirectGatewayExecutor,
    FaultInjectingExecutor,
    MixedExecutor,
    PrimeHeadlessExecutor,
    ScriptedExecutor,
)

#: The start of every message by which an executor says it cannot be launched on this machine;
#: ``verdict.orchestration.recovery`` maps it to a BLOCK (no retry, no cooldown).
_CONFIGURATION_BLOCK = "harness_configuration:"

#: Appended to the configuration block of an OpenCode worker: what the default needs, and the way
#: out of it.  The reason that comes before it is the executor's own, from the check that failed.
OPENCODE_DEFAULT_NOTE = (
    f"OpenCode is the default worker executor. It needs the opencode {PINNED_VERSION} binary "
    f"(at ~/.opencode/bin/opencode, on PATH, or named by {OPENCODE_BIN_ENV}) and a running "
    "'verdict serve' with its credential and the worker signing key, which "
    "'verdict setup --apply --allow host.serve' installs. To use the previous default instead, "
    "run with --executor direct-gateway"
)


class OpenCodeWorkerExecutor(OpenCodeExecutor):
    """The OpenCode executor the registry builds, for the run's default worker.

    It is :class:`OpenCodeExecutor` unchanged except that a ``harness_configuration:`` block (no
    binary, no serve, no credential, no signing key, a refused assignment) also carries
    :data:`OPENCODE_DEFAULT_NOTE`.  The executor cannot name the way out itself: it does not know
    it is the default, nor which flag selects the other executors.  The block stays one block:
    recovery maps it to BLOCK, so the node is not retried and nothing else is dispatched.
    """

    async def run(
        self,
        prompt: str,
        *,
        route_id: str,
        cwd: Path,
        timeout_seconds: float,
        run_id: str | None = None,
        node_id: str | None = None,
    ) -> WorkerTerminal:
        terminal = await super().run(
            prompt,
            route_id=route_id,
            cwd=cwd,
            timeout_seconds=timeout_seconds,
            run_id=run_id,
            node_id=node_id,
        )
        if terminal.error.startswith(_CONFIGURATION_BLOCK) and (
            OPENCODE_DEFAULT_NOTE not in terminal.error
        ):
            return replace(terminal, error=f"{terminal.error}; {OPENCODE_DEFAULT_NOTE}")
        return terminal


# OpenCode is the default worker backend (ADR-053 Decision 2, amended 2026-11-25, #1328): each
# coding node is a full edit-test-fix loop.  direct-gateway (one gateway call per node; it needs
# only the gateway) is the previous default, selected with ``--executor direct-gateway``.  Prime
# Agent is an optional harness selected with ``--executor prime-headless``.
DEFAULT_REGISTRY.register(
    PrimeHeadlessAdapter(),
    aliases=("prime",),
    executor_class=PrimeHeadlessExecutor,
    executor_label="prime-headless",
)
DEFAULT_REGISTRY.register(
    DirectGatewayAdapter(), executor_class=DirectGatewayExecutor, executor_label="direct-gateway"
)
DEFAULT_REGISTRY.register(
    OpenCodeAdapter(),
    default=True,
    executor_class=OpenCodeWorkerExecutor,
    executor_label="opencode",
)
# The executor class on its own (tests and other callers build it directly) keeps its label.
DEFAULT_REGISTRY._executor_labels[OpenCodeExecutor] = "opencode"
# Wrapper labels so run.py's receipt table can be fully delegated to the registry.
DEFAULT_REGISTRY._executor_labels[FaultInjectingExecutor] = "fault-injecting"
DEFAULT_REGISTRY._executor_labels[MixedExecutor] = "mixed"
DEFAULT_REGISTRY._executor_labels[ScriptedExecutor] = "scripted"
