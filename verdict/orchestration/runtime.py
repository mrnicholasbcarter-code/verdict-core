"""Bounded DAG runtime: concurrent dispatch, same-node reassignment, barriers.

The runtime owns orchestration only. It never chooses a model itself (the
``ModelSelector`` does), never classifies failures itself (the
``FailureClassifier`` does) and never executes a model itself (the
``WorkerExecutor`` does). Every decision is emitted as a ``RunEvent`` so the TUI
and the receipt are projections of the same append-only log.

Isolation guarantees:
- each node attempt runs in its own git worktree created from the node's base;
- a failed attempt's worktree is discarded; the SAME node contract is re-dispatched
  on a freshly selected route (same-node reassignment);
- one node's failure never cancels healthy siblings; dependents of a failed node
  become BLOCKED, independent nodes keep running;
- integration happens only after all dependencies are VALIDATED (barrier).
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import inspect
import json
import os
import secrets
import shlex
import shutil
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol, cast

from verdict.context_pack import ContextPlan, estimate_tokens
from verdict.models import AdmissionProof
from verdict.orchestration.candidate_builder import (
    MAX_CANDIDATES as _MAX_CANDIDATES,  # noqa: F401 re-exported; tests import from runtime
)
from verdict.orchestration.candidate_builder import build_candidates as _build_candidates
from verdict.orchestration.candidate_builder import build_rejections as _build_rejections
from verdict.orchestration.contracts import (
    TRANSITIONS,
    CapacityClass,
    EligibilityStage,
    FailureClassification,
    FailureClassifier,
    ModelSelector,
    NodeKind,
    NodeState,
    OrchestrationError,
    Reviewer,
    ReviewResult,
    RouteVerdict,
    RunOutcome,
    TaskRequirements,
    WorkerExecutor,
    WorkerTerminal,
    WorkGraph,
    WorkNode,
    check_transition,
    dispatch_blocker,
    require_launchable,
    route_family,
    route_provider,
)
from verdict.orchestration.controller_operations import (
    OwnershipError,
    bounded_executor_call,
    operation_scope,
    scrub_fencing_env,
)
from verdict.orchestration.controls import ControlReader, ControlRequest
from verdict.orchestration.credential_pools import POOL_COOLDOWN_CATEGORIES, pool_of
from verdict.orchestration.health_cache import (
    PERMIT_PROOF_MAX_VALIDITY_SECONDS,
    DispatchPermit,
    DispatchScope,
    HealthCache,
    default_cache_path,
)
from verdict.orchestration.recovery import RecoveryBudget
from verdict.orchestration.repo_gates import describe_gates, discover_repo_gates
from verdict.orchestration.salvage import (
    _MAX_EVIDENCE_PATHS,
    restore_salvaged_files,
    salvage_owned_files,
)
from verdict.orchestration.verification import resolve_gate_argv as _resolve_gate_argv
from verdict.orchestration.verification import resolve_verify_argv as _resolve_verify_argv
from verdict.orchestration.worker_context import WorkerPrompt
from verdict.relay import admission_proof, dispatch_scope
from verdict.subagent_selection import CONTEXT_LENGTH_CATEGORY

# Indirection for testing
_sleep = asyncio.sleep
# BOD-433: failures that revoke the permit scope (credential denial, wrong model served).
_REVOKING_CATEGORIES = frozenset({"authentication", "permission", "model_mismatch"})
_WORKER_OUTPUT_RESERVE = 1024
_WORKER_SAFETY_RESERVE = 512


class EventSink(Protocol):
    def emit(self, type: str, node_id: str = "", **data: Any) -> Any: ...


Runner = Callable[[Sequence[str], Path, float], Awaitable[tuple[int, str]]]


async def subprocess_runner(argv: Sequence[str], cwd: Path, timeout: float) -> tuple[int, str]:
    """Run argv without a shell; kill the process group on timeout."""
    try:
        child_env = dict(os.environ)
        scrub_fencing_env(child_env)
        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=child_env,
            start_new_session=True,
        )
    except (FileNotFoundError, PermissionError) as exc:
        return 127, f"verification command not found: {argv[0]}: {exc}"
    try:
        out, _ = await asyncio.wait_for(process.communicate(), timeout)
    except (asyncio.TimeoutError, asyncio.CancelledError) as exc:
        import contextlib
        import signal

        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        await process.wait()
        if isinstance(exc, asyncio.CancelledError):
            raise
        return 124, f"timeout after {timeout}s"
    return process.returncode or 0, out.decode("utf-8", "replace")[-4000:]


@dataclass(frozen=True)
class RuntimePolicy:
    max_parallel: int = 3
    max_attempts_per_node: int = 4
    attempt_timeout_seconds: float = 900.0
    verify_timeout_seconds: float = 600.0
    run_deadline_seconds: float = 3600.0
    require_review: bool = True
    max_review_rounds: int = 2
    review_timeout_seconds: float = 900.0
    max_cooldown_wait_seconds: float = 120.0
    # BOD-272: REQUIRED_CONTEXT byte budget for a node's worker prompt. After a
    # context-length overflow the next attempt gets a smaller pack (halved, not
    # below the floor) instead of a blind replay of the oversized request.
    context_budget_bytes: int = 60_000
    min_context_budget_bytes: int = 4_000
    # Repo quality gates belong to the integration runtime, not the planner.
    run_repo_gates: bool = True
    # BOD-433 authority mode: every worker send needs a minted dispatch permit.
    # Without a DispatchAuthority to prove routes, every send is denied.
    require_dispatch_permit: bool = False


@dataclass(frozen=True)
class DispatchAuthority:
    """Where DagRuntime gets route proofs for BOD-433 dispatch permits.

    ``gateway_origin`` is the gateway the run's workers are sent through.
    ``confirm(route_ids, now)`` returns a read-only passport confirmation in the
    shape ``relay.admission_proof`` consumes.
    """

    gateway_origin: str
    confirm: Callable[[Sequence[str], datetime], dict[str, Any]]


def node_profile_digest(node: WorkNode) -> str:
    """SHA-256 of the fields that define a node's work; the permit scope's profile digest.

    The prompt is excluded: it is hydrated after binding and changes with retry feedback.
    """
    body = {
        "objective": node.objective,
        "kind": node.kind.value,
        "acceptance": list(node.acceptance),
        "verification_command": list(node.verification_command),
        "owned_files": list(node.owned_files),
        "required_capabilities": sorted(node.required_capabilities),
        "min_context_tokens": node.min_context_tokens,
        "coding": node.coding,
        "reasoning": node.reasoning,
        "risk": node.risk,
    }
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _revocation_epoch(scope: DispatchScope) -> int:
    return HealthCache(default_cache_path()).revocation_epoch(scope)


def permit_attempt_id(run_dir: Path, node_id: str, attempt: int) -> str:
    """Permit attempt id for one node attempt of one run directory.

    Permit attempt ids live in one global table. Run ids repeat (``--run-id``,
    resume, several runs roots), so the id carries a digest of the resolved run
    directory: stable when the same run resumes, distinct for any other run.
    """
    digest = hashlib.sha256(str(run_dir.resolve()).encode("utf-8")).hexdigest()[:16]
    return f"{run_dir.name}:{digest}:{node_id}-a{attempt}"


@dataclass
class NodeRun:
    node: WorkNode
    state: NodeState = NodeState.PLANNED
    attempt: int = 0
    route_id: str = ""
    history: list[dict[str, Any]] = field(default_factory=list)
    failures: list[FailureClassification] = field(default_factory=list)
    excluded_routes: set[str] = field(default_factory=set)
    commit: str = ""
    reason: str = ""
    # BOD-272: REQUIRED_CONTEXT byte budget for this node's next attempt
    # (0 = RuntimePolicy.context_budget_bytes). Shrinks after a context overflow.
    context_budget: int = 0
    # Routes already retried once with a smaller pack after an overflow.
    repacked_routes: set[str] = field(default_factory=set)
    # Bounded verification/ownership evidence from the last failed attempt,
    # appended to every retry, including a retry on another route.
    failure_feedback: str = ""
    rehydrated_routes: set[str] = field(default_factory=set)
    excluded_pools: set[str] = field(default_factory=set)
    selected_context_window: int | None = None
    # BOD-433: proof for the bound route; committed to a permit just before the send.
    dispatch_proof: AdmissionProof | None = None
    # BOD-433: the permit the current attempt was sent under (None = no send yet).
    permit: DispatchPermit | None = None


@dataclass(frozen=True)
class RunResult:
    outcome: RunOutcome
    reason: str
    nodes: Mapping[str, NodeRun]
    integration_ref: str
    review: ReviewResult | None


# Tool byproducts are never part of a node's deliverable (and never ownership violations).
BYPRODUCT_EXCLUDES = (
    "__pycache__/",
    "*.pyc",
    ".pytest_cache/",
    ".mypy_cache/",
    ".ruff_cache/",
    ".coverage",
    "node_modules/",
    ".verdict/",
)


def _ensure_local_excludes(cwd: Path) -> None:
    """Append byproduct patterns to the worktree's info/exclude (untracked, local only)."""
    git_path = cwd / ".git"
    try:
        if git_path.is_file():
            gitdir = Path(git_path.read_text().split(":", 1)[1].strip())
            common = gitdir / "commondir"
            if common.exists():
                gitdir = (gitdir / common.read_text().strip()).resolve()
        else:
            gitdir = git_path
        exclude = gitdir / "info" / "exclude"
        exclude.parent.mkdir(parents=True, exist_ok=True)
        current = exclude.read_text() if exclude.exists() else ""
        missing = [p for p in BYPRODUCT_EXCLUDES if p not in current.splitlines()]
        if missing:
            with exclude.open("a", encoding="utf-8") as stream:
                stream.write("\n# verdict orchestration byproducts\n" + "\n".join(missing) + "\n")
    except OSError:
        pass


