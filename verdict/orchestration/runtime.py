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
from typing import Any, Literal, Protocol, cast

from verdict.context_pack import ContextPlan, estimate_tokens
from verdict.free_tier_admit import REASON_PROVIDER_UNHEALTHY
from verdict.models import AdmissionProof
from verdict.orchestration.candidate_builder import (
    MAX_CANDIDATES as _MAX_CANDIDATES,  # noqa: F401 re-exported; tests import from runtime
)
from verdict.orchestration.candidate_builder import build_candidates as _build_candidates
from verdict.orchestration.candidate_builder import build_rejections as _build_rejections
from verdict.orchestration.contracts import (
    EFFORT_OMITTED,
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
    dispatch_origin_problem,
    executor_gateway_origin,
    release_executor_origin_pin,
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
from verdict.orchestration.effort import (
    allowlisted_effort_facts,
    choose_effort,
    effort_reason,
    task_kind_for_node,
)
from verdict.orchestration.explain import missing_proof_hint
from verdict.orchestration.health_cache import (
    PERMIT_PROOF_MAX_VALIDITY_SECONDS,
    DispatchPermit,
    DispatchScope,
    HealthCache,
    default_cache_path,
)
from verdict.orchestration.opencode_denial import PERMANENT_DENIAL_CODES
from verdict.orchestration.recovery import RecoveryBudget
from verdict.orchestration.repo_gates import RepoGate, describe_gates, discover_repo_gates
from verdict.orchestration.research_metrics import (
    measure_research_duplication,
    normalize_research_fetch,
)
from verdict.orchestration.salvage import (
    _MAX_EVIDENCE_PATHS,
    clear_salvage_dir,
    restore_salvaged_files,
    salvage_owned_files,
)
from verdict.orchestration.verification import (
    GATE_UNAVAILABLE_EXIT_CODES,
    VERIFY_OUTPUT_TAIL_CHARS,
    UnresolvedImports,
    describe_environment_fault,
    environment_fault_refusal,
    unresolved_third_party_imports,
)
from verdict.orchestration.verification import (
    classify_verify_env_error as _classify_verify_env_error,
)
from verdict.orchestration.verification import resolve_gate_argv as _resolve_gate_argv
from verdict.orchestration.verification import resolve_verify_argv as _resolve_verify_argv
from verdict.orchestration.worker_context import (
    ResearchDelta,
    ResearchPack,
    WorkerPrompt,
    create_research_pack,
    reuse_research_pack,
)
from verdict.relay import admission_proof, dispatch_scope
from verdict.security import redact_text
from verdict.subagent_selection import CONTEXT_LENGTH_CATEGORY

# Serve-denial recovery: run-scoped cooldowns, in seconds (never persisted).
_DENIAL_PREFIX_COOLDOWN_S = 300.0
_DENIAL_ROUTE_COOLDOWN_S = 60.0

# Indirection for testing
_sleep = asyncio.sleep
# BOD-433: failures that revoke the permit scope (credential denial, wrong model served).
_REVOKING_CATEGORIES = frozenset({"authentication", "permission", "model_mismatch"})
# fix(retry-clean-base): categories that mean "work was judged wrong" but arrive as
# TERMINAL_FAILURE (validated=False).  Together with validated=True these form the full
# set of "rejected" outcomes that must start the next attempt from a clean base.
_JUDGED_WORK_CATEGORIES = frozenset({"diff_rejected"})
# Capability escalation: categories that count as judged failures for the escalation
# counter.  Extends _JUDGED_WORK_CATEGORIES with verification_failed (can arrive via
# either validated=True or validated=False).  repo_gate_failed is wrapped into
# verification_failed by _run_repo_gates() / _verify_node() before reaching here.
_ESCALATION_JUDGED_CATEGORIES = _JUDGED_WORK_CATEGORIES | frozenset({"verification_failed"})
# After this many judged failures on the same node, the next attempt must use a route
# with a strictly stronger capability tier than the one that last failed.
_ESCALATION_JUDGED_FAILURE_THRESHOLD = 2
# Ceiling used when the failing route's capability_tier is unknown (None).  Setting
# this to 3 (the weakest known tier) means "require a route with a KNOWN tier at or
# below 3"; the eligibility check then excludes unknown-tier routes when escalation
# is active, so the escalation path prefers any route with measured capability.
_ESCALATION_UNKNOWN_TIER_CEILING = 3
_WORKER_OUTPUT_RESERVE = 1024
_WORKER_SAFETY_RESERVE = 512
# File extensions that ruff treats as Python source.  Explicitly named files
# with any other extension (YAML, Markdown, TOML, …) are reformatted as Python
# syntax, corrupting their content.  Only these extensions are passed to
# ruff check --fix / ruff format.
_RUFF_PYTHON_EXTENSIONS: frozenset[str] = frozenset((".py", ".pyi"))


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
    return process.returncode or 0, out.decode("utf-8", "replace")[-VERIFY_OUTPUT_TAIL_CHARS:]


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
    # Capacity classes allowed for tier-0 escalation.  When a tier-1 route
    # accumulates K judged failures, the runtime attempts to escalate to tier-0
    # but restricts selection to these classes so that METERED (pay-per-token)
    # and UNKNOWN-capacity tier-0 routes are never selected through escalation.
    # Subscription and free tier-0 routes (e.g. Claude Max / Opus cc/) are safe.
    escalation_tier0_capacity_classes: frozenset[CapacityClass] = frozenset(
        {CapacityClass.SUBSCRIPTION, CapacityClass.FREE}
    )


@dataclass(frozen=True)
class DispatchAuthority:
    """Where DagRuntime gets route proofs for BOD-433 dispatch permits.

    ``gateway_origin`` is the gateway the run's workers are sent through.
    ``worker_serve_origin`` is the ``verdict serve`` origin of the workers that send through
    serve instead (the OpenCode executor); an executor that reports a
    :class:`~verdict.orchestration.contracts.WorkerServeOrigin` is checked against it and
    never against ``gateway_origin``, and None binds none, so such an executor is refused.
    ``confirm(route_ids, now)`` returns a read-only passport confirmation in the
    shape ``relay.admission_proof`` consumes.

    ``record_gateway_origin`` False keeps the origin out of the run's evidence:
    ``dispatch_permit`` events then carry :data:`REDACTED_GATEWAY_ORIGIN`. The
    permit check itself always uses ``gateway_origin``.
    """

    gateway_origin: str
    confirm: Callable[[Sequence[str], datetime], dict[str, Any]]
    record_gateway_origin: bool = True
    worker_serve_origin: str | None = None


#: Recorded in place of the gateway origin when the run asks for redaction.
#: A plain marker, not a hash: a likely hostname could be guessed and hashed.
REDACTED_GATEWAY_ORIGIN = "redacted"
#: Set to ``1`` to keep the gateway origin out of an orchestrate run's evidence
#: (the certification rehearsal sets it: its gateway address is a secret).
REDACT_GATEWAY_ORIGIN_ENV = "VERDICT_REDACT_GATEWAY_ORIGIN"


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
    # Per-attempt chosen effort. Reset on every reselection so a replaced
    # route cannot keep the previous attempt's override.
    chosen_effort: str | None = None
    effort_reason: dict[str, Any] = field(default_factory=dict)
    effort_route_id: str = ""
    effort_attempt: int = 0
    # fix(retry-clean-base): True when the most recently completed attempt ended
    # with its work JUDGED WRONG (validated=True → REJECTED, or category in
    # _JUDGED_WORK_CATEGORIES).  Read in _attempt() to decide clean-base vs
    # salvage-restore; overwritten by every _record_failure() call.
    last_rejected: bool = False
    # True when clear_salvage_dir failed on a rejected attempt, leaving stale
    # content in the salvage dir.  Forces clean-base on all future attempts of
    # this node until an interrupted save successfully replaces the dir.
    salvage_poisoned: bool = False
    # Capability escalation fields.
    # Number of judged work failures (categories in _ESCALATION_JUDGED_CATEGORIES,
    # or validated=True) accumulated across all attempts of this node.
    judged_failure_count: int = 0
    # Capability tier of the most recently dispatched route (from RouteVerdict.
    # capability_tier).  None when the tier is unknown.  Set each time a route
    # is selected; read by _record_failure() to tighten the escalation ceiling.
    last_selected_tier: int | None = None
    # Monotone escalation ceiling: when judged_failure_count >=
    # _ESCALATION_JUDGED_FAILURE_THRESHOLD this is set to (last_selected_tier - 1)
    # and only decreases from there.  None = no ceiling active.
    escalation_tier_ceiling: int | None = None
    # Capacity class restriction active for the current escalation ceiling.
    # Set when escalation_tier_ceiling == 0 (tier-0 escalation).
    # None = no restriction active (default).
    # frozenset() (empty) = restriction active but nothing allowed →
    #   escalation produces no candidates, _drive emits 'escalation_exhausted'.
    escalation_capacity_classes: frozenset[CapacityClass] | None = None


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
    ".serena/",
    "venv/",
    ".venv/",
)


