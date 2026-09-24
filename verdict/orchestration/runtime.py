"""BOD-153 bounded DAG runtime: concurrent dispatch, same-node reassignment, barriers.

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
import shutil
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from verdict.orchestration.contracts import (
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
    route_family,
    route_provider,
)


class EventSink(Protocol):
    def emit(self, type: str, node_id: str = "", **data: Any) -> Any: ...


Runner = Callable[[Sequence[str], Path, float], Awaitable[tuple[int, str]]]


async def subprocess_runner(argv: Sequence[str], cwd: Path, timeout: float) -> tuple[int, str]:
    """Run argv without a shell; kill the process group on timeout."""
    process = await asyncio.create_subprocess_exec(
        *argv,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        start_new_session=True,
    )
    try:
        out, _ = await asyncio.wait_for(process.communicate(), timeout)
    except asyncio.TimeoutError:
        import contextlib
        import os
        import signal

        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        await process.wait()
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


@dataclass
class NodeRun:
    node: WorkNode
    state: NodeState = NodeState.PLANNED
    attempt: int = 0
    route_id: str = ""
    history: list[dict[str, Any]] = field(default_factory=list)
    commit: str = ""
    reason: str = ""


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

    async def call(self, *args: str, cwd: Path | None = None, timeout: float = 120) -> str:
        code, out = await self.run(("git", *args), cwd or self.repo, timeout)
        if code != 0:
            raise OrchestrationError(f"git {' '.join(args)} failed ({code}): {out[-600:]}")
        return out.strip()

    async def head(self, cwd: Path | None = None) -> str:
        return await self.call("rev-parse", "HEAD", cwd=cwd)

    async def add_worktree(self, path: Path, branch: str, base: str) -> None:
        if path.exists():
            await self.remove_worktree(path)
        await self.call("worktree", "add", "-f", "-B", branch, str(path), base)

    async def remove_worktree(self, path: Path) -> None:
        code, _ = await self.run(("git", "worktree", "remove", "--force", str(path)), self.repo, 60)
        if code != 0 and path.exists():
            shutil.rmtree(path, ignore_errors=True)
            await self.run(("git", "worktree", "prune"), self.repo, 60)

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
                self._propagate_blocks()
                for node_id in self._ready():
                    if node_id not in pending:
                        pending[node_id] = asyncio.create_task(self._drive(node_id))
                if not pending:
                    break
                done, _ = await asyncio.wait(pending.values(), return_when=asyncio.FIRST_COMPLETED)
                for node_id, task in list(pending.items()):
                    if task in done:
                        del pending[node_id]
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
        return await self._conclude()

    def _ready(self) -> list[str]:
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
        failures: list[FailureClassification] = []
        tried: set[str] = set()
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
            if choice is None:
                run.reason = "no eligible model: " + _explain_exhaustion(considered)
                self.events.emit("eligibility", node_id, **counts, selected=None)
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
            previous = run.route_id
            run.attempt += 1
            run.route_id = choice.route_id
            self.events.emit(
                "eligibility",
                node_id,
                **counts,
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
            if ok:
                return
            if failures and failures[-1].action == "RETRY_INFRA":
                # Gateway-local transient: same route is still healthy; wait and retry.
                await asyncio.sleep(min(failures[-1].cooldown_seconds, 60.0))
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
                code, out = await self.runner(
                    node.verification_command, worktree, self.policy.verify_timeout_seconds
                )
                ok = code == 0
                self.events.emit(
                    "verify",
                    node.node_id,
                    ok=ok,
                    exit_code=code,
                    command=" ".join(node.verification_command),
                    tail=out[-600:],
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

    async def _attempt(self, run: NodeRun, failures: list[FailureClassification]) -> bool:
        node = run.node
        worktree = self.run_dir / "worktrees" / f"{node.node_id}-a{run.attempt}"
        branch = f"verdict/run-{self.run_dir.name}/{node.node_id}-a{run.attempt}"
        async with self._slots:
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
                prompt = self.prompt_for(node, worktree)
                terminal = await self._execute(prompt, run.route_id, worktree)
            finally:
                self.inflight.pop(node.node_id, None)
            self.events.emit(
                "terminal",
                node.node_id,
                ok=terminal.ok,
                route_id=run.route_id,
                reported_model=terminal.model,
                duration_seconds=round(terminal.duration_seconds, 2),
                session_ref=terminal.session_ref,
                stop_reason=terminal.stop_reason,
                attempt=run.attempt,
                fault_injected=terminal.session_ref.startswith("fault-injected"),
            )
            self._save_attempt(node.node_id, run.attempt, run.route_id, terminal)
            if terminal.ok and terminal.output.strip().upper().startswith("RESULT: BLOCKED"):
                terminal = WorkerTerminal(
                    ok=False,
                    output=terminal.output,
                    model=terminal.model,
                    error="worker_blocked: " + terminal.output.strip()[:200],
                    session_ref=terminal.session_ref,
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
            record = {
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
            fault_injected=terminal.session_ref.startswith("fault-injected"),
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
                "fault_injected": terminal.session_ref.startswith("fault-injected"),
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
        if node.verification_command:
            code, out = await self.runner(
                node.verification_command, worktree, self.policy.verify_timeout_seconds
            )
            self.events.emit(
                "verify",
                node.node_id,
                ok=code == 0,
                exit_code=code,
                command=" ".join(node.verification_command),
                tail=out[-600:],
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
            implementers = frozenset(r.route_id for r in self.nodes.values() if r.route_id)
            families = frozenset(route_family(r) for r in implementers)
            try:
                review = await self.reviewer.review(
                    repo=self.repo,
                    base_ref=self._base_sha,
                    head_ref=integration,
                    background=self.graph.goal,
                    exclude_routes=implementers,
                    exclude_families=frozenset() if len(families) > 3 else families,
                )
            except Exception as exc:  # fail closed
                review = ReviewResult(
                    status="ERROR",
                    reviewer="unknown",
                    route_id="",
                    detail=f"{type(exc).__name__}: {exc}",
                )
            self.events.emit(
                "review",
                status=review.status,
                reviewer=review.reviewer,
                route_id=review.route_id,
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
