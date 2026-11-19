"""Generic harness contracts for Verdict orchestration.

Adapter/registry layer that separates *harness identity* (which coding-agent
harness runs a bounded work node) from transport/provider/model/capacity
identity.  Harness adapters translate Verdict decisions into launches; they
never rank models, broaden admission, or own cross-harness orchestration.

This module is intentionally small and versioned.  Every symbol it defines has
a production caller from day one so the reachability baseline does not grow.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol, cast

from verdict.orchestration.contracts import CapacityClass, OrchestrationError, WorkerExecutor
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
        self, target: ExecutionTarget, *, gateway: str, api_key: str
    ) -> WorkerExecutor:
        """Build the worker executor for an ``ExecutionTarget``'s harness.

        Dispatch selects the adapter by ``target.harness`` alone; the adapter
        translates the target into a launch.  ``gateway``/``api_key`` are the
        dispatch credentials (Verdict key precedence applies inside the
        executor).
        """
        return self.resolve(target.harness).worker_executor(gateway=gateway, api_key=api_key)


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


# Wire up executor classes after import so the module has no import cycle.
from verdict.orchestration.executors import (  # noqa: E402
    DirectGatewayExecutor,
    FaultInjectingExecutor,
    MixedExecutor,
    PrimeHeadlessExecutor,
    ScriptedExecutor,
)

# direct-gateway is the default worker backend (1.0): it needs only the gateway.
# Prime Agent is an optional harness selected with ``--executor prime-headless``.
DEFAULT_REGISTRY.register(
    PrimeHeadlessAdapter(),
    aliases=("prime",),
    executor_class=PrimeHeadlessExecutor,
    executor_label="prime-headless",
)
DEFAULT_REGISTRY.register(
    DirectGatewayAdapter(),
    default=True,
    executor_class=DirectGatewayExecutor,
    executor_label="direct-gateway",
)
# Wrapper labels so run.py's receipt table can be fully delegated to the registry.
DEFAULT_REGISTRY._executor_labels[FaultInjectingExecutor] = "fault-injecting"
DEFAULT_REGISTRY._executor_labels[MixedExecutor] = "mixed"
DEFAULT_REGISTRY._executor_labels[ScriptedExecutor] = "scripted"