def _ensure_local_excludes(cwd: Path) -> None:
    """Append byproduct patterns to the worktree's info/exclude (untracked, local only).

    This protects every ``git add -A`` and merge in the worktree (node commits,
    integration), not just ``changed_files`` staging. Owned paths under an
    excluded prefix are force-added separately in ``Git.changed_files``.
    """
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


@dataclass(frozen=True)
class OwnedIgnoredReport:
    """Owned git-ignored files force-staged, and those refused with a reason."""

    forced: tuple[str, ...] = ()
    refused: tuple[tuple[str, str], ...] = ()


class Git:
    """Minimal git worktree operations for node isolation and integration."""

    def __init__(self, repo: Path, runner: Runner = subprocess_runner) -> None:
        self.repo = repo
        self.run = runner
        self._worktree_lock = asyncio.Lock()
        self.owned_ignored: dict[str, OwnedIgnoredReport] = {}

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

    async def changed_files(
        self, cwd: Path, base: str, *, owned_files: Sequence[str] = ()
    ) -> list[str]:
        """``git diff`` of the deliverable, excluding tool byproducts.

        Directory byproducts (``venv/``, ``.serena/``, ``__pycache__/``, ...)
        are never staged UNLESS a path under one of those prefixes is itself in
        ``owned_files`` — a node that legitimately owns a path like
        ``venv/src/module.py`` must still see it as its deliverable (including
        a deletion of it), not have it silently hidden by the exclude.

        Owned paths are staged as literal pathspecs (``:(literal)<path>``) so a
        path containing git pathspec magic characters (e.g. a filename starting
        with ``:(``) is matched exactly rather than reinterpreted.

        Finally, ignored untracked regular files under the owned paths are
        force-added so ``.gitignore`` cannot drop a deliverable; byproducts
        are excluded in the listing itself, and byproducts, symlinks and files
        with credential-shaped content are never added.
        """
        _ensure_local_excludes(cwd)
        dir_excludes = [p for p in BYPRODUCT_EXCLUDES if p.endswith("/")]
        await self.call("add", "-A", cwd=cwd)
        owned_under_byproduct = [
            p for p in owned_files if any(p.startswith(prefix) for prefix in dir_excludes)
        ]
        for p in owned_under_byproduct:
            spec = f":(literal){p}"
            # Skip only when the path is neither tracked by git nor present on
            # disk: nothing for THIS node to stage, not an error. Anything else
            # (index lock, permission, ...) must raise, not disappear a real
            # deliverable.
            on_disk = (cwd / p).exists() or (cwd / p).is_symlink()
            if not on_disk:
                code, _ = await self.run(
                    ("git", "ls-files", "--error-unmatch", "--", spec), cwd, 30
                )
                if code != 0:
                    continue
            # -A also stages a deletion of an already-tracked owned path even
            # when it no longer exists on disk.
            await self.call("add", "-A", "-f", "--", spec, cwd=cwd)
        forced: list[str] = []
        refused: list[tuple[str, str]] = []
        if owned_files:
            code, listing = await self.run(
                (
                    "git",
                    "ls-files",
                    "-z",
                    "-o",
                    "-i",
                    "--exclude-standard",
                    "--",
                    *(f":(literal){p}" for p in owned_files),
                    *(
                        f":(exclude,glob)**/{p}**" if p.endswith("/") else f":(exclude,glob)**/{p}"
                        for p in BYPRODUCT_EXCLUDES
                    ),
                ),
                cwd,
                60,
            )
            if code != 0:
                raise RuntimeError(f"git ls-files for owned ignored files failed ({code})")
            if len(listing) >= 4000:
                raise RuntimeError(
                    "the ignored-file listing for the node's owned paths is too large to trust"
                )
            for ignored in (item for item in listing.split("\0") if item):
                full = cwd / ignored
                if any(
                    (
                        ignored.startswith(pattern) or f"/{pattern}" in ignored
                        if pattern.endswith("/")
                        else (
                            ignored.endswith(pattern[1:])
                            if pattern.startswith("*")
                            else ignored.rsplit("/", 1)[-1] == pattern
                        )
                    )
                    for pattern in BYPRODUCT_EXCLUDES
                ):
                    continue
                if full.is_symlink():
                    refused.append((ignored, "symlink"))
                    continue
                if not full.is_file():
                    refused.append((ignored, "not_a_regular_file"))
                    continue
                try:
                    content = full.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    refused.append((ignored, "unreadable"))
                    continue
                if redact_text(content) != content:
                    refused.append((ignored, "credential_shaped"))
                    continue
                await self.call("add", "-A", "-f", "--", f":(literal){ignored}", cwd=cwd)
                forced.append(ignored)
        self.owned_ignored[str(cwd)] = OwnedIgnoredReport(
            forced=tuple(forced), refused=tuple(refused)
        )
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