class Git:
    """Minimal git worktree operations for node isolation and integration."""

    def __init__(self, repo: Path, runner: Runner = subprocess_runner) -> None:
        self.repo = repo
        self.run = runner
        self._worktree_lock = asyncio.Lock()

    async def call(self, *args: str, cwd: Path | None = None, timeout: float = 120) -> str:
        code, out = await self.run(("git", *args), cwd or self.repo, timeout)
        if code != 0:
            raise OrchestrationError(f"git {' '.join(args)} failed ({code}): {out[-600:]}")
        return out.strip()

    async def head(self, cwd: Path | None = None) -> str:
        return await self.call("rev-parse", "HEAD", cwd=cwd)

    async def _remove_worktree_unlocked(self, path: Path) -> None:
        """Internal unlocked version to avoid deadlock when called from add_worktree."""
        code, _ = await self.run(("git", "worktree", "remove", "--force", str(path)), self.repo, 60)
        if code != 0 and path.exists():
            shutil.rmtree(path, ignore_errors=True)
            await self.run(("git", "worktree", "prune"), self.repo, 60)

    async def add_worktree(self, path: Path, branch: str, base: str) -> None:
        async with self._worktree_lock:
            if path.exists():
                await self._remove_worktree_unlocked(path)
            await self.call("worktree", "add", "-f", "-B", branch, str(path), base)

    async def remove_worktree(self, path: Path) -> None:
        async with self._worktree_lock:
            await self._remove_worktree_unlocked(path)

    async def changed_files(self, cwd: Path, base: str) -> list[str]:
        _ensure_local_excludes(cwd)
        await self.call("add", "-A", cwd=cwd)
        out = await self.call("diff", "--cached", "--name-only", base, cwd=cwd)
        return [line for line in out.splitlines() if line.strip()]

    async def commit_all(self, cwd: Path, message: str) -> str:
        await self.call("add", "-A", cwd=cwd)
        code, _ = await self.run(("git", "diff", "--cached", "--quiet"), cwd, 60)
        if code != 0:
            await self.call(
                "-c",
                "user.name=verdict-worker",
                "-c",
                "user.email=verdict-worker@localhost",
                "commit",
                "-q",
                "--no-verify",
                "-m",
                message,
                cwd=cwd,
            )
        return await self.head(cwd)


def _blocked_report(output: str) -> str:
    """The worker's ``RESULT: BLOCKED <reason>`` line, first or final; else ""."""
    lines = [line.strip() for line in output.strip().splitlines() if line.strip()]
    for line in (lines[:1] + lines[-1:]) if lines else []:
        if line.upper().startswith("RESULT: BLOCKED"):
            return line
    return ""


