"""Shared contracts for Verdict's goal -> DAG -> workers -> review -> receipt run.

Pure data + validation. No I/O, no provider calls. Every other module in
``verdict.orchestration`` builds on these types, so they are deliberately small
and versioned.

Model identity: Verdict always speaks in bare OmniRoute route ids such as
``cc/claude-sonnet-5``. Harness-specific selectors (Prime's ``omniroute/`` provider
prefix, for example) are added only inside the harness executor adapter.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import PurePosixPath
from typing import Any

SCHEMA_VERSION = "verdict.orchestration/v1"


class OrchestrationError(ValueError):
    """Invalid plan, graph, or state transition."""


class NodeKind(str, Enum):
    IMPLEMENT = "implement"
    RESEARCH = "research"
    INTEGRATE = "integrate"
    REVIEW = "review"


class NodeState(str, Enum):
    """Explicit worker lifecycle. Admission is never success."""

    PLANNED = "PLANNED"
    ADMITTED = "ADMITTED"
    DISPATCHED = "DISPATCHED"
    RUNNING = "RUNNING"
    TERMINAL_SUCCESS = "TERMINAL_SUCCESS"
    TERMINAL_FAILURE = "TERMINAL_FAILURE"
    VALIDATED = "VALIDATED"
    REJECTED = "REJECTED"
    BLOCKED = "BLOCKED"


# Legal transitions. TERMINAL_FAILURE/REJECTED may return to PLANNED for a
# same-node reassignment (the contract is preserved; only the model changes).
TRANSITIONS: Mapping[NodeState, frozenset[NodeState]] = {
    NodeState.PLANNED: frozenset({NodeState.ADMITTED, NodeState.BLOCKED}),
    NodeState.ADMITTED: frozenset({NodeState.DISPATCHED, NodeState.TERMINAL_FAILURE}),
    NodeState.DISPATCHED: frozenset({NodeState.RUNNING, NodeState.TERMINAL_FAILURE}),
    NodeState.RUNNING: frozenset({NodeState.TERMINAL_SUCCESS, NodeState.TERMINAL_FAILURE}),
    NodeState.TERMINAL_SUCCESS: frozenset({NodeState.VALIDATED, NodeState.REJECTED}),
    NodeState.TERMINAL_FAILURE: frozenset({NodeState.PLANNED, NodeState.BLOCKED}),
    NodeState.REJECTED: frozenset({NodeState.PLANNED, NodeState.BLOCKED}),
    NodeState.VALIDATED: frozenset(),
    NodeState.BLOCKED: frozenset(),
}


def check_transition(current: NodeState, target: NodeState) -> None:
    if target not in TRANSITIONS[current]:
        raise OrchestrationError(f"illegal node transition {current.value} -> {target.value}")


class Topology(str, Enum):
    """BOD-151 cognition topology; chosen by deterministic rules, never by a model."""

    SOLO = "SOLO"
    WORKER_CRITIC = "WORKER_CRITIC"
    PARALLEL_WORK_UNITS = "PARALLEL_WORK_UNITS"


class RunOutcome(str, Enum):
    COMPLETE = "COMPLETE"
    BLOCKED = "BLOCKED"


def _norm_path(value: str) -> str:
    text = str(value).strip().replace("\\", "/")
    path = PurePosixPath(text)
    if not text or path.is_absolute() or ".." in path.parts:
        raise OrchestrationError(f"owned path must be repo-relative without '..': {value!r}")
    return str(path)


@dataclass(frozen=True)
class WorkNode:
    """One bounded, independently verifiable unit of work in the DAG."""

    node_id: str
    objective: str
    kind: NodeKind = NodeKind.IMPLEMENT
    story: str = ""
    depends_on: tuple[str, ...] = ()
    owned_files: tuple[str, ...] = ()
    required_context: tuple[str, ...] = ()  # repo-relative files to hydrate (read-only)
    acceptance: tuple[str, ...] = ()
    verification_command: tuple[str, ...] = ()
    barrier: str = ""  # integration barrier name this node must pass before dependents run
    risk: str = "low"  # low | medium | high
    required_capabilities: tuple[str, ...] = ("tools",)
    coding: bool = True
    reasoning: bool = False
    min_context_tokens: int = 32_000

    def __post_init__(self) -> None:
        if not self.node_id.strip() or not self.objective.strip():
            raise OrchestrationError("node_id and objective are required")
        if self.risk not in {"low", "medium", "high"}:
            raise OrchestrationError(f"{self.node_id}: risk must be low|medium|high")
        object.__setattr__(self, "kind", NodeKind(self.kind))
        object.__setattr__(self, "depends_on", tuple(self.depends_on))
        object.__setattr__(self, "owned_files", tuple(_norm_path(p) for p in self.owned_files))
        object.__setattr__(
            self, "required_context", tuple(_norm_path(p) for p in self.required_context)
        )
        object.__setattr__(self, "acceptance", tuple(self.acceptance))
        object.__setattr__(self, "verification_command", tuple(self.verification_command))
        object.__setattr__(self, "required_capabilities", tuple(self.required_capabilities))
        if self.kind is NodeKind.IMPLEMENT:
            if not self.owned_files:
                raise OrchestrationError(f"{self.node_id}: implement node needs owned_files")
            if not self.verification_command:
                raise OrchestrationError(
                    f"{self.node_id}: implement node needs a verification_command"
                )
        if self.node_id in self.depends_on:
            raise OrchestrationError(f"{self.node_id}: node depends on itself")

    def owns(self, path: str) -> bool:
        try:
            candidate = _norm_path(path)
        except OrchestrationError:
            return False
        return any(
            candidate == owned or candidate.startswith(owned.rstrip("/") + "/")
            for owned in self.owned_files
        )

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["kind"] = self.kind.value
        return value

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> WorkNode:
        known = {f for f in cls.__dataclass_fields__}
        unknown = sorted(set(value) - known)
        if unknown:
            raise OrchestrationError(f"work node has unknown field(s): {unknown}")
        data = dict(value)
        for key in (
            "depends_on",
            "owned_files",
            "required_context",
            "acceptance",
            "verification_command",
            "required_capabilities",
        ):
            if key in data:
                raw = data[key]
                if isinstance(raw, str) or not isinstance(raw, Sequence):
                    raise OrchestrationError(f"{key} must be a list")
                data[key] = tuple(str(item) for item in raw)
        return cls(**data)


@dataclass(frozen=True)
class WorkGraph:
    """Validated DAG of work nodes plus the topology chosen for it."""

    goal: str
    nodes: tuple[WorkNode, ...]
    topology: Topology = Topology.PARALLEL_WORK_UNITS
    rationale: tuple[str, ...] = ()
    max_parallel: int = 3

    def __post_init__(self) -> None:
        object.__setattr__(self, "topology", Topology(self.topology))
        ids = [node.node_id for node in self.nodes]
        if not ids:
            raise OrchestrationError("graph has no nodes")
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        if dupes:
            raise OrchestrationError(f"duplicate node ids: {dupes}")
        known = set(ids)
        for node in self.nodes:
            missing = sorted(set(node.depends_on) - known)
            if missing:
                raise OrchestrationError(f"{node.node_id}: unknown dependencies {missing}")
        self.layers()  # raises on cycles
        self._check_ownership()
        if self.max_parallel < 1:
            raise OrchestrationError("max_parallel must be >= 1")

    def node(self, node_id: str) -> WorkNode:
        for node in self.nodes:
            if node.node_id == node_id:
                return node
        raise KeyError(node_id)

    def layers(self) -> tuple[tuple[str, ...], ...]:
        """Topological layers; nodes inside one layer may run concurrently."""
        remaining = {n.node_id: set(n.depends_on) for n in self.nodes}
        done: set[str] = set()
        result: list[tuple[str, ...]] = []
        while remaining:
            ready = sorted(i for i, deps in remaining.items() if deps <= done)
            if not ready:
                raise OrchestrationError(f"dependency cycle among {sorted(remaining)}")
            result.append(tuple(ready))
            done.update(ready)
            for i in ready:
                del remaining[i]
        return tuple(result)

    def ancestors(self, node_id: str) -> set[str]:
        seen: set[str] = set()
        stack = list(self.node(node_id).depends_on)
        while stack:
            current = stack.pop()
            if current not in seen:
                seen.add(current)
                stack.extend(self.node(current).depends_on)
        return seen

    def _check_ownership(self) -> None:
        """Two writers may share a file only if one is an ancestor of the other."""
        writers = [n for n in self.nodes if n.kind is NodeKind.IMPLEMENT]
        for i, left in enumerate(writers):
            for right in writers[i + 1 :]:
                ordered = left.node_id in self.ancestors(
                    right.node_id
                ) or right.node_id in self.ancestors(left.node_id)
                if ordered:
                    continue
                overlap = sorted(set(left.owned_files) & set(right.owned_files))
                if overlap:
                    raise OrchestrationError(
                        f"concurrent nodes {left.node_id} and {right.node_id} both own {overlap}"
                    )

    def digest(self) -> str:
        return canonical_digest(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "goal": self.goal,
            "topology": self.topology.value,
            "rationale": list(self.rationale),
            "max_parallel": self.max_parallel,
            "nodes": [n.to_dict() for n in self.nodes],
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> WorkGraph:
        nodes = value.get("nodes")
        if not isinstance(nodes, list):
            raise OrchestrationError("graph.nodes must be a list")
        return cls(
            goal=str(value.get("goal", "")),
            nodes=tuple(WorkNode.from_dict(n) for n in nodes),
            topology=Topology(value.get("topology", Topology.PARALLEL_WORK_UNITS.value)),
            rationale=tuple(value.get("rationale", ())),
            max_parallel=int(value.get("max_parallel", 3)),
        )


# ---------------------------------------------------------------- eligibility


class EligibilityStage(str, Enum):
    """Ordered capacity ladder. A route must pass every stage to be SELECTED."""

    DISCOVERED = "DISCOVERED"  # present in live OmniRoute inventory
    ENTITLED = "ENTITLED"  # an active provider account/connection backs it
    HEALTHY = "HEALTHY"  # fresh 1-token inference probe succeeded
    AVAILABLE = "AVAILABLE"  # no active cooldown / quota-reset window
    TASK_ELIGIBLE = "TASK_ELIGIBLE"  # capabilities, context, frontier policy fit
    SELECTED = "SELECTED"


STAGE_ORDER: tuple[EligibilityStage, ...] = tuple(EligibilityStage)


class CapacityClass(str, Enum):
    """Economic class, derived from account evidence (never from a model name)."""

    SUBSCRIPTION = "subscription"
    FREE = "free"
    METERED = "metered"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class RouteVerdict:
    """Why one route did or did not reach a given ladder stage."""

    route_id: str
    provider: str
    reached: EligibilityStage | None  # highest stage passed; None = not even discovered
    failed_stage: EligibilityStage | None
    reason: str
    capacity_class: CapacityClass = CapacityClass.UNKNOWN
    plan_label: str = ""  # sanitized account plan label, e.g. "claude_max"
    cooldown_until: str | None = None  # ISO-8601 UTC
    rank: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "route_id": self.route_id,
            "provider": self.provider,
            "reached": self.reached.value if self.reached else None,
            "failed_stage": self.failed_stage.value if self.failed_stage else None,
            "reason": self.reason,
            "capacity_class": self.capacity_class.value,
            "plan_label": self.plan_label,
            "cooldown_until": self.cooldown_until,
            "rank": self.rank,
        }


@dataclass(frozen=True)
class TaskRequirements:
    """What a node needs from a model (input to TASK_ELIGIBLE)."""

    required_capabilities: frozenset[str] = frozenset({"tools"})
    min_context_tokens: int = 32_000
    coding: bool = True
    reasoning: bool = False
    frontier_worthy: bool = False
    exclude_routes: frozenset[str] = frozenset()  # e.g. implementer ids for an independent reviewer
    exclude_families: frozenset[str] = frozenset()  # e.g. {"claude"} to force a different family

    @classmethod
    def for_node(cls, node: WorkNode, **overrides: Any) -> TaskRequirements:
        base = cls(
            required_capabilities=frozenset(node.required_capabilities),
            min_context_tokens=node.min_context_tokens,
            coding=node.coding,
            reasoning=node.reasoning,
            frontier_worthy=node.risk == "high" or node.kind in {NodeKind.REVIEW},
        )
        return cls(**{**asdict(base), **overrides})


# ---------------------------------------------------------------- execution


@dataclass(frozen=True)
class WorkerTerminal:
    """Terminal result of one worker attempt, as observed by the executor adapter."""

    ok: bool
    output: str = ""
    model: str = ""  # route id actually reported by the harness
    stop_reason: str = ""
    error: str = ""  # raw sanitized error text when ok=False
    status_code: int | None = None
    retry_after_seconds: float | None = None
    duration_seconds: float = 0.0
    session_ref: str = ""  # harness session/journal pointer for provenance


@dataclass(frozen=True)
class FailureClassification:
    """BOD-152 normalized failure and bounded corrective action."""

    category: str  # e.g. rate_limited, quota_exhausted, authentication, payment_required,
    # permission, unsupported, timeout, upstream_temporary,
    # transport_temporary, malformed_response, empty_output,
    # no_final_answer, model_mismatch, verification_failed,
    # ownership_violation, unknown
    action: str  # REROUTE | RETRY_INFRA | CORRECT_IMPLEMENTATION | REHYDRATE | BLOCK
    cooldown_seconds: float
    scope: str  # "route" | "provider" | "none"
    evidence: str = ""


# ---------------------------------------------------------------- events

EVENT_TYPES = frozenset(
    {
        "run_started",
        "plan_started",
        "plan_ready",
        "topology",
        "eligibility",
        "node_state",
        "selection",
        "dispatch",
        "heartbeat",
        "terminal",
        "failure",
        "cooldown",
        "reassign",
        "verify",
        "barrier",
        "integrate",
        "review",
        "remediation",
        "controller",
        "run_finished",
    }
)


@dataclass(frozen=True)
class RunEvent:
    """One append-only event. The TUI and receipts are both projections of these."""

    seq: int
    at: str  # ISO-8601 UTC
    type: str
    node_id: str = ""
    data: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.type not in EVENT_TYPES:
            raise OrchestrationError(f"unknown event type {self.type!r}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "at": self.at,
            "type": self.type,
            "node_id": self.node_id,
            "data": dict(self.data),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> RunEvent:
        return cls(
            seq=int(value["seq"]),
            at=str(value["at"]),
            type=str(value["type"]),
            node_id=str(value.get("node_id", "")),
            data=dict(value.get("data") or {}),
        )


def canonical_digest(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def route_provider(route_id: str) -> str:
    return route_id.split("/", 1)[0].strip().lower()


def route_family(route_id: str) -> str:
    """Coarse model family used for reviewer independence (claude, gpt, gemini, ...)."""
    tail = route_id.lower().split("/")[-1]
    for family in (
        "claude",
        "gpt",
        "gemini",
        "grok",
        "qwen",
        "deepseek",
        "glm",
        "kimi",
        "llama",
        "mistral",
        "nemotron",
        "minimax",
    ):
        if family in tail:
            return family
    return tail.split("-")[0]


def dedupe(items: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(items))


# ---------------------------------------------------------------- component protocols
# Parallel implementation units code against these; the runtime composes them.

from datetime import datetime  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import Protocol  # noqa: E402


class WorkerExecutor(Protocol):
    """Runs ONE prompt on ONE exact route in ONE working directory, to a terminal result.

    Must never raise for provider/transport failures: those become
    ``WorkerTerminal(ok=False, ...)``. Must enforce ``timeout_seconds`` itself.
    ``route_id`` is a bare OmniRoute id (``cc/claude-sonnet-5``).
    """

    async def run(
        self, prompt: str, *, route_id: str, cwd: Path, timeout_seconds: float
    ) -> WorkerTerminal: ...


class ModelSelector(Protocol):
    """DISCOVERED -> ENTITLED -> HEALTHY -> AVAILABLE -> TASK_ELIGIBLE -> SELECTED."""

    def evaluate(
        self, requirements: TaskRequirements, *, now: datetime
    ) -> tuple[RouteVerdict, ...]:
        """Every discovered route with its ladder verdict; SELECTED-capable ones ranked."""
        ...

    def select(
        self, requirements: TaskRequirements, *, now: datetime
    ) -> tuple[RouteVerdict | None, tuple[RouteVerdict, ...]]:
        """Best route (may probe lazily) or None, plus the verdicts considered."""
        ...

    def record_failure(
        self, route_id: str, failure: FailureClassification, *, now: datetime
    ) -> None: ...

    def record_success(self, route_id: str, *, now: datetime) -> None: ...


class FailureClassifier(Protocol):
    def classify(self, terminal: WorkerTerminal, *, now: datetime) -> FailureClassification: ...


class Reviewer(Protocol):
    """Independent semantic review of an integrated diff (BOD-185)."""

    async def review(
        self,
        *,
        repo: Path,
        base_ref: str,
        head_ref: str,
        background: str,
        exclude_routes: frozenset[str],
        exclude_families: frozenset[str],
    ) -> ReviewResult: ...


@dataclass(frozen=True)
class ReviewFinding:
    severity: str  # critical | high | medium | low | info
    category: str
    file: str
    line: int | None
    message: str

    def blocking(self) -> bool:
        return self.severity in {"critical", "high"}


@dataclass(frozen=True)
class ReviewResult:
    """Fail-closed review result: ``passed`` only when the review really ran and is clean."""

    status: str  # PASS | FAIL | ERROR
    reviewer: str  # e.g. "open-code-review v1.12.9"
    route_id: str  # model that performed the review
    findings: tuple[ReviewFinding, ...] = ()
    raw_ref: str = ""  # path to raw reviewer output
    detail: str = ""

    @property
    def passed(self) -> bool:
        return self.status == "PASS" and not any(f.blocking() for f in self.findings)