def _effort_rows(
    model_rows: Mapping[str, Mapping[str, Any]] | None,
) -> dict[str, dict[str, Any]] | None:
    """Copy only reasoning/thinkingLevelMap facts. None stays None (unavailable)."""
    if model_rows is None:
        return None
    return {
        str(route): allowlisted_effort_facts(row)
        for route, row in model_rows.items()
        if isinstance(row, Mapping)
    }


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
        model_rows: Mapping[str, Mapping[str, Any]] | None = None,
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
        # Allowlisted effort facts only. None means the inventory was not supplied.
        self.model_rows = _effort_rows(model_rows)
        self._run_started_monotonic: float | None = None
        self._slots = asyncio.Semaphore(min(policy.max_parallel, graph.max_parallel))
        self._base_sha = ""
        self._integration = run_dir / "worktrees" / "_integration"
        self._capacity: dict[str, str] = {}
        # Session ledger writes are advisory: the first failure latches this
        # guard so a broken ledger fails open once and never fails the run.
        self._session_evidence_error: str | None = None
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

    def _bind_effort(self, run: NodeRun, route_id: str) -> None:
        """Choose effort for this route and attempt. Replaces any previous bind."""
        task_kind = task_kind_for_node(run.node.kind)
        rows = self.model_rows
        if rows is None or route_id not in rows:
            facts: Mapping[str, Any] = {}
            source = "unavailable"
        else:
            facts = rows[route_id]
            source = "model_rows"
        chosen = choose_effort(task_kind, facts)
        reason = effort_reason(task_kind, chosen, facts)
        reason["metadata_source"] = source
        reason["route_id"] = route_id
        reason["attempt"] = run.attempt
        if source == "unavailable":
            reason["row_unavailable"] = True
        run.chosen_effort = chosen
        run.effort_reason = reason
        run.effort_route_id = route_id
        run.effort_attempt = run.attempt

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
        if run.node.kind is NodeKind.REVIEW:
            # Review is mechanical, so it has no model-selection event today.
            # Emit the one selection that binds this attempt's chosen effort.
            # Integrate stays event-identical: it is not a worker dispatch.
            run.attempt += 1
            self._bind_effort(run, "")
            self.events.emit(
                "selection",
                node_id,
                route_id="",
                provider="",
                attempt=run.attempt,
                chosen_effort=run.chosen_effort,
                effort_reason=run.effort_reason,
            )
            run.attempt -= 1
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
            # Capability escalation: if a tier ceiling is active (set after K judged
            # failures), try a stricter selection first.  If that leaves no eligible
            # route, fall back to the full admitted set so the budget stays bounded.
            _esc_ceiling = run.escalation_tier_ceiling
            if _esc_ceiling is not None:
                _esc_requirements = TaskRequirements.for_node(
                    run.node,
                    exclude_routes=frozenset(tried),
                    exclude_pools=frozenset(run.excluded_pools),
                    escalation_max_tier=_esc_ceiling,
                    escalation_capacity_classes=run.escalation_capacity_classes,
                )
                # BOD-341: run on a worker thread to avoid blocking the event loop.
                _esc_choice, _esc_considered, _esc_post = await asyncio.to_thread(
                    self._select_with_stats, _esc_requirements, self.now()
                )
            else:
                _esc_choice = None

            # Initialize so mypy knows choice/considered/_post are defined across
            # both the escalation and fallback branches.
            choice: RouteVerdict | None = None
            considered: tuple[RouteVerdict, ...] = ()
            _post: dict[str, int] = {}
            if _esc_choice is not None:
                # Escalation found a stronger route within the admitted set.
                _cap_data: dict[str, Any] = (
                    {
                        "capacity_classes_allowed": [
                            c.value for c in sorted(run.escalation_capacity_classes)
                        ]
                    }
                    if run.escalation_capacity_classes is not None
                    else {}
                )
                self.events.emit(
                    "escalation",
                    node_id,
                    from_route=run.route_id,
                    from_tier=run.last_selected_tier,
                    to_route=_esc_choice.route_id,
                    to_tier=_esc_choice.capability_tier,
                    judged_failure_count=run.judged_failure_count,
                    attempt=run.attempt + 1,
                    reason="judged_failures_threshold",
                    **_cap_data,
                )
                run.history.append(
                    {
                        "attempt": run.attempt + 1,
                        "route_id": _esc_choice.route_id,
                        "outcome": "escalation",
                        "from_route": run.route_id,
                        "from_tier": run.last_selected_tier,
                        "to_tier": _esc_choice.capability_tier,
                        "judged_failure_count": run.judged_failure_count,
                    }
                )
                choice, considered, _post = _esc_choice, _esc_considered, _esc_post
            else:
                # When the tier-0 escalation ceiling is active but the restricted
                # selection found no qualifying route (no subscription/free tier-0
                # candidate exists), emit 'escalation_exhausted' so the receipt
                # records the failed escalation attempt before the fallback runs.
                if _esc_ceiling == 0:
                    self.events.emit(
                        "escalation_exhausted",
                        node_id,
                        from_tier=run.last_selected_tier,
                        judged_failure_count=run.judged_failure_count,
                        reason="at_strongest_tier",
                        capacity_classes_allowed=[
                            c.value for c in sorted(run.escalation_capacity_classes or ())
                        ],
                    )
                # Normal selection (no escalation ceiling, or ceiling left no route).
                # BOD-341: selection reads the health cache/inventory and can take
                # seconds. Running it inline here would block the whole event loop
                # (dispatch, slot release, other nodes' driving) for that long, so
                # --max-parallel is never reached. Run it in a thread instead.
                requirements = TaskRequirements.for_node(
                    run.node,
                    exclude_routes=frozenset(tried),
                    exclude_pools=frozenset(run.excluded_pools),
                )
                choice, considered, _post = await asyncio.to_thread(
                    self._select_with_stats, requirements, self.now()
                )
            counts = _ladder_counts(considered)
            accounting = (_post or {}).pop("selection_accounting", None)
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
                "selection_accounting": accounting,
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
                hint = missing_proof_hint(considered)
                # Hint first: run summaries may truncate the long ladder detail.
                run.reason = (
                    "no eligible model: "
                    + (f"{hint}; " if hint else "")
                    + _explain_exhaustion(considered)
                )
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
            # Track the dispatched route's tier for capability escalation.
            run.last_selected_tier = choice.capability_tier
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
                for _pf in ("probe_class", "cache_checked_at", "cache_freshness", "evidence_level"):
                    if choice.rank_components.get(_pf) is not None:
                        _probe_fields[_pf] = str(choice.rank_components[_pf])
            # Record UNKNOWN capacity opt-in when an UNKNOWN route was selected.
            if choice.capacity_class == CapacityClass.UNKNOWN and getattr(
                self.selector, "_allow_unknown", False
            ):
                _probe_fields["unknown_capacity_opt_in"] = True
            self._bind_effort(run, choice.route_id)
            self.events.emit(
                "selection",
                node_id,
                route_id=choice.route_id,
                provider=choice.provider,
                capacity_class=choice.capacity_class.value,
                plan=choice.plan_label,
                rank=choice.rank,
                attempt=run.attempt,
                chosen_effort=run.chosen_effort,
                effort_reason=run.effort_reason,
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
                elif last.category == "diff_rejected":
                    # fix(retry-clean-base): pass the diff rejection reason as advisory
                    # context so the next worker knows what search/replace failed.
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
        classify_environment: bool = False,
    ) -> str | None:
        """Run every declared gate, retaining the first failure with bounded evidence.

        ``worker_fixable_only`` (implement-node validation) reports only real check
        failures: configuration errors and missing executables are left for the
        integration barrier, since the node's worker cannot fix them.

        ``classify_environment`` (the integration barrier, where no worker is left to
        correct anything) looks for a fault of the environment in a failed gate
        (:meth:`_gate_environment_fault`): a gate command that could not start, or unresolved
        third-party imports that the run's untouched base has too.  Such a failure is returned as
        ``environment_error: repo_gate_failed: ...``, but only when no gate failed for another
        reason: a gate that really failed makes the result a plain ``repo_gate_failed`` one, so
        the implementation is never reported as sound because another gate's environment was
        broken.
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
        environment_reason: str | None = None
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
            gate_environment: str | None = None
            ruled_out: str | None = None
            if classify_environment and code != 0:
                gate_environment, ruled_out = await self._gate_environment_fault(
                    gate, code, out, worktree
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
                **({"environment_error": gate_environment} if gate_environment else {}),
                **({"environment_ruled_out": ruled_out} if ruled_out else {}),
            )
            fixable = code not in GATE_UNAVAILABLE_EXIT_CODES or not worker_fixable_only
            if code != 0 and fixable:
                message = f"repo_gate_failed: {gate.name} exit {code}: {out[-600:]}"
                if gate_environment is not None:
                    environment_reason = environment_reason or (
                        f"environment_error: repo_gate_failed: {gate.name} exit {code}: "
                        f"{gate_environment}"
                    )
                else:
                    reason = reason or message
                if failed is not None:
                    failed.append(message)
        return reason or environment_reason

    async def _gate_environment_fault(
        self, gate: RepoGate, code: int, out: str, worktree: Path
    ) -> tuple[str | None, str | None]:
        """``(fault, ruled_out)`` for a repo gate that failed at the integration barrier.

        ``fault`` is the reason when the failure is the environment's, never the implementation's:

        * the gate command could not start (exit 126/127: ``resolve_gate_argv`` found no
          executable, or the program is missing or not executable): no code was judged;
        * mypy cannot resolve third-party packages that it cannot resolve on the run's untouched
          base either (:meth:`_base_gate_imports`).  An unresolved name that is not first-party
          is not proof alone: a typo or a deleted package is unresolved only here.

        ``ruled_out`` records why a fault that looked like the environment's was not accepted.
        """
        if code in GATE_UNAVAILABLE_EXIT_CODES:
            return f"the gate could not run: {out[-500:]}", None
        candidate = unresolved_third_party_imports(gate.argv, code, out, worktree)
        if candidate is None:
            return None, None
        # The base is run only now: its first run in a fresh worktree installs every extra.
        refusal = environment_fault_refusal(candidate, await self._base_gate_imports(gate))
        if refusal is not None:
            return None, refusal
        return describe_environment_fault(gate.argv, candidate), None

    async def _base_gate_imports(self, gate: RepoGate) -> UnresolvedImports | None:
        """What ``gate`` reports on the run's untouched base about unresolved third-party
        imports: no packages when it passes or fails for another reason there, and ``None``
        when the base could not be checked (no base, no worktree, the gate cannot run there)."""
        if not self._base_sha:
            return None
        tag = secrets.token_hex(4)
        scratch = self.run_dir / "worktrees" / f"integration-base-{tag}"
        branch = f"verdict/integration-base-{tag}"
        try:
            try:
                await self.git.add_worktree(scratch, branch, self._base_sha)
            except OrchestrationError:
                return None
            for base_gate in discover_repo_gates(scratch):
                if base_gate.name == gate.name:
                    resolved = _resolve_gate_argv(base_gate.argv, scratch)
                    code, out = await self.runner(
                        resolved, scratch, self.policy.verify_timeout_seconds
                    )
                    return unresolved_third_party_imports(
                        base_gate.argv, code, out, scratch
                    ) or UnresolvedImports((), None, True)
            return None
        except (FileNotFoundError, ValueError):
            return None
        finally:
            await self.git.remove_worktree(scratch)
            with contextlib.suppress(OrchestrationError):
                await self.git.call("branch", "-D", branch)

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
                reason = await self._run_repo_gates(run, worktree, classify_environment=True) or ""
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
            if reason.startswith("environment_error:"):
                # A gate that could not judge the code (its environment lacks the project's
                # dependencies): the fail-closed environment path, never a wrong implementation
                # (no worker is left to correct anything here, and none could fix this).
                terminal = WorkerTerminal(ok=False, error=reason)
                failure = FailureClassification("environment_error", "BLOCK", 0, "none", reason)
            else:
                terminal = WorkerTerminal(ok=False, error="verification_failed: " + reason)
                # Gate diagnostics describe verification, never worker transport faults.
                failure = FailureClassification(
                    "verification_failed",
                    "CORRECT_IMPLEMENTATION",
                    0,
                    "none",
                    "verification_failed: " + reason,
                )
            self._record_failure(run, terminal, run.failures, validated=True, failure=failure)
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
            # fix(retry-clean-base): after a REJECTED attempt (work was judged wrong)
            # start from the clean base instead of restoring stale partial files.
            # Only INTERRUPTED attempts (timeout, transport) have their saved state
            # restored.  The restore event always records which path was taken.
            restored_paths: list[str] = []
            if run.attempt > 1 and run.node.owned_files:
                salvage_dir = self.run_dir / "salvage" / run.node.node_id
                was_rejected = run.last_rejected
                salvage_tainted = run.salvage_poisoned
                # Reset last_rejected; salvage_poisoned is managed by _fail().
                run.last_rejected = False
                if was_rejected or salvage_tainted:
                    # Previous attempt was rejected: start from the clean base.
                    # No files are restored; the event records the reason.
                    self.events.emit(
                        "restore",
                        node.node_id,
                        restored=[],
                        restored_count=0,
                        skipped=[],
                        total_bytes=0,
                        attempt=run.attempt,
                        evidence_truncated=False,
                        error="",
                        reason="clean_base",
                    )
                    run.history.append(
                        {
                            "attempt": run.attempt,
                            "route_id": run.route_id,
                            "outcome": "restore",
                            "restored": [],
                            "restored_count": 0,
                            "skipped": [],
                            "total_bytes": 0,
                            "evidence_truncated": False,
                            "error": "",
                            "reason": "clean_base",
                        }
                    )
                else:
                    # Previous attempt was interrupted: restore partial owned files.
                    restore = restore_salvaged_files(salvage_dir, worktree, run.node.owned_files)
                    restored_paths = list(restore.restored[:_MAX_EVIDENCE_PATHS])
                    if restore.restored or restore.error:
                        restore_reason = (
                            "verification_failed"
                            if run.failures and run.failures[-1].category == "verification_failed"
                            else "interrupted"
                        )
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
                            reason=restore_reason,
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
                                "reason": restore_reason,
                            }
                        )
                        if restore.restored:
                            # Files were written into the worktree: invalidate the
                            # cached research pack so the next attempt re-reads the
                            # restored state rather than the base-revision snapshot.
                            pack_path = self.run_dir / "context_artifacts" / f"{node.node_id}.json"
                            with contextlib.suppress(OSError):
                                pack_path.unlink(missing_ok=True)
            self.inflight[node.node_id] = run.route_id
            origin_pinned = False
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
                    capability_tier=run.last_selected_tier,
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
                    if restored_paths and run.failures[-1].category == "verification_failed":
                        suffix += (
                            "Your previous attempt's files are already in this worktree "
                            f"({', '.join(restored_paths)}): continue from them and fix what "
                            "failed; do not start again from nothing.\n"
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
                origin = executor_gateway_origin(
                    self.executor, node.node_id, route_id=run.route_id, cwd=worktree
                )
                # Until the send starts, a denial must free what the check pinned.
                origin_pinned = True
                origin_problem = (
                    None
                    if self.dispatch_authority is None
                    else dispatch_origin_problem(
                        origin,
                        gateway_origin=self.dispatch_authority.gateway_origin,
                        worker_serve_origin=self.dispatch_authority.worker_serve_origin,
                    )
                )
                if origin_problem is not None:
                    # An unknown origin, or another one than the run binds:
                    # no route change fixes it.
                    reason = f"dispatch_denied: {origin_problem}"
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
                origin_pinned = False  # the executor's run() takes over the pin
                terminal = await self._execute(
                    prompt,
                    run.route_id,
                    worktree,
                    node_id=node.node_id,
                    attempt=run.attempt,
                    effort=run.chosen_effort
                    if run.effort_route_id == run.route_id and run.effort_attempt == run.attempt
                    else EFFORT_OMITTED,
                )
            finally:
                if origin_pinned:
                    release_executor_origin_pin(
                        self.executor, node.node_id, route_id=run.route_id, cwd=worktree
                    )
                self.inflight.pop(node.node_id, None)
            try:
                research_metrics: dict[str, Any] | None = None
                if terminal.research_fetches is not None:
                    baseline_fetches: list[dict[str, Any]] = []
                    for src in evidence.get("sources", []):
                        if isinstance(src, Mapping):
                            span = src.get("span")
                            path = src.get("path")
                            if isinstance(span, Mapping) and isinstance(path, str):
                                start = span.get("start_line", 1)
                                end = span.get("end_line")
                                limit = (
                                    (end - start + 1)
                                    if (type(end) is int and type(start) is int and end >= start)
                                    else None
                                )
                                fetch = normalize_research_fetch(
                                    "read",
                                    {"path": path, "offset": start, "limit": limit},
                                    revision=src.get("revision")
                                    if isinstance(src.get("revision"), str)
                                    else None,
                                )
                                if fetch is not None:
                                    baseline_fetches.append(fetch)
                    pack_id = str(evidence.get("pack_id") or "")
                    research_metrics = measure_research_duplication(
                        baseline_fetches,
                        terminal.research_fetches,
                        worker_id=f"{node.node_id}-a{run.attempt}",
                        model=terminal.model or run.route_id,
                        pack_id=pack_id,
                        pack_revision=pack_id,
                        total_input_tokens=terminal.usage.input_tokens
                        if terminal.usage is not None
                        else None,
                    )
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
                                **(
                                    {"cost_note": terminal.usage.cost_note}
                                    if terminal.usage.cost_note
                                    else {}
                                ),
                            }
                        }
                        if terminal.usage is not None
                        else ({"usage_missing": True} if terminal.ok else {})
                    ),
                    **(
                        {"served_check": terminal.served_check}
                        if terminal.served_check is not None
                        else {}
                    ),
                    **(
                        {"worker_assignment": terminal.worker_assignment}
                        if terminal.worker_assignment is not None
                        else {}
                    ),
                    **(
                        {"research_duplication": research_metrics}
                        if research_metrics is not None
                        else {}
                    ),
                )
                self._save_attempt(
                    node.node_id,
                    run.attempt,
                    run.route_id,
                    terminal,
                    research_metrics=research_metrics,
                )
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
                        served_check=terminal.served_check,
                    )
                if not terminal.ok or not terminal.output.strip():
                    failure = self.classifier.classify(terminal, now=self.now())
                    self._persist_research_pack(
                        node,
                        hydrated,
                        base,
                        evidence,
                        deltas=(
                            ResearchDelta("failure", f"attempt-{run.attempt}", failure.category),
                        ),
                    )
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
                    failed = WorkerTerminal(
                        ok=False, model=run.route_id, error=verdict, harness=terminal.harness
                    )
                    if verdict.startswith("environment_error:"):
                        # The verification command could not start (missing runner or
                        # module).  This is an environment problem, not a route quality
                        # issue: do not count against escalation, do not write a session
                        # ledger row or health-cache entry, and fail closed immediately.
                        #
                        # State path: TERMINAL_SUCCESS → REJECTED (the only non-VALIDATED
                        # transition from TERMINAL_SUCCESS) → BLOCKED (via _drive's
                        # action=="BLOCK" check after attempt returns False).
                        env_failure = FailureClassification(
                            "environment_error", "BLOCK", 0, "none", verdict
                        )
                        failures.append(env_failure)
                        # judged_failure_count is intentionally NOT incremented here.
                        # scope="none" and cooldown=0 mean no health-cache or selector writes.
                        self.events.emit(
                            "failure",
                            run.node.node_id,
                            category=env_failure.category,
                            action=env_failure.action,
                            route_id=run.route_id,
                            evidence=env_failure.evidence[:300],
                            fault_injected=failed.executor_kind == "fault-injected",
                            attempt=run.attempt,
                        )
                        self._set(run, NodeState.REJECTED, reason=env_failure.category)
                        return False
                    self._persist_research_pack(
                        node,
                        hydrated,
                        base,
                        evidence,
                        deltas=(
                            ResearchDelta(
                                "failure", f"attempt-{run.attempt}-verify", "verification_failed"
                            ),
                        ),
                    )
                    return await self._fail(run, failed, failures, worktree, validated=True)
                run.commit = await self.git.commit_all(
                    worktree, f"verdict({node.node_id}): {node.objective[:60]} [{run.route_id}]"
                )
                forced_report = self.git.owned_ignored.get(str(worktree))
                force_staged = list(forced_report.forced) if forced_report is not None else []
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
                self._persist_research_pack(node, hydrated, base, evidence)
                if force_staged:
                    self._set(
                        run, NodeState.VALIDATED, commit=run.commit, force_staged=force_staged
                    )
                else:
                    self._set(run, NodeState.VALIDATED, commit=run.commit)
                self._record_session_outcome(run, terminal, outcome="pass")
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

        def commit() -> DispatchPermit | None:
            cache = HealthCache(default_cache_path())
            # Read the clock in the thread, at commit time: a late thread must not
            # commit an already expired proof under an earlier timestamp.
            return cache.commit_dispatch(
                proof.scope,
                route_id=proof.route_id,
                attempt_id=attempt_id,
                authorization_digest=proof.authorization_digest,
                expected_epoch=proof.epoch,
                qualified_at=proof.qualified_at,
                proof_deadline=deadline,
                now=self.now(),
            )

        permit = await asyncio.to_thread(commit)
        if permit is not None:
            assert self.dispatch_authority is not None
            recorded_origin = (
                permit.scope.gateway_origin
                if self.dispatch_authority.record_gateway_origin
                else REDACTED_GATEWAY_ORIGIN
            )
            self.events.emit(
                "controller",
                run.node.node_id,
                phase="dispatch_permit",
                route_id=permit.route_id,
                attempt_id=attempt_id,
                permit_id=permit.permit_id,
                epoch=permit.epoch,
                gateway_origin=recorded_origin,
            )
        return permit

    def _save_attempt(
        self,
        node_id: str,
        attempt: int,
        route_id: str,
        terminal: WorkerTerminal,
        research_metrics: Mapping[str, Any] | None = None,
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
                    **({"cost_note": terminal.usage.cost_note} if terminal.usage.cost_note else {}),
                }
            if terminal.served_check is not None:
                record["served_check"] = terminal.served_check
            if terminal.worker_assignment is not None:
                record["worker_assignment"] = terminal.worker_assignment
            if terminal.research_fetches is not None:
                record["research_fetches"] = list(terminal.research_fetches)
            if research_metrics is not None:
                record["research_duplication"] = dict(research_metrics)
            (directory / f"{node_id}-a{attempt}.json").write_text(json.dumps(record, indent=1))
        except OSError:
            pass

    def _persist_research_pack(
        self,
        node: WorkNode,
        hydrated: Any,
        base: str,
        evidence: Mapping[str, Any],
        *,
        deltas: tuple[ResearchDelta, ...] = (),
    ) -> None:
        """Create or refresh the persisted ResearchPack for a node; never fatal.

        Stored under run_dir/context_artifacts/<node_id>.json so a retry or
        replacement can reuse the same research_pack_id/research_revision and
        only append deltas, without repeating retrieval.
        """
        if not isinstance(hydrated, WorkerPrompt):
            return
        pack_dir = self.run_dir / "context_artifacts"
        pack_path = pack_dir / f"{node.node_id}.json"
        source_revisions = {
            str(src["path"]): str(src["revision"])
            for src in evidence.get("sources", [])
            if isinstance(src, Mapping)
            and isinstance(src.get("path"), str)
            and src["path"].strip()
            and isinstance(src.get("revision"), str)
            and src["revision"].strip()
        }
        try:
            pack_dir.mkdir(parents=True, exist_ok=True)
            if pack_path.exists():
                research = ResearchPack.from_dict(json.loads(pack_path.read_text()))
                try:
                    updated = reuse_research_pack(
                        node,
                        research,
                        repo_revision=base,
                        source_revisions=source_revisions,
                        deltas=deltas,
                    )
                except ValueError:
                    # Stale task/source/repo revision: invalidate reuse and
                    # rebuild from the current context rather than relabeling
                    # stale facts.
                    updated = create_research_pack(
                        node,
                        hydrated.context,
                        repo_revision=base,
                        source_revisions=source_revisions,
                    )
                    if deltas:
                        updated = reuse_research_pack(
                            node,
                            updated,
                            repo_revision=base,
                            source_revisions=source_revisions,
                            deltas=deltas,
                        )
            else:
                updated = create_research_pack(
                    node, hydrated.context, repo_revision=base, source_revisions=source_revisions
                )
                if deltas:
                    updated = reuse_research_pack(
                        node,
                        updated,
                        repo_revision=base,
                        source_revisions=source_revisions,
                        deltas=deltas,
                    )
            pack_path.write_text(json.dumps(updated.to_dict(), indent=2))
        except (OSError, ValueError, json.JSONDecodeError):
            # Research persistence is advisory; a run must never fail because of it.
            pass

    async def _execute(
        self,
        prompt: str,
        route_id: str,
        cwd: Path,
        *,
        node_id: str,
        attempt: int,
        effort: Any = EFFORT_OMITTED,
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
                effort=effort,
                # The run directory's name is the run id the run receipt shows (``run_started``
                # and ``graph.json`` carry it); an executor that signs a credential binds it.
                run_id=self.run_dir.name,
                node_id=node_id,
            )
        except OwnershipError as exc:
            return WorkerTerminal(
                ok=False, model=route_id, error=f"ownership_refused: {exc}", status_code=125
            )
        except Exception as exc:  # executor bugs must not unwind the controller
            return WorkerTerminal(
                ok=False, model=route_id, error=f"transport: {type(exc).__name__}: {exc}"
            )

    def _apply_serve_denial(self, run: NodeRun, failure: FailureClassification) -> None:
        """Act on ``verdict serve``'s refusal of this attempt's route, for this run only.

        A permanent code removes the route for every node; ``provider_unhealthy`` cools the
        routes sharing its id prefix; any other code cools only that route. Nothing is
        persisted (OpenSpec change ``serve-denial-recovery``).
        """
        code = failure.evidence.removeprefix("serve_denied:")
        action = "none"
        if code in PERMANENT_DENIAL_CODES:
            exclude = getattr(self.selector, "exclude_for_run", None)
            if callable(exclude):
                exclude(run.route_id, f"serve_denied:{code}")
                action = "excluded"
        else:
            cool = getattr(self.selector, "cool_for_run", None)
            if callable(cool):
                if code == REASON_PROVIDER_UNHEALTHY:
                    prefix = run.route_id.split("/", 1)[0]
                    cool(f"prefix:{prefix}", _DENIAL_PREFIX_COOLDOWN_S, now=self.now())
                    action = "prefix_cooled"
                else:
                    cool(f"route:{run.route_id}", _DENIAL_ROUTE_COOLDOWN_S, now=self.now())
                    action = "route_cooled"
        self.events.emit(
            "controller",
            run.node.node_id,
            phase="serve_denial",
            route_id=run.route_id,
            code=code,
            action=action,
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
        # An environment error says nothing about the work: ``validated`` then only chooses
        # the state transition (REJECTED, from TERMINAL_SUCCESS), never a judgement.
        judged = validated and failure.category != "environment_error"
        # fix(retry-clean-base): track whether this attempt's work was judged wrong so
        # _attempt() can start the next attempt from the clean base.
        if failure.category == "verification_failed" and terminal.harness == "opencode":
            run.last_rejected = False
        else:
            run.last_rejected = judged or failure.category in _JUDGED_WORK_CATEGORIES
        # Capability escalation: count judged work failures and tighten the tier
        # ceiling once the threshold is reached.  Interrupted failures (transport,
        # timeout) do not count — only failures that mean the work was wrong.
        if judged or failure.category in _ESCALATION_JUDGED_CATEGORIES:
            run.judged_failure_count += 1
            if run.judged_failure_count >= _ESCALATION_JUDGED_FAILURE_THRESHOLD:
                t = run.last_selected_tier
                if t is not None and t > 1:
                    new_ceil = t - 1
                    if (
                        run.escalation_tier_ceiling is None
                        or new_ceil < run.escalation_tier_ceiling
                    ):
                        run.escalation_tier_ceiling = new_ceil
                elif t is None:
                    # Unknown-tier route: treat as weaker than any known tier.
                    # Set ceiling to _ESCALATION_UNKNOWN_TIER_CEILING (3) so the
                    # eligibility check excludes unknown-tier routes while keeping
                    # all routes with a measured capability_tier.
                    new_ceil = _ESCALATION_UNKNOWN_TIER_CEILING
                    if (
                        run.escalation_tier_ceiling is None
                        or new_ceil < run.escalation_tier_ceiling
                    ):
                        run.escalation_tier_ceiling = new_ceil
                elif t == 1:
                    # t == 1 (Sonnet-class): attempt to escalate to tier-0 (frontier).
                    # Restrict selection to subscription/free capacity so the runtime
                    # never reaches for METERED or UNKNOWN-capacity tier-0 routes
                    # through the escalation path.  Whether a qualifying tier-0
                    # candidate exists is resolved lazily by _drive; if none is found,
                    # _drive emits 'escalation_exhausted'.
                    new_ceil = 0
                    if (
                        run.escalation_tier_ceiling is None
                        or new_ceil < run.escalation_tier_ceiling
                    ):
                        run.escalation_tier_ceiling = new_ceil
                        run.escalation_capacity_classes = (
                            self.policy.escalation_tier0_capacity_classes
                        )
                else:
                    # t == 0: already running at tier-0 (frontier); no stronger tier
                    # exists.  Emit exhausted from _record_failure so the receipt
                    # captures it immediately (no escalation ceiling can be set here).
                    self.events.emit(
                        "escalation_exhausted",
                        run.node.node_id,
                        from_tier=run.last_selected_tier,
                        judged_failure_count=run.judged_failure_count,
                        reason="at_strongest_tier",
                    )
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
        if failure.category == "route_denied":
            self._apply_serve_denial(run, failure)
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
                "rejected": run.last_rejected,
            }
        )

    def _session_failure_class(self, failure: FailureClassification) -> str:
        """Map an orchestration failure category onto SessionLedger's fixed classes.

        SessionLedger admits only ``gave_up``, ``narrates_tool_calls``,
        ``false_success_claim``, ``transport_error``, ``timeout`` and
        ``wrong_result``. Availability/transport categories collapse onto
        ``transport_error``; verification and ownership failures are wrong
        results; everything else is ``wrong_result``.
        """
        category = failure.category
        if category in {"timeout", "stalled"}:
            return "timeout"
        if category in {
            "rate_limited",
            "quota_exhausted",
            "payment_required",
            "authentication",
            "permission",
            "unsupported",
            "model_unavailable",
            "gone",
            "bad_request",
            "upstream_temporary",
            "gateway_busy",
            "transport_temporary",
            "no_final_answer",
            "empty_output",
            "malformed_response",
            "model_mismatch",
            "unknown",
        }:
            return "transport_error"
        # verification_failed, ownership_violation, worker_blocked,
        # harness_configuration, ownership_refused and anything unmapped are
        # task-level wrong results, not transport faults.
        return "wrong_result"

    def _record_session_outcome(
        self, run: NodeRun, terminal: WorkerTerminal, *, outcome: Literal["pass", "fail"]
    ) -> None:
        """Append this attempt's verified outcome to the SessionLedger.

        Only model attempts on real routes are recorded; mechanical integrate
        and review nodes carry no route and are skipped. The ledger write is
        advisory: an unreadable or failing ledger must never fail the run, so
        every error path returns silently after the first failure.
        """
        if self._session_evidence_error is not None:
            return
        route_id = run.route_id
        if not route_id or route_id.startswith("local/"):
            return
        if terminal.executor_kind == "fault-injected":
            return
        try:
            from verdict.orchestration.session_evidence import SessionLedger, SessionOutcome

            failure_class = (
                None
                if outcome == "pass"
                else self._session_failure_class(self.classifier.classify(terminal, now=self.now()))
            )
            item = SessionOutcome(
                route_id=route_id,
                outcome=outcome,
                kind="real_task",
                failure_class=failure_class,
                verified_by_controller=True,
                at=self.now(),
                source="verdict.orchestration.receipt",
                child_id=f"{self.run_dir.name}:{run.node.node_id}:a{run.attempt}",
                node_kind=run.node.kind.value,
                task_family=self._session_task_family(),
            )
            SessionLedger().append(item)
        except Exception:
            self._session_evidence_error = "unreadable"
            return

    def _session_task_family(self) -> str | None:
        """The goal-derived task family, as the receipt's attempt records name it.

        A goal that cannot be fingerprinted leaves the family null. It must not
        reach the caller's ledger guard, which would stop every later write.
        """
        goal = self.graph.goal
        if not goal.strip():
            return None
        try:
            from verdict.candidate_pool import fingerprint_task

            return fingerprint_task(goal).task_family
        except Exception:
            return None

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
            # A validated=True _fail is a checker/verification failure: the
            # worker's terminal was accepted but independent checks rejected it.
            # That is a verified fail for session evidence, never a pass.
            self._record_session_outcome(run, terminal, outcome="fail")
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
            # fix(retry-clean-base): when the attempt was REJECTED (work judged wrong),
            # clear any stale salvage instead of saving, so the next attempt starts
            # from the clean base.  Only INTERRUPTED attempts (timeout, transport) save.
            if (
                run.node.owned_files
                and worktree.exists()
                and terminal.executor_kind != "fault-injected"
            ):
                salvage_dir = self.run_dir / "salvage" / run.node.node_id
                if run.last_rejected:
                    # Work was judged wrong: wipe prior salvage so the next
                    # attempt starts from the clean base.  The restore event
                    # emitted in _attempt() records which path was taken.
                    clear_err = clear_salvage_dir(salvage_dir)
                    if clear_err:
                        # Could not remove the salvage dir; stale content remains.
                        # Emit an event and poison the dir so _attempt() keeps
                        # the node on the clean-base path until an interrupted
                        # save successfully replaces the contents.
                        self.events.emit(
                            "salvage_clear_error",
                            run.node.node_id,
                            attempt=run.attempt,
                            error=clear_err,
                        )
                        run.history.append(
                            {
                                "attempt": run.attempt,
                                "route_id": run.route_id,
                                "outcome": "salvage_clear_error",
                                "error": clear_err,
                            }
                        )
                        run.salvage_poisoned = True
                else:
                    # Work was interrupted: preserve partial owned files.
                    node_base = await self._node_base(run.node)
                    changed = await self.git.changed_files(
                        worktree, node_base, owned_files=run.node.owned_files
                    )
                    result = salvage_owned_files(
                        worktree,
                        run.node.owned_files,
                        salvage_dir,
                        changed_files=changed,
                        replace_existing=True,
                        base_sha=node_base,
                        repo_root=self.repo,
                    )
                    if result.saved and not result.error:
                        # Successful save replaced any prior stale content.
                        run.salvage_poisoned = False
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

    async def _apply_lint_autofix(self, run: NodeRun, worktree: Path, changed: list[str]) -> None:
        """Run ``ruff check --fix`` then ``ruff format`` on this node's changed files.

        Runs only when ruff is a declared gate; imposing a tool not chosen by the
        repo would widen scope without consent.  Emits a ``lint_autofix`` event
        recording which files changed, so autofixed work is visible in the receipt
        and never silent.

        If ruff is not declared, emits the event with ``skipped=True``.  If ruff
        is missing, records ``ruff_missing=True`` and returns without touching any
        file.  The repo gates always run afterward; this step does not replace them.

        Only ``.py`` and ``.pyi`` files are passed to ruff.  Other owned files
        (YAML, Markdown, TOML, …) are recorded in the event as
        ``skipped_non_python`` but never passed to ruff: when ruff receives an
        explicitly named file it processes it as Python regardless of extension,
        which corrupts non-Python content.  Exit codes from ``--fix`` and
        ``format`` are recorded in the event but never treated as failures: the
        subsequent gate run decides.
        """
        node_id = run.node.node_id
        try:
            gates = discover_repo_gates(worktree)
        except ValueError:
            # config error — _run_repo_gates will surface it; skip autofix silently.
            return
        if not any(g.name in ("ruff-check", "ruff-format") for g in gates):
            self.events.emit(
                "lint_autofix",
                node_id,
                files=[],
                changed_files=[],
                skipped=True,
                ruff_missing=False,
                reason="no_ruff_gate",
            )
            return

        root = worktree.resolve()
        targets = [
            p
            for p in changed
            if run.node.owns(p)
            and not (worktree / p).is_symlink()
            and (worktree / p).is_file()
            and (worktree / p).resolve().is_relative_to(root)
        ]
        if not targets:
            self.events.emit(
                "lint_autofix",
                node_id,
                files=[],
                changed_files=[],
                skipped=False,
                ruff_missing=False,
            )
            return

        # Only Python source files are eligible for ruff; passing a non-Python
        # file by explicit path causes ruff to interpret it as Python, corrupting
        # YAML, Markdown, TOML and other formats.
        python_targets = [p for p in targets if Path(p).suffix in _RUFF_PYTHON_EXTENSIONS]
        non_python_targets = [p for p in targets if Path(p).suffix not in _RUFF_PYTHON_EXTENSIONS]

        if not python_targets:
            self.events.emit(
                "lint_autofix",
                node_id,
                files=[],
                changed_files=[],
                skipped=False,
                ruff_missing=False,
                skipped_non_python=non_python_targets,
            )
            return

        before = {
            p: hashlib.sha256((worktree / p).read_bytes()).hexdigest() for p in python_targets
        }

        try:
            check_argv = _resolve_gate_argv(("ruff", "check", "--fix", *python_targets), worktree)
            fmt_argv = _resolve_gate_argv(("ruff", "format", *python_targets), worktree)
        except FileNotFoundError:
            self.events.emit(
                "lint_autofix",
                node_id,
                files=python_targets,
                changed_files=[],
                skipped=False,
                ruff_missing=True,
                skipped_non_python=non_python_targets,
            )
            return

        check_code, check_out = await self.runner(
            check_argv, worktree, self.policy.verify_timeout_seconds
        )
        fmt_code, _fmt_out = await self.runner(
            fmt_argv, worktree, self.policy.verify_timeout_seconds
        )

        after = {
            p: hashlib.sha256((worktree / p).read_bytes()).hexdigest()
            for p in python_targets
            if (worktree / p).is_file()
        }
        fixed_files = [p for p in python_targets if before.get(p) != after.get(p)]

        self.events.emit(
            "lint_autofix",
            node_id,
            files=python_targets,
            changed_files=fixed_files,
            skipped=False,
            ruff_missing=False,
            check_command=shlex.join(["ruff", "check", "--fix", *python_targets]),
            format_command=shlex.join(["ruff", "format", *python_targets]),
            ruff_check_exit=check_code,
            ruff_format_exit=fmt_code,
            check_tail=check_out[-300:],
            before_digests={p: before[p][:16] for p in python_targets},
            after_digests={p: after.get(p, before[p])[:16] for p in python_targets},
            skipped_non_python=non_python_targets,
        )

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
        changed = await self.git.changed_files(worktree, base, owned_files=node.owned_files)
        report = self.git.owned_ignored.get(str(worktree))
        refused = report.refused if report is not None else ()
        if node.kind is NodeKind.IMPLEMENT:
            outside = [p for p in changed if not node.owns(p)]
            refused_paths = [f"{path} ({reason})" for path, reason in refused][:8]
            if outside:
                detail = "outside owned: " + ", ".join(outside[:8])
            elif refused_paths:
                detail = "owned ignored file(s) not committable: " + ", ".join(refused_paths)
            else:
                detail = f"{len(changed)} file(s) within ownership"
            ownership_extra: dict[str, Any] = {}
            if refused_paths:
                ownership_extra["refused_paths"] = refused_paths
            self.events.emit(
                "barrier",
                node.node_id,
                name="ownership",
                ok=not outside and not refused,
                stray_paths=outside,
                detail=detail,
                **ownership_extra,
            )
            if outside:
                return "ownership_violation: " + ", ".join(outside[:8])
            if refused_paths:
                return (
                    "verification_failed: owned_file_not_committable: "
                    + ", ".join(refused_paths)
                    + ". The file is git-ignored and cannot be committed as it is; remove the "
                    "credential-shaped content or the symlink, or create such data at test "
                    "run time."
                )
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
        # Apply safe lint fixes to this node's changed files before running
        # verification and repo gates, so both see the corrected code.
        # Runs only when ruff is a declared gate and the node changed files.
        if node.kind is NodeKind.IMPLEMENT and changed:
            await self._apply_lint_autofix(run, worktree, changed)
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
                env_err = _classify_verify_env_error(
                    resolved, code, out, owned_files=node.owned_files
                )
                if env_err is not None:
                    return env_err
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
            # Issue #1137: findings_lost only when OCR dropped comment calls, so
            # earlier event shapes are unchanged.
            lost_extra: dict[str, Any] = (
                {"findings_lost": review.findings_lost} if review.findings_lost else {}
            )
            budget_extra: dict[str, Any] = {}
            if review.token_budget_used:
                budget_extra["token_budget_used"] = review.token_budget_used
            if review.token_budget_source:
                budget_extra["token_budget_source"] = review.token_budget_source
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
                **lost_extra,
                **budget_extra,
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