class DagRuntime:
    """Execute a validated ``WorkGraph`` to an explicit COMPLETE or BLOCKED result."""

    def __init__(
        self,
        *,
        repo: Path,
        run_dir: Path,
        graph: WorkGraph,
        selector: ModelSelector,
        executor: WorkerExecutor,
        classifier: FailureClassifier,
        events: EventSink,
        prompt_for: Callable[..., str | WorkerPrompt],
        reviewer: Reviewer | None = None,
        policy: RuntimePolicy = RuntimePolicy(),
        runner: Runner = subprocess_runner,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        base_ref: str = "HEAD",
        inflight: dict[str, str] | None = None,
        dispatch_authority: DispatchAuthority | None = None,
    ) -> None:
        self.repo, self.run_dir, self.graph = repo, run_dir, graph
        self.dispatch_authority = dispatch_authority
        self.selector, self.executor, self.classifier = selector, executor, classifier
        self.events, self.prompt_for, self.reviewer = events, prompt_for, reviewer
        self.policy, self.runner, self.now = policy, runner, now
        self.base_ref = base_ref
        self.git = Git(repo, runner)
        self.nodes = {n.node_id: NodeRun(n) for n in graph.nodes}
        # node_id -> route_id. Shared with the selector's load callback so concurrent
        # selections spread across routes; reserved at SELECTION time, not dispatch.
        self.inflight: dict[str, str] = inflight if inflight is not None else {}
        self._run_started_monotonic: float | None = None
        self._slots = asyncio.Semaphore(min(policy.max_parallel, graph.max_parallel))
        self._base_sha = ""
        self._integration = run_dir / "worktrees" / "_integration"
        self._capacity: dict[str, str] = {}
        # Controls -------------------------------------------------------
        self._controls = ControlReader(run_dir)
        self._cancel_requested = False
        self._processed_controls: set[str] = set()
        self._recovery_budget = RecoveryBudget(max_attempts_per_node=policy.max_attempts_per_node)
        # The 900s attempt timeout ceiling (see _execute's min(..., 900.0)) is a
        # hard operational limit. A configured value above it would otherwise be
        # silently unusable; make the clamp an explicit, one-time event per run,
        # emitted here (before any dispatch) rather than hidden inside _execute.
        if policy.attempt_timeout_seconds > 900.0:
            self.events.emit(
                "controller",
                state="ATTEMPT_TIMEOUT_CLAMPED",
                configured=policy.attempt_timeout_seconds,
                applied=900.0,
            )

    # ------------------------------------------------------------------ state
    def _set(self, run: NodeRun, state: NodeState, **data: Any) -> None:
        check_transition(run.state, state)
        run.state = state
        self.events.emit(
            "node_state",
            run.node.node_id,
            state=state.value,
            route_id=run.route_id,
            attempt=run.attempt,
            **data,
        )

    def _select_with_stats(
        self, requirements: TaskRequirements, now: datetime
    ) -> tuple[RouteVerdict | None, tuple[RouteVerdict, ...], dict[str, int]]:
        """Select and read post-probe stats (BOD-203) on the same worker thread."""
        choice, considered = self.selector.select(requirements, now=now)
        return choice, considered, getattr(self.selector, "last_select_stats", {})

    def route_load(self, route_id: str) -> int:
        return sum(1 for r in self.inflight.values() if r == route_id)

    # ------------------------------------------------------------------ controls
    def _poll_controls(self, pending: dict[str, asyncio.Task[None]]) -> RunOutcome | None:
        """Poll the control channel and handle pending requests.

        Returns ``RunOutcome.CANCELLED`` when the caller should exit the main
        loop, or ``None`` to continue normally.
        """
        for req in self._controls.pending():
            if req.id in self._processed_controls:
                continue
            self._processed_controls.add(req.id)
            if req.kind == "cancel_run":
                self._handle_cancel_run(req, pending)
                return RunOutcome.CANCELLED
            elif req.kind == "cancel_node":
                self._handle_cancel_node(req, pending)
            elif req.kind == "retry_node":
                self._handle_retry_node(req, pending)
            else:
                self.events.emit(
                    "control",
                    id=req.id,
                    kind=req.kind,
                    accepted=False,
                    reason=f"unknown control kind: {req.kind!r}",
                )
        return None

    def _handle_cancel_run(
        self, req: ControlRequest, pending: dict[str, asyncio.Task[None]]
    ) -> None:
        """Cancel every running task and mark PLANNED/non-terminal nodes as BLOCKED."""
        self._cancel_requested = True
        for task in pending.values():
            task.cancel()
        for run in self.nodes.values():
            if run.state in {
                NodeState.PLANNED,
                NodeState.ADMITTED,
                NodeState.DISPATCHED,
                NodeState.RUNNING,
            } and run.state not in {NodeState.BLOCKED, NodeState.VALIDATED}:
                terminal = frozenset(TRANSITIONS.get(run.state, frozenset()))
                if NodeState.BLOCKED in terminal:
                    run.reason = "cancelled by operator"
                    run.state = NodeState.BLOCKED
                    self.events.emit(
                        "node_state",
                        run.node.node_id,
                        state="BLOCKED",
                        reason="cancelled by operator",
                    )
                elif NodeState.TERMINAL_FAILURE in terminal:
                    run.reason = "cancelled by operator"
                    run.state = NodeState.TERMINAL_FAILURE
                    self.events.emit(
                        "node_state",
                        run.node.node_id,
                        state="TERMINAL_FAILURE",
                        reason="cancelled by operator",
                    )
        self.events.emit(
            "control",
            id=req.id,
            kind="cancel_run",
            accepted=True,
            reason="run cancelled by operator",
            requested_by=req.requested_by,
        )

    def _handle_cancel_node(
        self, req: ControlRequest, pending: dict[str, asyncio.Task[None]]
    ) -> None:
        """Cancel a specific node — no automatic replacement; dependents BLOCKED."""
        node_id = req.node_id or ""
        if node_id not in self.nodes:
            self.events.emit(
                "control",
                id=req.id,
                kind="cancel_node",
                accepted=False,
                reason=f"unknown node_id: {node_id!r}",
            )
            return
        run = self.nodes[node_id]
        if run.state not in {
            NodeState.PLANNED,
            NodeState.ADMITTED,
            NodeState.DISPATCHED,
            NodeState.RUNNING,
        }:
            self.events.emit(
                "control",
                node_id,
                id=req.id,
                kind="cancel_node",
                accepted=False,
                reason=f"node {node_id} is {run.state.value}; only active work can be cancelled",
            )
            return
        # Cancel the running task if present
        if node_id in pending:
            pending[node_id].cancel()
        # Transition to BLOCKED via the legal path
        if run.state in {
            NodeState.PLANNED,
            NodeState.ADMITTED,
            NodeState.DISPATCHED,
            NodeState.RUNNING,
        }:
            terminal = frozenset(TRANSITIONS.get(run.state, frozenset()))
            if NodeState.TERMINAL_FAILURE in terminal:
                run.reason = "cancelled_by_operator"
                run.state = NodeState.TERMINAL_FAILURE
                self.events.emit(
                    "node_state", node_id, state="TERMINAL_FAILURE", reason="cancelled_by_operator"
                )
                # Move to BLOCKED to prevent automatic recovery
                run.state = NodeState.BLOCKED
                self.events.emit(
                    "node_state", node_id, state="BLOCKED", reason="cancelled_by_operator"
                )
            elif NodeState.BLOCKED in terminal:
                run.reason = "cancelled_by_operator"
                run.state = NodeState.BLOCKED
                self.events.emit(
                    "node_state", node_id, state="BLOCKED", reason="cancelled_by_operator"
                )
        elif run.state in {NodeState.TERMINAL_FAILURE, NodeState.REJECTED}:
            # Already failed; block to prevent retry
            run.reason = "cancelled_by_operator"
            run.state = NodeState.BLOCKED
            self.events.emit("node_state", node_id, state="BLOCKED", reason="cancelled_by_operator")
        self.events.emit(
            "control",
            id=req.id,
            kind="cancel_node",
            accepted=True,
            reason="node cancelled by operator",
            node_id=node_id,
            requested_by=req.requested_by,
        )

    def _handle_retry_node(
        self, req: ControlRequest, pending: dict[str, asyncio.Task[None]]
    ) -> None:
        """Retry a TERMINAL_FAILURE/BLOCKED node through RecoveryBudget."""
        node_id = req.node_id or ""
        if node_id not in self.nodes:
            self.events.emit(
                "control",
                id=req.id,
                kind="retry_node",
                accepted=False,
                reason=f"unknown node_id: {node_id!r}",
            )
            return
        run = self.nodes[node_id]
        if run.state not in {NodeState.TERMINAL_FAILURE, NodeState.BLOCKED}:
            self.events.emit(
                "control",
                id=req.id,
                kind="retry_node",
                accepted=False,
                reason=f"node {node_id} in state {run.state.value}; retry only for TERMINAL_FAILURE/BLOCKED",
                node_id=node_id,
            )
            return
        if node_id in pending and not pending[node_id].done():
            self.events.emit(
                "control",
                node_id,
                id=req.id,
                kind="retry_node",
                accepted=False,
                reason="node driver is still active; retry refused",
            )
            return
        if "cancelled" in run.reason or not run.failures:
            refusal = (
                "node was cancelled; replacement prohibited"
                if "cancelled" in run.reason
                else "no recoverable failure evidence recorded; retry refused"
            )
            self.events.emit(
                "control", node_id, id=req.id, kind="retry_node", accepted=False, reason=refusal
            )
            return
        action, reason = self._recovery_budget.decide(node_id, run.failures, attempts=run.attempt)
        if action == "FAIL_CLOSED":
            self.events.emit(
                "control",
                id=req.id,
                kind="retry_node",
                accepted=False,
                reason=f"recovery budget refused: {reason}",
                node_id=node_id,
            )
            return
        # Re-open the node for dispatch
        run.state = NodeState.PLANNED
        run.reason = ""
        self.events.emit(
            "node_state", node_id, state="PLANNED", reason=f"retry requested by operator ({reason})"
        )
        self.events.emit(
            "control",
            id=req.id,
            kind="retry_node",
            accepted=True,
            reason=f"retry accepted: {reason}",
            node_id=node_id,
            requested_by=req.requested_by,
        )

    # ------------------------------------------------------------------ run
    async def run(self) -> RunResult:
        started = time.monotonic()
        self._run_started_monotonic = started
        self._base_sha = await self.git.head() if self.base_ref == "HEAD" else self.base_ref
        self.events.emit(
            "topology",
            topology=self.graph.topology.value,
            max_parallel=min(self.policy.max_parallel, self.graph.max_parallel),
            rationale=list(self.graph.rationale),
            layers=[list(layer) for layer in self.graph.layers()],
        )
        pending: dict[str, asyncio.Task[None]] = {}
        try:
            while True:
                if time.monotonic() - started > self.policy.run_deadline_seconds:
                    for task in pending.values():
                        task.cancel()
                    return await self._finish(RunOutcome.BLOCKED, "run deadline exceeded")
                # Poll external control channel
                cancel = self._poll_controls(pending)
                if cancel is not None:
                    # Wait for the cancellable executor to kill its process group
                    # before publishing the terminal event and receipt.
                    await asyncio.gather(*pending.values(), return_exceptions=True)
                    pending.clear()
                    return await self._finish(RunOutcome.CANCELLED, "run cancelled by operator")
                self._propagate_blocks()
                for node_id in self._ready():
                    if node_id not in pending:
                        pending[node_id] = asyncio.create_task(self._drive(node_id))
                if not pending:
                    break
                # Wake often enough to notice a cancel while workers are running.
                done, _ = await asyncio.wait(
                    pending.values(), timeout=0.1, return_when=asyncio.FIRST_COMPLETED
                )
                for node_id, task in list(pending.items()):
                    if task in done:
                        del pending[node_id]
                        if task.cancelled():
                            continue
                        exc = task.exception()
                        if exc is not None:  # isolation: a crashed driver blocks only its node
                            run = self.nodes[node_id]
                            run.reason = f"driver crash: {type(exc).__name__}: {exc}"
                            if run.state not in {NodeState.VALIDATED, NodeState.BLOCKED}:
                                run.state = NodeState.BLOCKED
                            self.events.emit(
                                "node_state", node_id, state="BLOCKED", reason=run.reason
                            )
        finally:
            for task in pending.values():
                task.cancel()
            if pending:
                await asyncio.gather(*pending.values(), return_exceptions=True)
        return await self._conclude()

    def _ready(self) -> list[str]:
        if self._cancel_requested:
            return []  # Stop admitting new work
        ready = []
        for node_id, run in self.nodes.items():
            if run.state is not NodeState.PLANNED:
                continue
            deps = [self.nodes[d] for d in run.node.depends_on]
            if all(d.state is NodeState.VALIDATED for d in deps):
                ready.append(node_id)
        return ready

    def _propagate_blocks(self) -> None:
        changed = True
        while changed:
            changed = False
            for run in self.nodes.values():
                if run.state is not NodeState.PLANNED:
                    continue
                blocked = [
                    d for d in run.node.depends_on if self.nodes[d].state is NodeState.BLOCKED
                ]
                if blocked:
                    run.state = NodeState.BLOCKED
                    run.reason = f"dependency blocked: {', '.join(blocked)}"
                    self.events.emit(
                        "node_state", run.node.node_id, state="BLOCKED", reason=run.reason
                    )
                    changed = True

    # ------------------------------------------------------------------ node
    async def _drive(self, node_id: str) -> None:
        run = self.nodes[node_id]
        if run.node.kind in {NodeKind.INTEGRATE, NodeKind.REVIEW}:
            await self._integrate_node(run)
            return
        failures = run.failures
        tried = run.excluded_routes
        waited_once = False
        revocations = 0
        while True:
            if run.attempt >= self.policy.max_attempts_per_node:
                run.reason = (
                    f"replacement budget exhausted after {run.attempt} attempts: "
                    + ", ".join(f"{h['route_id']}:{h['outcome']}" for h in run.history)
                )
                self._set(run, NodeState.BLOCKED, reason=run.reason)
                self.events.emit(
                    "failure",
                    node_id,
                    category="pool_exhausted",
                    action="FAIL_CLOSED",
                    route_id=run.route_id,
                    evidence=run.reason,
                )
                return
            requirements = TaskRequirements.for_node(
                run.node,
                exclude_routes=frozenset(tried),
                exclude_pools=frozenset(run.excluded_pools),
            )
            # BOD-341: selection reads the health cache/inventory and can take
            # seconds. Running it inline here would block the whole event loop
            # (dispatch, slot release, other nodes' driving) for that long, so
            # --max-parallel is never reached. Run it in a thread instead.
            choice, considered, _post = await asyncio.to_thread(
                self._select_with_stats, requirements, self.now()
            )
            counts = _ladder_counts(considered)
            if _post:
                counts = {**counts, **_post}
            # BOD-277: per-route eligibility verdicts for trace drill-down.
            _rejections = _build_rejections(considered)
            _selected_route = choice.route_id if choice is not None else None
            _cands, _cands_omitted, _omitted_summary = _build_candidates(
                considered, _selected_route
            )
            _evidence: dict[str, Any] = {
                "rejections": _rejections,
                "candidates": _cands,
                "candidates_omitted": _cands_omitted,
                "omitted_summary": _omitted_summary,  # AC7: per-state detail for omitted
            }
            if choice is None:
                # Check if we can wait for a short cooldown to expire
                earliest_cooldown: datetime | None = None
                if not waited_once:
                    for v in considered:
                        # Only consider routes that failed at AVAILABLE stage with a cooldown
                        if (
                            v.failed_stage == EligibilityStage.AVAILABLE
                            and v.cooldown_until
                            and v.route_id not in tried
                        ):
                            try:
                                cooldown_time = datetime.fromisoformat(v.cooldown_until)
                                if earliest_cooldown is None or cooldown_time < earliest_cooldown:
                                    earliest_cooldown = cooldown_time
                            except (ValueError, TypeError):
                                pass

                # If we found a short cooldown, wait for it
                if earliest_cooldown is not None:
                    wait_seconds = (earliest_cooldown - self.now()).total_seconds()
                    if 0 < wait_seconds <= self.policy.max_cooldown_wait_seconds:
                        self.events.emit(
                            "cooldown",
                            node_id,
                            key="pool",
                            scope="wait",
                            category="waiting_for_capacity",
                            until=earliest_cooldown.isoformat(),
                        )
                        await _sleep(wait_seconds + 1)
                        waited_once = True
                        continue

                # Otherwise, fail closed as before
                run.reason = "no eligible model: " + _explain_exhaustion(considered)
                self.events.emit("eligibility", node_id, **counts, **_evidence, selected=None)
                self._set(run, NodeState.BLOCKED, reason=run.reason)
                self.events.emit(
                    "failure",
                    node_id,
                    category="pool_exhausted",
                    action="FAIL_CLOSED",
                    route_id="",
                    evidence=run.reason,
                )
                return
            # Launch gate: the selected route must be proven healthy or confirmed
            # live before it is bound. Raises AdmissionBypassError on violation.
            require_launchable(self.selector, choice.route_id, surface="DagRuntime.bind")
            # BOD-223: re-check launch-critical evidence immediately before
            # dispatch. A route/provider cooled since selection is not launched;
            # the node goes back through selection on the refreshed evidence.
            blocker = dispatch_blocker(self.selector, choice.route_id, self.now())
            if blocker is not None:
                self.events.emit(
                    "eligibility",
                    node_id,
                    **counts,
                    **_evidence,
                    selected=None,
                    revoked=choice.route_id,
                    reason=f"pre-dispatch recheck: {blocker} cooling",
                )
                tried.add(choice.route_id)
                revocations += 1
                if revocations > self.policy.max_attempts_per_node:
                    run.reason = "pre-dispatch revalidation kept revoking selections"
                    self._set(run, NodeState.BLOCKED, reason=run.reason)
                    self.events.emit(
                        "failure",
                        node_id,
                        category="pool_exhausted",
                        action="FAIL_CLOSED",
                        route_id=choice.route_id,
                        evidence=run.reason,
                    )
                    return
                continue
            if self.dispatch_authority is not None:
                # Reads the passport store and health cache: keep it off the loop (BOD-341).
                run.dispatch_proof = await asyncio.to_thread(
                    self._bind_proof, run.node, choice.route_id
                )
                if run.dispatch_proof is None:
                    self.events.emit(
                        "eligibility",
                        node_id,
                        **counts,
                        **_evidence,
                        selected=None,
                        revoked=choice.route_id,
                        reason="pre-dispatch recheck: no_fresh_passport",
                    )
                    tried.add(choice.route_id)
                    revocations += 1
                    if revocations > self.policy.max_attempts_per_node:
                        run.reason = "pre-dispatch revalidation kept revoking selections"
                        self._set(run, NodeState.BLOCKED, reason=run.reason)
                        self.events.emit(
                            "failure",
                            node_id,
                            category="pool_exhausted",
                            action="FAIL_CLOSED",
                            route_id=choice.route_id,
                            evidence=run.reason,
                        )
                        return
                    continue
            previous = run.route_id
            run.attempt += 1
            run.route_id = choice.route_id
            run.selected_context_window = choice.context_window
            self.events.emit(
                "eligibility",
                node_id,
                **counts,
                **_evidence,
                selected=choice.route_id,
                capacity_class=choice.capacity_class.value,
                rank=choice.rank,
            )
            # Emit probe-class fields so receipts record qualifying probe info.
            _probe_fields: dict[str, Any] = {}
            if choice.rank_components:
                for _pf in ("probe_class", "cache_checked_at", "cache_freshness"):
                    if choice.rank_components.get(_pf) is not None:
                        _probe_fields[_pf] = str(choice.rank_components[_pf])
            # Record UNKNOWN capacity opt-in when an UNKNOWN route was selected.
            if choice.capacity_class == CapacityClass.UNKNOWN and getattr(
                self.selector, "_allow_unknown", False
            ):
                _probe_fields["unknown_capacity_opt_in"] = True
            self.events.emit(
                "selection",
                node_id,
                route_id=choice.route_id,
                provider=choice.provider,
                capacity_class=choice.capacity_class.value,
                plan=choice.plan_label,
                rank=choice.rank,
                attempt=run.attempt,
                **_probe_fields,
            )
            self._capacity[node_id] = choice.capacity_class.value
            self.inflight[node_id] = choice.route_id
            if previous and failures:
                self.events.emit(
                    "reassign",
                    node_id,
                    from_route=previous,
                    to_route=choice.route_id,
                    reason=failures[-1].category,
                    attempt=run.attempt,
                )
            ok = await self._attempt(run, failures)
            if ok is None:
                # BOD-223: evidence changed while this node waited for a slot.
                # Nothing was dispatched; undo the binding and reselect.
                run.attempt -= 1
                run.route_id = previous
                self.inflight.pop(node_id, None)
                tried.add(choice.route_id)
                revocations += 1
                if revocations > self.policy.max_attempts_per_node:
                    run.reason = "pre-dispatch revalidation kept revoking selections"
                    self._set(run, NodeState.BLOCKED, reason=run.reason)
                    self.events.emit(
                        "failure",
                        node_id,
                        category="pool_exhausted",
                        action="FAIL_CLOSED",
                        route_id=choice.route_id,
                        evidence=run.reason,
                    )
                    return
                continue
            if ok:
                return
            run.failure_feedback = ""
            if failures:
                last = failures[-1]
                if last.category == "ownership_violation":
                    run.failure_feedback = (
                        last.evidence[:1700]
                        + "\nThe out-of-scope files above must not be created or modified. "
                        "Only OWNED_FILES may change. If one is required, finish with "
                        "RESULT: BLOCKED naming the file."
                    )
                elif last.category == "verification_failed":
                    run.failure_feedback = last.evidence[-2000:]
            if failures and failures[-1].action == "RETRY_INFRA":
                # Gateway-local transient: same route is still healthy; wait and retry.
                await asyncio.sleep(min(failures[-1].cooldown_seconds, 60.0))
            elif failures and failures[-1].category == CONTEXT_LENGTH_CATEGORY:
                # BOD-272: the request, not the route, was too large. Shrink the
                # pack for every later attempt. Retry the SAME route at most once,
                # and only when the prompt can actually get smaller; otherwise
                # move on, so one route cannot burn the whole attempt budget.
                shrunk = self._repack(run)
                if (
                    shrunk
                    and self._hydrator_takes_budget()
                    and run.route_id not in run.repacked_routes
                ):
                    run.repacked_routes.add(run.route_id)
                else:
                    tried.add(run.route_id)
            elif (
                failures
                and failures[-1].category in {"verification_failed", "ownership_violation"}
                and run.route_id not in run.rehydrated_routes
            ):
                # Failure-directed rehydration: give the same route one retry
                # with the failing barrier evidence before escalating.
                run.rehydrated_routes.add(run.route_id)
                self.events.emit(
                    "rehydrate",
                    node_id,
                    reason=failures[-1].category,
                    route_id=run.route_id,
                    evidence_chars=len(run.failure_feedback),
                    attempt=run.attempt,
                )
            else:
                tried.add(run.route_id)
                if failures and failures[-1].category in POOL_COOLDOWN_CATEGORIES:
                    run.excluded_pools.add(pool_of(run.route_id))
            if failures and failures[-1].action == "BLOCK":
                run.reason = f"non-recoverable: {failures[-1].category}"
                self._set(run, NodeState.BLOCKED, reason=run.reason)
                return
            self._set(run, NodeState.PLANNED, reassign=True)

    async def _run_repo_gates(
        self,
        run: NodeRun,
        worktree: Path,
        *,
        worker_fixable_only: bool = False,
        failed: list[str] | None = None,
    ) -> str | None:
        """Run every declared gate, retaining the first failure with bounded evidence.

        ``worker_fixable_only`` (implement-node validation) reports only real check
        failures: configuration errors and missing executables are left for the
        integration barrier, since the node's worker cannot fix them.
        """
        node_id = run.node.node_id
        if not self.policy.run_repo_gates:
            self.events.emit(
                "repo_gates", node_id, disabled=True, note="disabled by runtime policy"
            )
            return None
        try:
            gates = discover_repo_gates(worktree)
        except ValueError as exc:
            config_reason = f"repo_gates_config_error: {exc}"[:300]
            self.events.emit(
                "repo_gates",
                node_id,
                declared=None,
                gates=[],
                note="discovery failed",
                error=config_reason,
            )
            return None if worker_fixable_only else config_reason
        self.events.emit("repo_gates", node_id, **describe_gates(gates))
        reason: str | None = None
        for gate in gates:
            resolved = list(gate.argv)
            try:
                resolved = _resolve_gate_argv(gate.argv, worktree)
            except FileNotFoundError as exc:
                code, out = 127, str(exc)
            else:
                code, out = await self.runner(
                    resolved, worktree, self.policy.verify_timeout_seconds
                )
            self.events.emit(
                "repo_gate",
                node_id,
                name=gate.name,
                ok=code == 0,
                exit_code=code,
                command=shlex.join(gate.argv),
                executed_command=shlex.join(resolved),
                source=gate.source,
                tail=out[-600:],
            )
            fixable = code not in (126, 127) or not worker_fixable_only
            if code != 0 and fixable:
                message = f"repo_gate_failed: {gate.name} exit {code}: {out[-600:]}"
                reason = reason or message
                if failed is not None:
                    failed.append(message)
        return reason

    async def _integrate_node(self, run: NodeRun) -> None:
        """Mechanical integration barrier: merge, run combined tests and repo quality gates."""
        node = run.node
        run.attempt += 1
        run.route_id = ""
        self._set(run, NodeState.ADMITTED)
        self._set(run, NodeState.DISPATCHED)
        self._set(run, NodeState.RUNNING)
        try:
            base = await self._node_base(node)
        except OrchestrationError as exc:
            self._set(run, NodeState.TERMINAL_FAILURE, reason="merge_conflict")
            run.reason = f"integration merge failed: {exc}"
            self.events.emit(
                "barrier",
                node.node_id,
                name=node.barrier or "integration",
                ok=False,
                detail=run.reason[:300],
            )
            self._set(run, NodeState.BLOCKED, reason=run.reason)
            return
        self.events.emit(
            "terminal",
            node.node_id,
            ok=True,
            route_id="",
            attempt=run.attempt,
            duration_seconds=0.0,
            reported_model="(mechanical merge)",
        )
        self._set(run, NodeState.TERMINAL_SUCCESS)
        worktree = self.run_dir / "worktrees" / f"{node.node_id}-a{run.attempt}"
        await self.git.add_worktree(
            worktree, f"verdict/run-{self.run_dir.name}/{node.node_id}", base
        )
        try:
            ok = True
            reason = ""
            if node.verification_command:
                resolved, resolved_argv0 = _resolve_verify_argv(node.verification_command)
                code, out = await self.runner(
                    resolved, worktree, self.policy.verify_timeout_seconds
                )
                ok = code == 0
                verify_extra: dict[str, Any] = {}
                if resolved_argv0:
                    verify_extra["resolved_argv0"] = resolved_argv0
                self.events.emit(
                    "verify",
                    node.node_id,
                    ok=ok,
                    exit_code=code,
                    command=shlex.join(node.verification_command),
                    executed_command=shlex.join(resolved),
                    tail=out[-600:],
                    **verify_extra,
                )
                if not ok:
                    reason = f"integration verification failed: exit {code}: {out[-600:]}"
            if ok:
                reason = await self._run_repo_gates(run, worktree) or ""
                ok = not reason
            self.events.emit(
                "barrier",
                node.node_id,
                name=node.barrier or "integration",
                ok=ok,
                detail=reason[:300] if reason else "combined verification and repo gates passed",
            )
        finally:
            await self.git.remove_worktree(worktree)
        if not ok:
            run.reason = reason
            self._record_failure(
                run,
                WorkerTerminal(ok=False, error="verification_failed: " + reason),
                run.failures,
                validated=True,
                # Gate diagnostics describe verification, never worker transport faults.
                failure=FailureClassification(
                    "verification_failed",
                    "CORRECT_IMPLEMENTATION",
                    0,
                    "none",
                    "verification_failed: " + reason,
                ),
            )
            run.failure_feedback = reason[-2000:]
            run.history[-1]["evidence"] = run.failure_feedback
            self._set(run, NodeState.BLOCKED, reason=run.reason)
            return
        run.commit = base
        run.history.append(
            {"attempt": run.attempt, "route_id": "", "outcome": "validated", "commit": base}
        )
        self._set(run, NodeState.VALIDATED, commit=base)

    def _hydrator_takes_budget(self) -> bool:
        try:
            params = inspect.signature(self.prompt_for).parameters
        except (TypeError, ValueError):
            return False
        return any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()) or any(
            key in params and params[key].kind != inspect.Parameter.POSITIONAL_ONLY
            for key in ("context_budget_bytes", "context_plan")
        )

    def _context_plan(self, run: NodeRun, budget: int) -> ContextPlan:
        # RouteVerdict already owns this fact. It is a conservative catalog
        # ceiling: the public contract does not distinguish input-only from
        # total windows. Never infer the source shape or query private inventory.
        output, safety = _WORKER_OUTPUT_RESERVE, _WORKER_SAFETY_RESERVE
        window = run.selected_context_window
        if isinstance(window, bool) or not isinstance(window, int) or window < 1:
            window = None
        total = min(window if window is not None else 8192, max(0, budget // 4) + output + safety)
        return ContextPlan(
            plan_id=f"worker:{run.node.node_id}:a{run.attempt}",
            candidate_id=run.route_id,
            # ContextPlan requires one remaining input token. Clamp reserves
            # only to represent a tiny ceiling, not to waive the runtime's
            # reserve policy. The event records the shortfall and rejects input.
            token_budget=max(1, total),
            output_token_reserve=min(output, max(0, total - 1)),
            tool_token_reserve=min(safety, max(0, total - output - 1)),
            candidate_context_window=window,
        )

    def _prompt(
        self, node: WorkNode, worktree: Path, budget: int, plan: ContextPlan, suffix: str
    ) -> str | WorkerPrompt:
        """Pass only supported keywords; legacy strings have unknown provenance."""
        values: dict[str, Any] = {
            "context_budget_bytes": budget,
            "context_plan": plan,
            "mandatory_suffix": suffix,
        }
        params: Mapping[str, inspect.Parameter]
        try:
            params = inspect.signature(self.prompt_for).parameters
        except (TypeError, ValueError):
            params = {}
        arbitrary = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values())
        kwargs = {
            k: v
            for k, v in values.items()
            if arbitrary or (k in params and params[k].kind != inspect.Parameter.POSITIONAL_ONLY)
        }
        result = self.prompt_for(node, worktree, **kwargs)
        if "mandatory_suffix" not in kwargs and suffix:
            if isinstance(result, WorkerPrompt):
                # Metadata is no longer authoritative after a foreign hydrator
                # omitted retry instructions. Report unknown sources, not a lie.
                return result.text + suffix
            return result + suffix
        return result

    def _repack(self, run: NodeRun) -> bool:
        """Halve the node's context budget after an overflow. False once at the floor."""
        current = run.context_budget or self.policy.context_budget_bytes
        smaller = max(self.policy.min_context_budget_bytes, current // 2)
        if smaller >= current:
            return False
        run.context_budget = smaller
        self.events.emit(
            "repack",
            run.node.node_id,
            reason=CONTEXT_LENGTH_CATEGORY,
            from_budget_bytes=current,
            to_budget_bytes=smaller,
            attempt=run.attempt,
        )
        return True

    async def _attempt(self, run: NodeRun, failures: list[FailureClassification]) -> bool | None:
        """Run one attempt. ``None`` means revoked before dispatch (nothing ran)."""
        node = run.node
        run.permit = None
        worktree = self.run_dir / "worktrees" / f"{node.node_id}-a{run.attempt}"
        branch = f"verdict/run-{self.run_dir.name}/{node.node_id}-a{run.attempt}"
        async with self._slots:
            # BOD-223: the node may have waited for a slot while a sibling's
            # failure cooled this route/provider. Re-check launch-critical
            # evidence now, immediately before dispatch, not only at binding.
            blocker = dispatch_blocker(self.selector, run.route_id, self.now())
            if blocker is not None:
                self.events.emit(
                    "eligibility",
                    node.node_id,
                    selected=None,
                    revoked=run.route_id,
                    reason=f"pre-dispatch recheck: {blocker} cooling",
                )
                return None
            self._set(run, NodeState.ADMITTED)
            base = await self._node_base(node)
            await self.git.add_worktree(worktree, branch, base)
            # BOD-308: restore salvaged partial work from a previous attempt.
            if run.attempt > 1 and run.node.owned_files:
                salvage_dir = self.run_dir / "salvage" / run.node.node_id
                restore = restore_salvaged_files(salvage_dir, worktree, run.node.owned_files)
                if restore.restored or restore.error:
                    restored_proj = list(restore.restored[:_MAX_EVIDENCE_PATHS])
                    skipped_proj = list(restore.skipped[:_MAX_EVIDENCE_PATHS])
                    restore_truncated = (
                        len(restore.restored) > _MAX_EVIDENCE_PATHS
                        or len(restore.skipped) > _MAX_EVIDENCE_PATHS
                    )
                    self.events.emit(
                        "restore",
                        node.node_id,
                        restored=restored_proj,
                        restored_count=len(restore.restored),
                        skipped=skipped_proj,
                        total_bytes=restore.total_bytes,
                        attempt=run.attempt,
                        evidence_truncated=restore_truncated,
                        error=restore.error,
                    )
                    run.history.append(
                        {
                            "attempt": run.attempt,
                            "route_id": run.route_id,
                            "outcome": "restore",
                            "restored": restored_proj,
                            "restored_count": len(restore.restored),
                            "skipped": skipped_proj,
                            "total_bytes": restore.total_bytes,
                            "evidence_truncated": restore_truncated,
                            "error": restore.error,
                        }
                    )
            self.inflight[node.node_id] = run.route_id
            try:
                self._set(run, NodeState.DISPATCHED)
                self.events.emit(
                    "dispatch",
                    node.node_id,
                    route_id=run.route_id,
                    attempt=run.attempt,
                    worktree=str(worktree),
                    base=base,
                    provider=route_provider(run.route_id),
                    capacity_class=self._capacity.get(node.node_id, "unknown"),
                )
                self._set(run, NodeState.RUNNING)
                budget = run.context_budget or self.policy.context_budget_bytes
                plan = self._context_plan(run, budget)
                suffix = ""
                if run.failure_feedback:
                    ownership = run.failures[-1].category == "ownership_violation"
                    header = (
                        "PREVIOUS_ATTEMPT_FAILED_OWNERSHIP"
                        if ownership
                        else "PREVIOUS_ATTEMPT_FAILED_VERIFICATION"
                    )
                    suffix = (
                        f"\n\n{header}:\n"
                        + run.failure_feedback
                        + "\nFix the cause above, then run VERIFICATION_COMMAND again.\n"
                    )
                hydrated = self._prompt(node, worktree, budget, plan, suffix)
                prompt = hydrated.text if isinstance(hydrated, WorkerPrompt) else hydrated
                evidence = (
                    hydrated.evidence()
                    if isinstance(hydrated, WorkerPrompt)
                    else {
                        "sources": [],
                        "sources_status": "unknown_legacy_callback",
                        "budget_source": "candidate_plan"
                        if plan.candidate_context_window is not None
                        else "conservative_unknown_candidate",
                        "omissions": ["legacy_callback_pack_provenance_unavailable"],
                    }
                )
                input_limit = plan.input_token_budget
                if plan.candidate_context_window is not None:
                    input_limit = max(
                        0,
                        min(
                            input_limit,
                            plan.candidate_context_window
                            - _WORKER_OUTPUT_RESERVE
                            - _WORKER_SAFETY_RESERVE,
                        ),
                    )
                estimated = estimate_tokens(prompt)
                fits = estimated <= input_limit and len(prompt.encode()) <= budget
                self.events.emit(
                    "hydrate",
                    node.node_id,
                    context_files=list(node.required_context),
                    prompt_bytes=len(prompt.encode()),
                    # A plain-string callback has no pack provenance; only its own
                    # truncation marker shows lost content.
                    truncated=bool(evidence["omissions"])
                    if isinstance(hydrated, WorkerPrompt)
                    else "[truncated" in prompt.lower(),
                    budget_bytes=budget,
                    route_id=run.route_id,
                    context_plan=plan.to_dict(),
                    input_token_budget=input_limit,
                    reserve_policy={
                        "output_tokens": _WORKER_OUTPUT_RESERVE,
                        "safety_tokens": _WORKER_SAFETY_RESERVE,
                        "shortfall_tokens": _WORKER_OUTPUT_RESERVE
                        + _WORKER_SAFETY_RESERVE
                        - plan.output_token_reserve
                        - plan.tool_token_reserve,
                    },
                    prompt_tokens_estimated=estimated,
                    prompt_tokens_measured=None,
                    tokenizer="unknown:offline_four_char_estimate",
                    estimated_fit=fits,
                    compression="not_performed",
                    **evidence,
                )
                if not fits:
                    # This is a local input failure, not admission or provider
                    # health. Do not launch or let transport classification cool
                    # a route for mandatory instructions that cannot fit.
                    reason = "context_input_rejected: full prompt exceeds conservative input budget"
                    terminal = WorkerTerminal(ok=False, error=reason)
                    self._record_failure(
                        run,
                        terminal,
                        failures,
                        failure=FailureClassification(
                            "context_input_rejected", "FAIL_CLOSED", 0, "none", reason
                        ),
                    )
                    run.reason = reason
                    self._set(run, NodeState.BLOCKED, reason=reason)
                    await self.git.remove_worktree(worktree)
                    return True  # attempt settled locally; do not reselect
                if self.dispatch_authority is not None:
                    run.permit = await self._commit_permit(run)
                    if run.permit is None:
                        reason = "permit_denied: proof revoked or expired before the send"
                        self._record_failure(
                            run,
                            WorkerTerminal(ok=False, error=reason),
                            failures,
                            failure=FailureClassification(
                                "permit_denied", "REROUTE", 0, "none", reason
                            ),
                        )
                        await self.git.remove_worktree(worktree)
                        return False  # nothing was sent; reselect another route
                elif self.policy.require_dispatch_permit:
                    reason = "dispatch_denied: no admission proof for the bound route"
                    terminal = WorkerTerminal(ok=False, error=reason)
                    self._record_failure(
                        run,
                        terminal,
                        failures,
                        failure=FailureClassification(
                            "dispatch_denied", "BLOCK", 0, "none", reason
                        ),
                    )
                    run.reason = reason
                    self._set(run, NodeState.BLOCKED, reason=reason)
                    await self.git.remove_worktree(worktree)
                    return True  # nothing was sent; do not reselect or retry
                terminal = await self._execute(
                    prompt, run.route_id, worktree, node_id=node.node_id, attempt=run.attempt
                )
            finally:
                self.inflight.pop(node.node_id, None)
            try:
                self.events.emit(
                    "terminal",
                    node.node_id,
                    ok=terminal.ok,
                    route_id=run.route_id,
                    reported_model=terminal.model,
                    # Empty is explicitly unknown for custom adapters; never legacy inference.
                    executor_kind=terminal.executor_kind,
                    harness=terminal.harness,
                    duration_seconds=round(terminal.duration_seconds, 2),
                    session_ref=terminal.session_ref,
                    stop_reason=terminal.stop_reason,
                    error=terminal.error,
                    attempt=run.attempt,
                    fault_injected=terminal.executor_kind == "fault-injected",
                    **(
                        {
                            "usage": {
                                "input_tokens": terminal.usage.input_tokens,
                                "output_tokens": terminal.usage.output_tokens,
                                "cost_usd": terminal.usage.cost_usd,
                                "tokens_source": terminal.usage.tokens_source,
                                "turns": terminal.usage.turns,
                            }
                        }
                        if terminal.usage is not None
                        else ({"usage_missing": True} if terminal.ok else {})
                    ),
                )
                self._save_attempt(node.node_id, run.attempt, run.route_id, terminal)
                blocked_line = _blocked_report(terminal.output) if terminal.ok else ""
                if blocked_line:
                    terminal = WorkerTerminal(
                        ok=False,
                        output=terminal.output,
                        model=terminal.model,
                        error="worker_blocked: " + blocked_line[:200],
                        session_ref=terminal.session_ref,
                        executor_kind=terminal.executor_kind,
                        harness=terminal.harness,
                    )
                if not terminal.ok or not terminal.output.strip():
                    return await self._fail(run, terminal, failures, worktree)
                if run.permit is not None and (
                    await asyncio.to_thread(_revocation_epoch, run.permit.scope) != run.permit.epoch
                ):
                    # The scope was revoked while this send was in flight: the
                    # output is discarded, never salvaged; the retry needs a new permit.
                    reason = "permit_revoked_after_commit: scope revoked during the send"
                    self._record_failure(
                        run,
                        terminal,
                        failures,
                        failure=FailureClassification(
                            "permit_revoked_after_commit", "REROUTE", 0, "none", reason
                        ),
                    )
                    return False
                self._set(run, NodeState.TERMINAL_SUCCESS)
                verdict = await self._validate(run, worktree, base)
                if verdict is not None:
                    failed = WorkerTerminal(ok=False, model=run.route_id, error=verdict)
                    return await self._fail(run, failed, failures, worktree, validated=True)
                run.commit = await self.git.commit_all(
                    worktree, f"verdict({node.node_id}): {node.objective[:60]} [{run.route_id}]"
                )
                # The commit is durable on its branch; the checkout is no longer needed.
                await self.git.remove_worktree(worktree)
                self.selector.record_success(run.route_id, now=self.now())
                run.history.append(
                    {
                        "attempt": run.attempt,
                        "route_id": run.route_id,
                        "outcome": "validated",
                        "commit": run.commit,
                    }
                )
                self._set(run, NodeState.VALIDATED, commit=run.commit)
                return True
            finally:
                if worktree.exists():
                    await self.git.remove_worktree(worktree)

    def _bind_proof(self, node: WorkNode, route_id: str) -> AdmissionProof | None:
        """Fresh passport proof for the exact route, or None when there is none."""
        assert self.dispatch_authority is not None
        provider = route_provider(route_id)
        scope = dispatch_scope(
            gateway_origin=self.dispatch_authority.gateway_origin,
            provider=provider,
            model=route_id,
            route_id=route_id,
            profile_digest=node_profile_digest(node),
        )
        now = self.now()
        confirmation = self.dispatch_authority.confirm([route_id], now)
        return admission_proof(
            confirmation,
            route_id=route_id,
            scope=scope,
            authorization_digest=str(confirmation.get("receipt_id", "")),
            provider=provider,
            model=route_id,
            now=now,
        )

    async def _commit_permit(self, run: NodeRun) -> DispatchPermit | None:
        """Durably consume this attempt's unique id as MAYBE_SENT, or deny (None).

        The id is namespaced by run: permit attempt ids are one global table and
        node ids repeat across runs.
        """
        proof = run.dispatch_proof
        if proof is None or proof.route_id != run.route_id:
            return None
        attempt_id = permit_attempt_id(self.run_dir, run.node.node_id, run.attempt)
        deadline = min(
            proof.expires_at,
            proof.qualified_at + timedelta(seconds=PERMIT_PROOF_MAX_VALIDITY_SECONDS),
        )
        now = self.now()
        permit = await asyncio.to_thread(
            lambda: HealthCache(default_cache_path()).commit_dispatch(
                proof.scope,
                route_id=proof.route_id,
                attempt_id=attempt_id,
                authorization_digest=proof.authorization_digest,
                expected_epoch=proof.epoch,
                proof_deadline=deadline,
                now=now,
            )
        )
        if permit is not None:
            self.events.emit(
                "controller",
                run.node.node_id,
                phase="dispatch_permit",
                route_id=permit.route_id,
                attempt_id=attempt_id,
                permit_id=permit.permit_id,
                epoch=permit.epoch,
                gateway_origin=permit.scope.gateway_origin,
            )
        return permit

    def _save_attempt(
        self, node_id: str, attempt: int, route_id: str, terminal: WorkerTerminal
    ) -> None:
        """Durable per-attempt evidence (sanitized by the executor); never fatal."""
        if terminal.executor_kind == "fault-injected":
            return

        import json

        try:
            directory = self.run_dir / "attempts"
            directory.mkdir(parents=True, exist_ok=True)
            record: dict[str, object] = {
                "node_id": node_id,
                "attempt": attempt,
                "route_id": route_id,
                "ok": terminal.ok,
                "reported_model": terminal.model,
                "stop_reason": terminal.stop_reason,
                "status_code": terminal.status_code,
                "error": terminal.error[:2000],
                "output_tail": terminal.output[-2000:],
                "duration_seconds": terminal.duration_seconds,
                "session_ref": terminal.session_ref,
            }
            if terminal.usage is not None:
                record["usage"] = {
                    "input_tokens": terminal.usage.input_tokens,
                    "output_tokens": terminal.usage.output_tokens,
                    "cost_usd": terminal.usage.cost_usd,
                    "tokens_source": terminal.usage.tokens_source,
                    "turns": terminal.usage.turns,
                }
            (directory / f"{node_id}-a{attempt}.json").write_text(json.dumps(record, indent=1))
        except OSError:
            pass

    async def _execute(
        self, prompt: str, route_id: str, cwd: Path, *, node_id: str, attempt: int
    ) -> WorkerTerminal:
        timeout_seconds = min(float(self.policy.attempt_timeout_seconds), 900.0)
        try:
            return await bounded_executor_call(
                self.executor,
                prompt,
                route_id=route_id,
                cwd=cwd,
                timeout_seconds=timeout_seconds,
                run_dir=self.run_dir,
                phase="worker",
                attempt_id=f"{node_id}-a{attempt}",
            )
        except OwnershipError as exc:
            return WorkerTerminal(
                ok=False, model=route_id, error=f"ownership_refused: {exc}", status_code=125
            )
        except Exception as exc:  # executor bugs must not unwind the controller
            return WorkerTerminal(
                ok=False, model=route_id, error=f"transport: {type(exc).__name__}: {exc}"
            )

    def _record_failure(
        self,
        run: NodeRun,
        terminal: WorkerTerminal,
        failures: list[FailureClassification],
        *,
        validated: bool = False,
        failure: FailureClassification | None = None,
    ) -> None:
        if failure is None:
            failure = self.classifier.classify(terminal, now=self.now())
        failures.append(failure)
        if validated:
            self._set(run, NodeState.REJECTED, reason=failure.category)
        else:
            self._set(run, NodeState.TERMINAL_FAILURE, reason=failure.category)
        permit = run.permit
        self.events.emit(
            "failure",
            run.node.node_id,
            category=failure.category,
            action=failure.action,
            route_id=run.route_id,
            evidence=failure.evidence[:300],
            fault_injected=terminal.executor_kind == "fault-injected",
            attempt=run.attempt,
            **(
                {"permit_id": permit.permit_id, "permit_epoch": permit.epoch}
                if permit is not None
                else {}
            ),
        )
        feedback = getattr(self.selector, "record_session_failure", None)
        if callable(feedback):
            try:
                recorded = feedback(
                    run.route_id,
                    failure,
                    terminal,
                    now=self.now(),
                    provenance=f"{self.run_dir.name}:{run.node.node_id}:a{run.attempt}",
                )
                self.events.emit(
                    "controller",
                    run.node.node_id,
                    route_id=run.route_id,
                    phase="session_feedback",
                    recorded=bool(recorded and recorded.recorded),
                    reason=recorded.reason if recorded is not None else "no_health_cache",
                )
            except Exception as exc:
                self.events.emit(
                    "controller",
                    run.node.node_id,
                    route_id=run.route_id,
                    recorded=False,
                    phase="session_feedback",
                    reason=f"persistence_error:{type(exc).__name__}",
                )
        if failure.scope != "none" and failure.cooldown_seconds > 0:
            self.selector.record_failure(run.route_id, failure, now=self.now())
            pooled = failure.scope == "provider" and failure.category in POOL_COOLDOWN_CATEGORIES
            key = pool_of(run.route_id) if pooled else run.route_id
            until = datetime.fromtimestamp(
                self.now().timestamp() + failure.cooldown_seconds, timezone.utc
            )
            self.events.emit(
                "cooldown",
                run.node.node_id,
                key=key,
                scope="pool" if pooled else "route",
                category=failure.category,
                until=until.isoformat(timespec="seconds"),
            )
        run.history.append(
            {
                "attempt": run.attempt,
                "route_id": run.route_id,
                "outcome": failure.category,
                "fault_injected": terminal.executor_kind == "fault-injected",
            }
        )

    async def _fail(
        self,
        run: NodeRun,
        terminal: WorkerTerminal,
        failures: list[FailureClassification],
        worktree: Path,
        *,
        validated: bool = False,
    ) -> bool:
        try:
            self._record_failure(run, terminal, failures, validated=validated)
            permit = run.permit
            if permit is not None and failures[-1].category in _REVOKING_CATEGORIES:
                event_id = f"{self.run_dir.name}:{permit.permit_id}:{failures[-1].category}"
                observed_at = self.now()
                await asyncio.to_thread(
                    lambda: HealthCache(default_cache_path()).record_revocation(
                        permit.scope,
                        route_id=permit.route_id,
                        event_id=event_id,
                        source_revision=None,
                        observed_at=observed_at,
                    )
                )
            # BOD-308: salvage owned partial work before discarding the worktree.
            if (
                run.node.owned_files
                and worktree.exists()
                and terminal.executor_kind != "fault-injected"
            ):
                salvage_dir = self.run_dir / "salvage" / run.node.node_id
                changed = await self.git.changed_files(worktree, await self._node_base(run.node))
                result = salvage_owned_files(
                    worktree,
                    run.node.owned_files,
                    salvage_dir,
                    changed_files=changed,
                    replace_existing=True,
                )
                if result.saved or result.skipped or result.error:
                    saved_proj = list(result.saved[:_MAX_EVIDENCE_PATHS])
                    skipped_proj = list(result.skipped[:_MAX_EVIDENCE_PATHS])
                    evidence_truncated = (
                        result.evidence_truncated
                        or len(result.saved) > _MAX_EVIDENCE_PATHS
                        or len(result.skipped) > _MAX_EVIDENCE_PATHS
                    )
                    self.events.emit(
                        "salvage",
                        run.node.node_id,
                        saved=saved_proj,
                        saved_count=len(result.saved),
                        skipped=skipped_proj,
                        files=list(result.files),
                        evidence_truncated=evidence_truncated,
                        total_bytes=result.total_bytes,
                        attempt=run.attempt,
                        error=result.error,
                    )
                    run.history.append(
                        {
                            "attempt": run.attempt,
                            "route_id": run.route_id,
                            "outcome": "salvage",
                            "saved": saved_proj,
                            "saved_count": len(result.saved),
                            "skipped": skipped_proj,
                            "files": list(result.files),
                            "evidence_truncated": evidence_truncated,
                            "total_bytes": result.total_bytes,
                            "error": result.error,
                        }
                    )
        finally:
            await self.git.remove_worktree(worktree)
        return False

    async def _node_base(self, node: WorkNode) -> str:
        deps = [self.nodes[d].commit for d in node.depends_on if self.nodes[d].commit]
        if not deps:
            return self._base_sha
        if len(deps) == 1:
            return deps[0]
        return await self._merge_commits(deps, label=f"base-{node.node_id}")

    async def _merge_commits(self, commits: Sequence[str], *, label: str) -> str:
        path = self.run_dir / "worktrees" / f"_{label}"
        await self.git.add_worktree(
            path, f"verdict/run-{self.run_dir.name}/_{label}", self._base_sha
        )
        try:
            for sha in commits:
                await self.git.call(
                    "-c",
                    "user.name=verdict",
                    "-c",
                    "user.email=verdict@localhost",
                    "merge",
                    "--no-ff",
                    "--no-edit",
                    "-q",
                    sha,
                    cwd=path,
                )
            return await self.git.head(path)
        finally:
            await self.git.remove_worktree(path)

    async def _repair_formatting(
        self, run: NodeRun, worktree: Path, changed: list[str]
    ) -> list[str]:
        """Run ``ruff format`` on the node's changed files, then rerun the gates."""
        root = worktree.resolve()
        targets = [
            p
            for p in changed
            if not (worktree / p).is_symlink()
            and (worktree / p).is_file()
            and (worktree / p).resolve().is_relative_to(root)
        ]
        if not targets:
            return ["repo_gate_failed: ruff-format exit 1: no changed files to format"]
        try:
            argv = _resolve_gate_argv(("ruff", "format", *targets), worktree)
        except FileNotFoundError:
            return ["repo_gate_failed: ruff-format exit 127: ruff not found"]
        code, out = await self.runner(argv, worktree, self.policy.verify_timeout_seconds)
        self.events.emit(
            "controller",
            run.node.node_id,
            state="FORMATTER_APPLIED",
            command=shlex.join(["ruff", "format", *targets]),
            files=targets,
            exit_code=code,
            tail=out[-300:],
        )
        failed: list[str] = []
        await self._run_repo_gates(run, worktree, worker_fixable_only=True, failed=failed)
        return failed

    async def _gate_fails_on_base(self, gate_reason: str, base: str) -> bool:
        """Whether the failing gate named in ``gate_reason`` already fails on ``base``."""
        name = gate_reason.removeprefix("repo_gate_failed: ").split(" ", 1)[0]
        tag = secrets.token_hex(4)
        scratch = self.run_dir / "worktrees" / f"gate-base-{tag}"
        await self.git.add_worktree(scratch, f"verdict/gate-base-{tag}", base)
        try:
            for gate in discover_repo_gates(scratch):
                if gate.name == name:
                    resolved = _resolve_gate_argv(gate.argv, scratch)
                    code, _ = await self.runner(
                        resolved, scratch, self.policy.verify_timeout_seconds
                    )
                    return code != 0
            return False
        except (FileNotFoundError, ValueError):
            return False
        finally:
            await self.git.remove_worktree(scratch)
            with contextlib.suppress(OrchestrationError):
                await self.git.call("branch", "-D", f"verdict/gate-base-{tag}")

    async def _validate(self, run: NodeRun, worktree: Path, base: str) -> str | None:
        node = run.node
        changed = await self.git.changed_files(worktree, base)
        if node.kind is NodeKind.IMPLEMENT:
            outside = [p for p in changed if not node.owns(p)]
            self.events.emit(
                "barrier",
                node.node_id,
                name="ownership",
                ok=not outside,
                stray_paths=outside,
                detail=("outside owned: " + ", ".join(outside[:8]))
                if outside
                else f"{len(changed)} file(s) within ownership",
            )
            if outside:
                return "ownership_violation: " + ", ".join(outside[:8])
            if not changed:
                # Already satisfied (e.g. the planner scheduled a file that exists).
                # Acceptance is the verification command, not the size of the diff.
                self.events.emit(
                    "barrier",
                    node.node_id,
                    name="no_change",
                    ok=True,
                    detail="no file changes; accepted only if verification passes",
                )
        elif not node.owned_files and changed:
            # Non-implement nodes with no owned_files (e.g. research) must not
            # produce file changes — their output belongs in the answer text.
            self.events.emit(
                "barrier",
                node.node_id,
                name="ownership",
                ok=False,
                stray_paths=changed,
                detail="no owned_files but changed: " + ", ".join(changed[:8]),
            )
            return "ownership_violation: node has no owned_files but changed: " + ", ".join(
                changed[:8]
            )
        if node.verification_command:
            resolved, resolved_argv0 = _resolve_verify_argv(node.verification_command)
            code, out = await self.runner(resolved, worktree, self.policy.verify_timeout_seconds)
            verify_extra: dict[str, Any] = {}
            if resolved_argv0:
                verify_extra["resolved_argv0"] = resolved_argv0
            self.events.emit(
                "verify",
                node.node_id,
                ok=code == 0,
                exit_code=code,
                command=shlex.join(node.verification_command),
                executed_command=shlex.join(resolved),
                tail=out[-600:],
                **verify_extra,
            )
            if code != 0:
                return f"verification_failed: exit {code}: {out[-300:]}"
            if node.kind is NodeKind.IMPLEMENT and changed:
                # Declared repo gates also run here, so a lint or format slip goes back
                # to this node's worker as correction feedback instead of blocking the
                # run at integration, where no worker can fix it (ADR-036 amendment).
                # Only failures the node introduced count: if a gate already fails on
                # the node's base, that is existing debt for the integration barrier.
                # A gate already failing on the base must not hide a different gate
                # this node newly broke, so every failing gate is checked.
                failed: list[str] = []
                await self._run_repo_gates(run, worktree, worker_fixable_only=True, failed=failed)
                if failed and all(f.startswith("repo_gate_failed: ruff-format ") for f in failed):
                    # Formatting is mechanical: apply the repo's formatter to the files
                    # this node changed, record it, and check the gates again. Lint and
                    # type failures are never auto-fixed; they go back to the worker.
                    failed = await self._repair_formatting(run, worktree, changed)
                for gate_reason in failed:
                    if not await self._gate_fails_on_base(gate_reason, base):
                        return f"verification_failed: {gate_reason[:600]}"
        else:
            self.events.emit(
                "verify", node.node_id, ok=True, exit_code=0, command="(none: non-code node)"
            )
        return None

    # ------------------------------------------------------------------ conclude
    def _remaining_run_seconds(self) -> float:
        now = time.monotonic()
        remaining = float(self.policy.run_deadline_seconds)
        if self._run_started_monotonic is not None:
            remaining = min(remaining, self._run_started_monotonic + remaining - now)
        controller_deadline = os.environ.get("VERDICT_CONTROLLER_DEADLINE")
        if controller_deadline:
            with contextlib.suppress(ValueError):
                remaining = min(remaining, float(controller_deadline) - now)
        return remaining

    def _review_timeout_seconds(self) -> float:
        timeout = min(
            float(self.policy.review_timeout_seconds), 900.0, self._remaining_run_seconds()
        )
        if not (timeout > 0):
            raise OwnershipError("overall controller deadline spent")
        return timeout

    async def _review_with_bound(
        self,
        *,
        level: str,
        round_number: int,
        repo: Path,
        base_ref: str,
        head_ref: str,
        background: str,
        exclude_routes: frozenset[str],
        exclude_families: frozenset[str],
    ) -> ReviewResult:
        assert self.reviewer is not None
        started = time.monotonic()
        timeout_seconds = self._review_timeout_seconds()
        attempt_id = f"review-round-{round_number}"
        with operation_scope(
            self.run_dir, phase="review", attempt_id=attempt_id, timeout_seconds=timeout_seconds
        ):
            try:
                review = self.reviewer.review
                supports_timeout = False
                try:
                    parameters = inspect.signature(review).parameters
                    supports_timeout = "timeout_seconds" in parameters or any(
                        parameter.kind is inspect.Parameter.VAR_KEYWORD
                        for parameter in parameters.values()
                    )
                except (TypeError, ValueError):
                    pass
                if supports_timeout:
                    review_call = cast(Any, review)(
                        repo=repo,
                        base_ref=base_ref,
                        head_ref=head_ref,
                        background=background,
                        exclude_routes=exclude_routes,
                        exclude_families=exclude_families,
                        timeout_seconds=timeout_seconds,
                    )
                else:
                    review_call = review(
                        repo=repo,
                        base_ref=base_ref,
                        head_ref=head_ref,
                        background=background,
                        exclude_routes=exclude_routes,
                        exclude_families=exclude_families,
                    )
                return await asyncio.wait_for(review_call, timeout_seconds)
            except asyncio.TimeoutError:
                return ReviewResult(
                    status="ERROR",
                    reviewer="unknown",
                    route_id="",
                    detail=(
                        f"review {attempt_id} timed out after {timeout_seconds:g}s "
                        f"during {level} independence"
                    ),
                    attempts=(
                        {
                            "route_id": "",
                            "status": "ERROR",
                            "category": "review_timeout",
                            "scope": "none",
                            "duration_seconds": round(time.monotonic() - started, 3),
                        },
                    ),
                )

    async def _conclude(self) -> RunResult:
        blocked = [r for r in self.nodes.values() if r.state is not NodeState.VALIDATED]
        if blocked:
            first = blocked[0]
            return await self._finish(
                RunOutcome.BLOCKED, f"{first.node.node_id}: {first.reason or first.state.value}"
            )
        commits = [r.commit for r in self.nodes.values() if r.commit]
        leaves = self._leaf_commits()
        integration = (
            leaves[0]
            if len(leaves) == 1
            else await self._merge_commits(leaves, label="integration")
        )
        self.events.emit("integrate", ok=True, commits=commits, ref=integration, commit=integration)
        self.events.emit(
            "barrier",
            name="integration",
            ok=True,
            detail=f"{len(commits)} validated commit(s) merged",
        )
        review: ReviewResult | None = None
        if self.policy.require_review:
            if self.reviewer is None:
                return await self._finish(
                    RunOutcome.BLOCKED,
                    "review required but no reviewer configured",
                    integration=integration,
                )
            # Independence policy: the reviewer must not be any route that wrote code in
            # this run, including attempts that failed and were replaced (their partial
            # work can survive into the result). Prefer a different model family too; if
            # no other family has capacity, fall back to route-level independence.
            implementers = frozenset(
                route
                for r in self.nodes.values()
                for route in (r.route_id, *(h.get("route_id", "") for h in r.history))
                if route
            )
            families = frozenset(route_family(r) for r in implementers)
            review = None
            for level, excluded_families in (("family", families), ("route", frozenset())):
                try:
                    candidate = await self._review_with_bound(
                        level=level,
                        round_number=1 if level == "family" else 2,
                        repo=self.repo,
                        base_ref=self._base_sha,
                        head_ref=integration,
                        background=self.graph.goal,
                        exclude_routes=implementers,
                        exclude_families=excluded_families,
                    )
                except Exception as exc:  # fail closed
                    candidate = ReviewResult(
                        status="ERROR",
                        reviewer="unknown",
                        route_id="",
                        detail=f"{type(exc).__name__}: {exc}",
                    )
                review = candidate
                self.events.emit(
                    "controller",
                    state="REVIEW_INDEPENDENCE",
                    level=level,
                    excluded_routes=sorted(implementers),
                    excluded_families=sorted(excluded_families),
                    reviewer_route=candidate.route_id,
                )
                if not (
                    candidate.status == "ERROR" and "no independent reviewer" in candidate.detail
                ):
                    break
            assert review is not None
            for number, attempt in enumerate(review.attempts, 1):
                # BOD-224: every reviewer attempt is evidence, not only the last.
                record: dict[str, Any] = dict(attempt)
                self.events.emit("review_attempt", attempt=number, **record)
            self.events.emit(
                "review",
                status=review.status,
                reviewer=review.reviewer,
                route_id=review.route_id,
                observed_model=review.observed_model,
                blocking=sum(f.blocking() for f in review.findings),
                findings=len(review.findings),
                detail=review.detail[:300],
                raw_ref=review.raw_ref,
            )
            if not review.passed:
                return await self._finish(
                    RunOutcome.BLOCKED,
                    f"independent review {review.status}: {review.detail or 'blocking findings'}",
                    integration=integration,
                    review=review,
                )
        return await self._finish(
            RunOutcome.COMPLETE,
            "all nodes validated, integrated and reviewed",
            integration=integration,
            review=review,
        )

    def _leaf_commits(self) -> list[str]:
        consumed = {d for r in self.nodes.values() for d in r.node.depends_on}
        return [r.commit for nid, r in self.nodes.items() if nid not in consumed and r.commit]

    async def _finish(
        self,
        outcome: RunOutcome,
        reason: str,
        *,
        integration: str = "",
        review: ReviewResult | None = None,
    ) -> RunResult:
        self.events.emit(
            "run_finished", outcome=outcome.value, reason=reason, integration_ref=integration
        )
        return RunResult(outcome, reason, self.nodes, integration, review)


def _explain_exhaustion(verdicts: Sequence[Any]) -> str:
    """Why the pool is empty, most relevant first: cooldowns/quota, then health, then fit."""
    order = {
        "AVAILABLE": 0,
        "HEALTHY": 1,
        "TASK_ELIGIBLE": 2,
        "SELECTED": 3,
        "ENTITLED": 4,
        "DISCOVERED": 5,
    }
    ranked = sorted(
        (v for v in verdicts if getattr(v, "failed_stage", None) is not None),
        key=lambda v: (order.get(v.failed_stage.value, 9), v.route_id),
    )
    parts = [
        f"{v.route_id}:{v.failed_stage.value}:{v.reason}"
        + (f" until {v.cooldown_until}" if v.cooldown_until else "")
        for v in ranked[:8]
    ]
    stages: dict[str, int] = {}
    for v in ranked:
        stages[v.failed_stage.value] = stages.get(v.failed_stage.value, 0) + 1
    summary = ", ".join(f"{count} failed {stage}" for stage, count in sorted(stages.items()))
    return f"[{summary}] " + "; ".join(parts)


def _ladder_counts(verdicts: Sequence[Any]) -> dict[str, int]:
    order = ["DISCOVERED", "ENTITLED", "HEALTHY", "AVAILABLE", "TASK_ELIGIBLE", "SELECTED"]
    counts = {k.lower(): 0 for k in order}
    for verdict in verdicts:
        reached = getattr(verdict, "reached", None)
        if reached is None:
            continue
        idx = order.index(reached.value)
        for k in order[: idx + 1]:
            counts[k.lower()] += 1
    return {
        "discovered": counts["discovered"],
        "entitled": counts["entitled"],
        "healthy": counts["healthy"],
        "available": counts["available"],
        "eligible": counts["task_eligible"],
    }


def _normalize_context_path(rel: str) -> str:
    """Normalise a required_context path to a canonical relative form.

    Removes a leading ``./`` so that ``./a.py`` and ``a.py`` are treated as
    the same file.  ``../`` traversal is left untouched — it is handled (or
    rejected) upstream; we must not silently change its semantics here.
    """
    # Strip leading ./ only; never modify ../ paths.
    if rel.startswith("./"):
        return rel[2:]
    return rel


def _hydrate_sources(
    required_context: Sequence[str], worktree: Path, budget_bytes: int
) -> list[dict[str, Any]]:
    """Legacy prefix accounting retained for historical tooling only.

    Production hydrate events MUST NOT use this helper. It does not describe
    the selected native pack.

    Normalises each path and de-duplicates: the *first* occurrence of a
    canonical path is processed normally; subsequent occurrences get
    ``included=False, reason="deduplicated", duplicate_of=<first_path>``.
    """
    sources: list[dict[str, Any]] = []
    remaining = budget_bytes
    seen: dict[str, str] = {}  # canonical path -> original path of first occurrence
    for rel in required_context:
        canonical = _normalize_context_path(rel)
        entry: dict[str, Any] = {"path": str(rel)}
        if canonical in seen:
            entry["bytes"] = None
            entry["included"] = False
            entry["truncated_at"] = None
            entry["reason"] = "deduplicated"
            entry["duplicate_of"] = seen[canonical]
            sources.append(entry)
            continue
        seen[canonical] = str(rel)
        path = worktree / canonical
        try:
            size = path.stat().st_size
        except OSError:
            entry["bytes"] = 0
            entry["included"] = False
            entry["truncated_at"] = None
            entry["reason"] = "unreadable"
            sources.append(entry)
            continue
        entry["bytes"] = size
        if remaining <= 0:
            entry["included"] = False
            entry["truncated_at"] = None
            entry["reason"] = "budget_exhausted"
        elif size > remaining:
            entry["included"] = True
            entry["truncated_at"] = remaining
            entry["reason"] = "truncated"
        else:
            entry["included"] = True
            entry["truncated_at"] = None
            entry["reason"] = None
        remaining -= min(size, max(remaining, 0))
        sources.append(entry)
    return sources
