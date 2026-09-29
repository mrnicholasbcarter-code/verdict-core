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
import inspect
import shutil
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from verdict.orchestration.candidate_builder import (
    MAX_CANDIDATES as _MAX_CANDIDATES,  # noqa: F401 re-exported; tests import from runtime
)
from verdict.orchestration.candidate_builder import build_candidates as _build_candidates
from verdict.orchestration.candidate_builder import build_rejections as _build_rejections
from verdict.orchestration.contracts import (
    TRANSITIONS,
    EligibilityStage,
    FailureClassification,
    FailureClassifier,
    ModelSelector,
    NodeKind,
    NodeState,
    OrchestrationError,
    Reviewer,
    ReviewResult,
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
from verdict.orchestration.controls import ControlReader, ControlRequest
from verdict.orchestration.recovery import RecoveryBudget
from verdict.subagent_selection import CONTEXT_LENGTH_CATEGORY

# Indirection for testing
_sleep = asyncio.sleep


class EventSink(Protocol):
    def emit(self, type: str, node_id: str = "", **data: Any) -> Any: ...


Runner = Callable[[Sequence[str], Path, float], Awaitable[tuple[int, str]]]


def _resolve_verify_argv(argv: Sequence[str]) -> tuple[list[str], str]:
    """Resolve interpreter for verification commands; return (resolved_argv, resolved_argv0).

    When ``argv[0]`` is ``"python"`` or ``"python3"`` and that name is not
    found on ``PATH``, substitute ``sys.executable`` (the interpreter running
    Verdict itself).  Any other ``argv[0]`` is returned unchanged.

    Returns ``(resolved_argv, resolved_argv0)`` where *resolved_argv0* is the
    original value when no substitution was made, or the resolved path when it
    was.  The caller should record *resolved_argv0* in the verify event so the
    substitution is visible in the run log.
    """
    import shutil
    import sys

    resolved: list[str] = list(argv)
    resolved_argv0 = ""
    if argv and argv[0] in {"python", "python3"} and shutil.which(argv[0]) is None:
        resolved[0] = sys.executable
        resolved_argv0 = sys.executable
    return resolved, resolved_argv0


async def subprocess_runner(argv: Sequence[str], cwd: Path, timeout: float) -> tuple[int, str]:
    """Run argv without a shell; kill the process group on timeout."""
    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )
    except (FileNotFoundError, PermissionError) as exc:
        return 127, f"verification command not found: {argv[0]}: {exc}"
    try:
        out, _ = await asyncio.wait_for(process.communicate(), timeout)
    except (asyncio.TimeoutError, asyncio.CancelledError) as exc:
        import contextlib
        import os
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
    max_cooldown_wait_seconds: float = 120.0
    # BOD-272: REQUIRED_CONTEXT byte budget for a node's worker prompt. After a
    # context-length overflow the next attempt gets a smaller pack (halved, not
    # below the floor) instead of a blind replay of the oversized request.
    context_budget_bytes: int = 60_000
    min_context_budget_bytes: int = 4_000


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
    # BOD-272: verification evidence from the last failed attempt, appended to
    # the next prompt so the same route can fix it before escalating.
    failure_feedback: str = ""
    rehydrated_routes: set[str] = field(default_factory=set)


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
        prompt_for: Callable[[WorkNode, Path], str],
        reviewer: Reviewer | None = None,
        policy: RuntimePolicy = RuntimePolicy(),
        runner: Runner = subprocess_runner,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        base_ref: str = "HEAD",
        inflight: dict[str, str] | None = None,
    ) -> None:
        self.repo, self.run_dir, self.graph = repo, run_dir, graph
        self.selector, self.executor, self.classifier = selector, executor, classifier
        self.events, self.prompt_for, self.reviewer = events, prompt_for, reviewer
        self.policy, self.runner, self.now = policy, runner, now
        self.base_ref = base_ref
        self.git = Git(repo, runner)
        self.nodes = {n.node_id: NodeRun(n) for n in graph.nodes}
        # node_id -> route_id. Shared with the selector's load callback so concurrent
        # selections spread across routes; reserved at SELECTION time, not dispatch.
        self.inflight: dict[str, str] = inflight if inflight is not None else {}
        self._slots = asyncio.Semaphore(min(policy.max_parallel, graph.max_parallel))
        self._base_sha = ""
        self._integration = run_dir / "worktrees" / "_integration"
        self._capacity: dict[str, str] = {}
        # Controls -------------------------------------------------------
        self._controls = ControlReader(run_dir)
        self._cancel_requested = False
        self._processed_controls: set[str] = set()
        self._recovery_budget = RecoveryBudget(max_attempts_per_node=policy.max_attempts_per_node)

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
            requirements = TaskRequirements.for_node(run.node, exclude_routes=frozenset(tried))
            choice, considered = self.selector.select(requirements, now=self.now())
            counts = _ladder_counts(considered)
            # Post-probe statistics for eligibility events (BOD-203).
            _post: dict[str, int] = getattr(self.selector, "last_select_stats", {})
            if _post:
                counts = {**counts, **_post}
            # BOD-277: per-route eligibility verdicts for trace drill-down.
            _rejections = _build_rejections(considered)
            _selected_route = choice.route_id if choice is not None else None
            _cands, _cands_omitted = _build_candidates(considered, _selected_route)
            _evidence: dict[str, Any] = {
                "rejections": _rejections,
                "candidates": _cands,
                "candidates_omitted": _cands_omitted,
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
            previous = run.route_id
            run.attempt += 1
            run.route_id = choice.route_id
            self.events.emit(
                "eligibility",
                node_id,
                **counts,
                **_evidence,
                selected=choice.route_id,
                capacity_class=choice.capacity_class.value,
                rank=choice.rank,
            )
            self.events.emit(
                "selection",
                node_id,
                route_id=choice.route_id,
                provider=choice.provider,
                capacity_class=choice.capacity_class.value,
                plan=choice.plan_label,
                rank=choice.rank,
                attempt=run.attempt,
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
                and failures[-1].category == "verification_failed"
                and run.route_id not in run.rehydrated_routes
            ):
                # BOD-272: failure-directed rehydration. The route produced a
                # candidate that failed verification; give the SAME route one
                # retry with the exact failing evidence before escalating to
                # another (usually more expensive) route.
                run.rehydrated_routes.add(run.route_id)
                run.failure_feedback = failures[-1].evidence[-2000:]
                self.events.emit(
                    "rehydrate",
                    node_id,
                    reason="verification_failed",
                    route_id=run.route_id,
                    evidence_chars=len(run.failure_feedback),
                    attempt=run.attempt,
                )
            else:
                tried.add(run.route_id)
            if failures and failures[-1].action == "BLOCK":
                run.reason = f"non-recoverable: {failures[-1].category}"
                self._set(run, NodeState.BLOCKED, reason=run.reason)
                return
            self._set(run, NodeState.PLANNED, reassign=True)

    async def _integrate_node(self, run: NodeRun) -> None:
        """Mechanical integration barrier: merge validated dependencies, run the combined check."""
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
                    command=" ".join(node.verification_command),
                    tail=out[-600:],
                    **verify_extra,
                )
            self.events.emit(
                "barrier",
                node.node_id,
                name=node.barrier or "integration",
                ok=ok,
                detail="combined verification " + ("passed" if ok else "failed"),
            )
        finally:
            await self.git.remove_worktree(worktree)
        if not ok:
            run.reason = "integration verification failed"
            self._set(run, NodeState.REJECTED, reason=run.reason)
            self._set(run, NodeState.BLOCKED, reason=run.reason)
            return
        run.commit = base
        run.history.append(
            {"attempt": run.attempt, "route_id": "", "outcome": "validated", "commit": base}
        )
        self._set(run, NodeState.VALIDATED, commit=base)

    def _hydrator_takes_budget(self) -> bool:
        try:
            return "context_budget_bytes" in inspect.signature(self.prompt_for).parameters
        except (TypeError, ValueError):
            return False

    def _prompt(self, node: WorkNode, worktree: Path, budget: int) -> str:
        """Hydrate the node prompt; pass the byte budget when the hydrator takes one."""
        if self._hydrator_takes_budget():
            hydrate: Any = self.prompt_for
            return str(hydrate(node, worktree, context_budget_bytes=budget))
        return self.prompt_for(node, worktree)

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
                prompt = self._prompt(node, worktree, budget)
                if run.failure_feedback:
                    prompt += (
                        "\n\nPREVIOUS_ATTEMPT_FAILED_VERIFICATION:\n"
                        + run.failure_feedback
                        + "\nFix the cause above, then run VERIFICATION_COMMAND again.\n"
                    )
                prompt_bytes = len(prompt.encode())
                self.events.emit(
                    "hydrate",
                    node.node_id,
                    context_files=list(node.required_context),
                    prompt_bytes=prompt_bytes,
                    truncated="[truncated" in prompt.lower(),
                    budget_bytes=budget,
                    sources=_hydrate_sources(node.required_context, worktree, budget),
                    compression="not_performed",
                )
                terminal = await self._execute(prompt, run.route_id, worktree)
            finally:
                self.inflight.pop(node.node_id, None)
            self.events.emit(
                "terminal",
                node.node_id,
                ok=terminal.ok,
                route_id=run.route_id,
                reported_model=terminal.model,
                # Empty is explicitly unknown for custom adapters; never legacy inference.
                executor_kind=terminal.executor_kind,
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
            if terminal.ok and terminal.output.strip().upper().startswith("RESULT: BLOCKED"):
                terminal = WorkerTerminal(
                    ok=False,
                    output=terminal.output,
                    model=terminal.model,
                    error="worker_blocked: " + terminal.output.strip()[:200],
                    session_ref=terminal.session_ref,
                    executor_kind=terminal.executor_kind,
                )
            if not terminal.ok or not terminal.output.strip():
                return await self._fail(run, terminal, failures, worktree)
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

    def _save_attempt(
        self, node_id: str, attempt: int, route_id: str, terminal: WorkerTerminal
    ) -> None:
        """Durable per-attempt evidence (sanitized by the executor); never fatal."""
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

    async def _execute(self, prompt: str, route_id: str, cwd: Path) -> WorkerTerminal:
        try:
            return await asyncio.wait_for(
                self.executor.run(
                    prompt,
                    route_id=route_id,
                    cwd=cwd,
                    timeout_seconds=self.policy.attempt_timeout_seconds,
                ),
                self.policy.attempt_timeout_seconds + 30,
            )
        except asyncio.TimeoutError:
            return WorkerTerminal(ok=False, model=route_id, error="timeout (runtime watchdog)")
        except Exception as exc:  # executor bugs must not unwind the controller
            return WorkerTerminal(
                ok=False, model=route_id, error=f"transport: {type(exc).__name__}: {exc}"
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
        failure = self.classifier.classify(terminal, now=self.now())
        failures.append(failure)
        if validated:
            self._set(run, NodeState.REJECTED, reason=failure.category)
        else:
            self._set(run, NodeState.TERMINAL_FAILURE, reason=failure.category)
        self.events.emit(
            "failure",
            run.node.node_id,
            category=failure.category,
            action=failure.action,
            route_id=run.route_id,
            evidence=failure.evidence[:300],
            fault_injected=terminal.executor_kind == "fault-injected",
            attempt=run.attempt,
        )
        if failure.scope != "none" and failure.cooldown_seconds > 0:
            self.selector.record_failure(run.route_id, failure, now=self.now())
            key = route_provider(run.route_id) if failure.scope == "provider" else run.route_id
            until = datetime.fromtimestamp(
                self.now().timestamp() + failure.cooldown_seconds, timezone.utc
            )
            self.events.emit(
                "cooldown",
                run.node.node_id,
                key=key,
                scope=failure.scope,
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
                command=" ".join(node.verification_command),
                tail=out[-600:],
                **verify_extra,
            )
            if code != 0:
                return f"verification_failed: exit {code}: {out[-300:]}"
        else:
            self.events.emit(
                "verify", node.node_id, ok=True, exit_code=0, command="(none: non-code node)"
            )
        return None

    # ------------------------------------------------------------------ conclude
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
            # this run. Prefer a different model family too; if no other family has
            # capacity, fall back to route-level independence (recorded as such).
            implementers = frozenset(r.route_id for r in self.nodes.values() if r.route_id)
            families = frozenset(route_family(r) for r in implementers)
            review = None
            for level, excluded_families in (("family", families), ("route", frozenset())):
                try:
                    candidate = await self.reviewer.review(
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
    """Build per-source entries from the same data hydrate_node_prompt uses.

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
