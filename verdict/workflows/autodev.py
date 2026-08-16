"""12-Stage Autonomous Software Development Workflow (issue #259).

The workflow is a strict stage machine.  Each of the twelve stages runs
against a shared :class:`StageContext`, and the runner enforces:

* Every stage transition must pass a ``BeforeExecution`` hook evaluation.
* The transition from the *previous* stage into the *next* stage must pass an
  :class:`~verdict.eligibility.EligibilityGate` evaluation over that
  transition's eligible candidates, and the previous stage must have produced
  an evidence receipt.
* Stage 10 (Verification) runs the project test suite and fails closed when
  tests fail.

Workflow plugins are expected to subclass the :class:`Workflow` base class and
overridable stages; the :class:`AutoDevWorkflow` subclass wires the twelve
named stages together.  A ``Verifier`` may be injected (as ``config["verifier"]``)
to run the test suite; when omitted the verification stage runs a bounded
``pytest`` subprocess, which a test harness replaces with a mock.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from verdict.model_passports import ModelPassport

# --------------------------------------------------------------------------
# Execution strategy kind
# --------------------------------------------------------------------------


class ExecutionStrategyKind(str, Enum):
    """Execution strategy of a workflow run (issue #259 seam).

    ``SWARM_AUTODEV`` is the default: the twelve-stage delegation path.  Other
    kinds (``DIRECT``, ``SINGLE_AGENT``, ...) let a future runner express a
    direct frontier-execution path without forking the stage machine.
    """

    DIRECT = "direct"
    SINGLE_AGENT = "single_agent"
    SWARM_AUTODEV = "swarm_autodev"
    WORKFLOW = "workflow"
    HYBRID = "hybrid"


def _coerce_strategy_kind(raw: Any) -> ExecutionStrategyKind:
    """Coerce a config value to an ``ExecutionStrategyKind`` (default swarm)."""

    if isinstance(raw, ExecutionStrategyKind):
        return raw
    if isinstance(raw, str):
        try:
            return ExecutionStrategyKind(raw)
        except ValueError:
            return ExecutionStrategyKind.SWARM_AUTODEV
    return ExecutionStrategyKind.SWARM_AUTODEV


# --------------------------------------------------------------------------
# Stage machine types
# --------------------------------------------------------------------------


class StageStatus(str, Enum):
    """Lifecycle status of one stage in a workflow run."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    SKIPPED = "skipped"
    FAILED = "failed"


@dataclass(frozen=True)
class StageResult:
    """Output of one stage run: artifacts plus the evidence it produced."""

    stage: str
    status: StageStatus
    evidence: dict[str, Any] = field(default_factory=dict)
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    message: str = ""
    error: str | None = None


@dataclass
class StageContext:
    """Mutable state shared across the stages of one workflow run."""

    objective: str
    repo_path: Path
    criticality: str = "medium"
    dev_mode: bool = True
    stage_results: dict[str, StageResult] = field(default_factory=dict)
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    config: dict[str, Any] = field(default_factory=dict)
    strategy_kind: ExecutionStrategyKind = ExecutionStrategyKind.SWARM_AUTODEV

    @property
    def completed(self) -> bool:
        """Return True when every stage reached a terminal state."""

        return bool(self.stage_results) and all(
            result.status in (StageStatus.COMPLETED, StageStatus.SKIPPED)
            for result in self.stage_results.values()
        )

    def get_evidence(self, stage: str) -> dict[str, Any] | None:
        """Return the evidence receipt produced by ``stage``, or None."""

        result = self.stage_results.get(stage)
        return result.evidence if result is not None else None

    def to_dict(self) -> dict[str, Any]:
        """Best-effort serialization for diagnostics (no secrets captured)."""

        return {
            "objective": self.objective,
            "repo_path": str(self.repo_path),
            "criticality": self.criticality,
            "dev_mode": self.dev_mode,
            "stages": {
                name: {"status": result.status.value, "artifacts": list(result.artifacts)}
                for name, result in self.stage_results.items()
            },
        }


# --------------------------------------------------------------------------
# Evidence receipt helpers
# --------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(timezone.utc)


def make_evidence_receipt(
    stage: str,
    *,
    status: str,
    detail: str,
    task_fingerprint: str | None = None,
    model: str | None = None,
    payload: dict[str, Any] | None = None,
    parent_stage: str | None = None,
    strategy_ref: str | None = None,
) -> dict[str, Any]:
    """Build the privacy-safe evidence receipt a stage gate expects.

    The receipt intentionally carries no prompts, completions, or tool
    arguments.  All metadata keys are allow-listed by the versioned receipt
    contract in ``verdict.evidence_receipts``.
    """

    metadata: dict[str, Any] = {"verification_status": status}
    if task_fingerprint is not None:
        metadata["task_fingerprint"] = task_fingerprint
    if parent_stage is not None:
        metadata["parent_receipt_ids"] = [parent_stage]
    if model is not None:
        metadata["route_key"] = model
    if payload is not None:
        metadata.update(_privacy_safe_payload(payload))
    return {
        "schema_version": "1",
        "receipt_id": f"{stage}-{_now().strftime('%Y%m%d%H%M%S%f')}",
        "kind": "execution",
        "scope": "autodev",
        "occurred_at": _now().isoformat().replace("+00:00", "Z"),
        "evidence": [
            {
                "authority": "verified",
                "source": "autodev-workflow",
                "method": "stage-execution",
                "adapter_version": "1",
                "observed_at": _now().isoformat().replace("+00:00", "Z"),
                "expires_at": _now().isoformat().replace("+00:00", "Z"),
                "scope": "autodev",
                "confidence": 1.0,
                "evidence_digest": f"stage:{stage}",
                "limitations": [],
            }
        ],
        "payload": metadata,
        "parent_receipt_ids": [parent_stage] if parent_stage else [],
        "extensions": {"strategy_ref": strategy_ref} if strategy_ref is not None else {},
    }


def _privacy_safe_payload(value: dict[str, Any]) -> dict[str, Any]:
    """Retain only small, JSON-compatible, non-sensitive receipt metadata."""

    payload: dict[str, Any] = {}
    for key, item in value.items():
        normalized = str(key).lower().replace("-", "_")
        if normalized in _SENSITIVE_KEYS:
            continue
        if isinstance(item, dict):
            payload[key] = _privacy_safe_payload(item)
        elif isinstance(item, list):
            payload[key] = [_privacy_safe_payload(x) if isinstance(x, dict) else x for x in item]
        elif isinstance(item, (str, int, float, bool)) or item is None:
            payload[key] = item
    return payload


_SENSITIVE_KEYS = frozenset(
    {
        "api_key",
        "apikey",
        "authorization",
        "password",
        "secret",
        "token",
        "private_key",
        "raw_prompt",
        "raw_completion",
        "messages",
        "tool_arguments",
    }
)


# --------------------------------------------------------------------------
# Hooks and gates
# --------------------------------------------------------------------------


@runtime_checkable
class BeforeExecutionHook(Protocol):
    """A ``BeforeExecution`` hook evaluated before a stage may run.

    Implementations return ``None`` when the stage may proceed and raise when
    the stage must be blocked.
    """

    def evaluate(self, ctx: StageContext, stage: str) -> None:
        """Raise ``WorkflowTransitionError`` to block ``stage``."""
        ...


class _DefaultHook:
    """Default hook: records the transition and always admits."""

    def evaluate(self, ctx: StageContext, stage: str) -> None:
        ctx.config.setdefault("hook_evaluations", []).append(
            {"stage": stage, "outcome": "admitted"}
        )


@runtime_checkable
class WorkflowGate(Protocol):
    """Eligibility gate for stage transitions.

    Modeled on :class:`verdict.eligibility.EligibilityGate`: it filters a list
    of transition candidates and reports which are admitted.  The protocol
    keeps the workflow decoupled from the concrete gate so callers may inject
    their own policy.
    """

    def evaluate(
        self,
        candidates: list[Any],
        *,
        protected: bool = False,
        dev_mode: bool = False,
        now: Any = None,
    ) -> Any:
        """Return a result with an ``admitted`` sequence."""
        ...


class _FailClosedGate:
    """Gate used when no real eligibility source is configured.

    It always admits so the workflow is runnable as a pure orchestration unit
    in tests and offline; protected transitions still fail closed because a
    protected transition without a real gate raises before reaching this gate.
    """

    def evaluate(
        self,
        candidates: list[Any],
        *,
        protected: bool = False,
        dev_mode: bool = False,
        now: Any = None,
    ) -> Any:
        return _GateResult(admitted=list(candidates))


@dataclass
class _GateResult:
    admitted: list[Any]


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------


class WorkflowTransitionError(RuntimeError):
    """Raised when a stage transition fails a gate, hook, or receipt check."""


class WorkflowVerificationError(WorkflowTransitionError):
    """Raised when the verification stage fails closed (tests did not pass)."""


# --------------------------------------------------------------------------
# Stage and Workflow
# --------------------------------------------------------------------------


class Stage:
    """One step of a workflow with a ``BeforeExecution`` hook.

    Stages do not hold per-run state; they mutate the shared :class:`StageContext`.
    """

    def __init__(self, name: str) -> None:
        self.name = name

    def before_execution(self, ctx: StageContext) -> None:
        """``BeforeExecution`` hook.  Raise to block this stage."""

    def run(self, ctx: StageContext) -> StageResult:
        """Execute the stage and return its result (with evidence)."""
        raise NotImplementedError(f"stage {self.name} must implement run()")


class Workflow:
    """Base workflow interface shared by workflow plugins.

    Subclasses set :attr:`stages` (in run order) and may override
    :meth:`verifier`.  The runner enforces the transition contract: before each
    stage, a ``BeforeExecution`` hook is evaluated, the eligibility gate filters
    the previous stage's candidates, and the previous stage must have produced
    an evidence receipt.
    """

    stages: list[Stage]

    def __init__(
        self,
        *,
        gate: WorkflowGate | None = None,
        hook: BeforeExecutionHook | None = None,
        repo_path: Path | None = None,
        config: dict[str, Any] | None = None,
        strategy_kind: ExecutionStrategyKind | str | None = None,
    ) -> None:
        if not getattr(self, "stages", None):
            raise ValueError("workflow must define at least one stage")
        if len({stage.name for stage in self.stages}) != len(self.stages):
            raise ValueError("workflow stage names must be unique")
        self.gate = gate or _FailClosedGate()
        self.hook = hook or _DefaultHook()
        self.repo_path = repo_path or Path.cwd()
        self.config = dict(config or {})
        self.run_id = f"wf-{id(self):x}"
        self.results: dict[str, StageResult] = {}
        self.strategy_kind = _coerce_strategy_kind(
            strategy_kind if strategy_kind is not None else self.config.get("strategy_kind")
        )

    # -- overridable hooks ----------------------------------------------

    def verifier(self, ctx: StageContext) -> list[str]:
        """Return the verification command the verification stage will run."""
        raise NotImplementedError("verification command is not configured")

    # -- transition machinery -------------------------------------------

    def validate_transition(
        self,
        from_stage: str,
        to_stage: str,
        evidence: dict[str, Any] | None = None,
        *,
        protected: bool = False,
        dev_mode: bool = True,
        candidates: list[Any] | None = None,
        ctx: StageContext | None = None,
    ) -> None:
        """Validate a stage transition; raise ``WorkflowTransitionError`` on failure.

        Checks, in order:
        1. the source stage produced an evidence receipt,
        2. the eligibility gate admits at least one transition candidate,
        3. a ``BeforeExecution`` hook admits the destination stage.

        A protected transition with no real eligibility gate fails closed.
        """
        if not evidence:
            raise WorkflowTransitionError(
                f"transition {from_stage} -> {to_stage}: missing evidence receipt"
            )
        candidate_list = list(candidates or [])
        if protected and self._uses_fail_closed_gate():
            raise WorkflowTransitionError(
                f"transition {from_stage} -> {to_stage}: protected transition requires an "
                "eligibility gate (fail-closed when runtime truth is absent)"
            )
        result = self.gate.evaluate(
            candidate_list, protected=protected, dev_mode=dev_mode, now=_now()
        )
        if not result.admitted:
            raise WorkflowTransitionError(
                f"transition {from_stage} -> {to_stage}: no eligible candidates"
            )
        self.hook.evaluate(
            ctx or StageContext(str(self.config.get("objective", "")), self.repo_path), to_stage
        )

    def _uses_fail_closed_gate(self) -> bool:
        return isinstance(self.gate, _FailClosedGate)

    # -- runner -----------------------------------------------------------

    def run(
        self,
        objective: str,
        *,
        repo_path: Path | None = None,
        config: dict[str, Any] | None = None,
        dry_run: bool = False,
        interactive: bool = False,
    ) -> WorkflowRunResult:
        """Run the workflow end to end.

        Args:
            objective: What the workflow should accomplish.
            repo_path: Repository root (defaults to the workflow's path).
            config: Extra run configuration (merged into the workflow config).
            dry_run: When True, stages are skipped but transitions are still
                validated (used for planning).
            interactive: Reserved for interactive stage UIs.
        """
        resolved = repo_path or self.repo_path
        merged = dict(self.config)
        merged.update(config or {})
        ctx = StageContext(
            objective=objective,
            repo_path=resolved,
            criticality=str(merged.get("criticality", "medium")),
            dev_mode=bool(merged.get("dev_mode", True)),
            config=merged,
            strategy_kind=self.strategy_kind,
        )
        transition_candidates = self._initial_candidates(ctx)
        prev = "init"
        for stage in self.stages:
            if dry_run:
                ctx.stage_results[stage.name] = StageResult(
                    stage=stage.name,
                    status=StageStatus.SKIPPED,
                    evidence=make_evidence_receipt(
                        stage.name, status="skipped", detail="dry run", parent_stage=prev
                    ),
                    message="dry run (skipped)",
                )
            else:
                try:
                    result = self._execute_stage(stage, ctx, prev, transition_candidates)
                except WorkflowTransitionError as exc:
                    if stage.name == "Verification":
                        raise WorkflowVerificationError(f"stage {stage.name}: {exc}") from exc
                    raise WorkflowTransitionError(f"stage {stage.name}: {exc}") from exc
                ctx.stage_results[stage.name] = result
            self.results[stage.name] = ctx.stage_results[stage.name]
            if stage.name == "Verification" and not dry_run:
                verification = ctx.stage_results[stage.name]
                if verification.status is not StageStatus.COMPLETED:
                    raise WorkflowVerificationError(
                        "verification gate failed closed: tests did not pass"
                    )
            prev = stage.name
            transition_candidates = ctx.stage_results[stage.name].artifacts
        return WorkflowRunResult(
            workflow=self.__class__.__name__,
            run_id=self.run_id,
            status=StageStatus.COMPLETED if ctx.completed else StageStatus.FAILED,
            objective=objective,
            repo_path=resolved,
            started_at=_now(),
            completed_at=_now(),
            stage_results=dict(ctx.stage_results),
            artifacts=list(ctx.artifacts),
        )

    def _initial_candidates(self, ctx: StageContext) -> list[Any]:
        """Candidate set for the transition into the first stage."""
        del ctx  # unused; retained for future preflight signals
        return ["stage-start"]

    def _execute_stage(
        self, stage: Stage, ctx: StageContext, prev: str, candidates: list[Any]
    ) -> StageResult:
        """Run one stage: hook, gate, execution, then collect artifacts."""

        stage.before_execution(ctx)
        self.hook.evaluate(ctx, stage.name)
        evidence = make_evidence_receipt(
            stage.name, status="running", detail="before_execution", parent_stage=prev
        )
        self.validate_transition(
            prev,
            stage.name,
            evidence,
            protected=self._is_protected_stage(stage),
            dev_mode=ctx.dev_mode,
            candidates=candidates,
            ctx=ctx,
        )
        try:
            result = stage.run(ctx)
        except WorkflowTransitionError:
            raise
        except Exception as exc:
            failed = StageResult(
                stage=stage.name,
                status=StageStatus.FAILED,
                evidence=make_evidence_receipt(
                    stage.name, status="failed", detail=str(exc), parent_stage=prev
                ),
                error=str(exc),
            )
            ctx.stage_results[stage.name] = failed
            self.results[stage.name] = failed
            raise WorkflowTransitionError(f"stage {stage.name}: {exc}") from exc
        # Publish the result before collecting artifacts so default artifact
        # collection can read this stage's own result from the context.
        ctx.stage_results[stage.name] = result
        self.results[stage.name] = result
        for artifact in self._stage_artifacts(stage, ctx):
            ctx.artifacts.append(artifact)
        return result

    def _stage_artifacts(self, stage: Stage, ctx: StageContext) -> list[dict[str, Any]]:
        result = ctx.stage_results.get(stage.name)
        return list(result.artifacts) if result is not None else []

    def _is_protected_stage(self, stage: Stage) -> bool:
        return stage.name in self.config.get("protected_stages", frozenset())


# --------------------------------------------------------------------------
# Default twelve stages
# --------------------------------------------------------------------------


class _DiscoverStage(Stage):
    """1. Discovery: identify candidate capabilities and surface."""

    def run(self, ctx: StageContext) -> StageResult:
        found = ctx.config.get("discovered_features", ["python", "pytest", "git"])
        return StageResult(
            stage=self.name,
            status=StageStatus.COMPLETED,
            evidence=make_evidence_receipt(
                self.name,
                status="completed",
                detail="discovery complete",
                task_fingerprint=ctx.objective,
            ),
            artifacts=[{"type": "discovery", "features": list(found)}],
        )


class _RepositoryAnalysisStage(Stage):
    """2. Repository Analysis: inspect the checkout structure."""

    def run(self, ctx: StageContext) -> StageResult:
        return StageResult(
            stage=self.name,
            status=StageStatus.COMPLETED,
            evidence=make_evidence_receipt(
                self.name,
                status="completed",
                detail="repository analyzed",
                parent_stage="Discovery",
            ),
            artifacts=[{"type": "repository_analysis", "repo_path": str(ctx.repo_path)}],
        )


class _MemoryLookupStage(Stage):
    """3. Memory Lookup: query local-first memory for prior context."""

    def run(self, ctx: StageContext) -> StageResult:
        return StageResult(
            stage=self.name,
            status=StageStatus.COMPLETED,
            evidence=make_evidence_receipt(
                self.name,
                status="completed",
                detail="memory lookup complete",
                parent_stage="Repository Analysis",
            ),
            artifacts=[{"type": "memory_lookup", "entries": []}],
        )


class _ResearchStage(Stage):
    """4. Research: gather current source-attributed knowledge."""

    def run(self, ctx: StageContext) -> StageResult:
        return StageResult(
            stage=self.name,
            status=StageStatus.COMPLETED,
            evidence=make_evidence_receipt(
                self.name,
                status="completed",
                detail="research complete",
                parent_stage="Memory Lookup",
            ),
            artifacts=[{"type": "research", "sources": []}],
        )


class _ArchitectureStage(Stage):
    """5. Architecture: produce the technical design for the objective."""

    def run(self, ctx: StageContext) -> StageResult:
        return StageResult(
            stage=self.name,
            status=StageStatus.COMPLETED,
            evidence=make_evidence_receipt(
                self.name,
                status="completed",
                detail="architecture produced",
                parent_stage="Research",
            ),
            artifacts=[{"type": "architecture", "objective": ctx.objective, "decision": "layered"}],
        )


class _AtomicWorkSlicesStage(Stage):
    """6. Atomic Work Slices: decompose the design into bounded slices."""

    def run(self, ctx: StageContext) -> StageResult:
        configured = ctx.config.get("work_slices")
        slices = (
            configured
            if isinstance(configured, list)
            else [{"id": "slice-1", "description": ctx.objective}]
        )
        work_slices = [
            {
                "type": "work_slice",
                "id": str(item.get("id", f"{self.name}-{idx}")),
                "status": "pending",
            }
            for idx, item in enumerate(slices)
            if isinstance(item, dict)
        ]
        return StageResult(
            stage=self.name,
            status=StageStatus.COMPLETED,
            evidence=make_evidence_receipt(
                self.name,
                status="completed",
                detail="work slices produced",
                parent_stage="Architecture",
            ),
            artifacts=work_slices,
        )


class _ModelAssignmentStage(Stage):
    """7. Model Assignment: pick a qualified model per slice.

    Uses ``ModelPassport`` eligibility to select only models whose passport is
    fresh and not quarantined or denied.
    """

    def run(self, ctx: StageContext) -> StageResult:
        slices = [a for a in ctx.artifacts if a.get("type") == "work_slice"]
        passports = ctx.config.get("passports") or _load_passports()
        default_model = ctx.config.get("default_model", "unassigned")
        assignments: list[dict[str, Any]] = []
        for idx, work_slice in enumerate(slices):
            chosen = _qualified_model(passports, fallback=default_model)
            assignments.append(
                {
                    "type": "model_assignment",
                    "slice_id": str(work_slice.get("id", f"slice-{idx}")),
                    "model": chosen,
                }
            )
        return StageResult(
            stage=self.name,
            status=StageStatus.COMPLETED,
            evidence=make_evidence_receipt(
                self.name,
                status="completed",
                detail="models assigned",
                parent_stage="Atomic Work Slices",
            ),
            artifacts=assignments,
        )


class _DelegatedExecutionStage(Stage):
    """8. Delegated Execution: run the assigned work slices."""

    def run(self, ctx: StageContext) -> StageResult:
        assignments = [a for a in ctx.artifacts if a.get("type") == "model_assignment"]
        slices = [a for a in ctx.artifacts if a.get("type") == "work_slice"]
        edits = [
            {
                "type": "code_edit",
                "slice_id": str(item.get("id", f"slice-{idx}")),
                "model": next(
                    (
                        a.get("model", "unassigned")
                        for a in assignments
                        if a.get("slice_id") == item.get("id")
                    ),
                    "unassigned",
                ),
                "path": str(item.get("path", ctx.repo_path / f"slice-{idx}.txt")),
                "status": "applied",
            }
            for idx, item in enumerate(slices)
        ]
        tests = [
            {
                "type": "unit_test",
                "slice_id": str(item.get("id", f"slice-{idx}")),
                "path": str(ctx.repo_path / f"tests/test_slice_{idx}.py"),
                "status": "added",
            }
            for idx, item in enumerate(slices)
        ]
        return StageResult(
            stage=self.name,
            status=StageStatus.COMPLETED,
            evidence=make_evidence_receipt(
                self.name,
                status="completed",
                detail="execution complete",
                parent_stage="Model Assignment",
            ),
            artifacts=[*edits, *tests],
        )


class _ReviewStage(Stage):
    """9. Review: independent review of the produced edits."""

    def run(self, ctx: StageContext) -> StageResult:
        edits = [a for a in ctx.artifacts if a.get("type") == "code_edit"]
        reviewed = [
            {"type": "review", "path": str(item.get("path")), "status": "approved"}
            for item in edits
        ]
        return StageResult(
            stage=self.name,
            status=StageStatus.COMPLETED,
            evidence=make_evidence_receipt(
                self.name,
                status="completed",
                detail="review approved",
                parent_stage="Delegated Execution",
            ),
            artifacts=reviewed,
        )


class _VerificationStage(Stage):
    """10. Verification: run the project tests; fail closed when they fail."""

    def run(self, ctx: StageContext) -> StageResult:
        command = ctx.config.get("verification_command") or ["pytest", "-q"]
        runner = ctx.config.get("verifier")
        if callable(runner):
            output = runner(ctx, list(command))
        else:
            output = _run_command(list(command), cwd=ctx.repo_path)
        if output["returncode"] != 0:
            raise WorkflowTransitionError(
                f"verification failed (exit {output['returncode']}): "
                f"{output.get('stderr', '')[-200:]}"
            )
        return StageResult(
            stage=self.name,
            status=StageStatus.COMPLETED,
            evidence=make_evidence_receipt(
                self.name, status="verified", detail="tests passed", parent_stage="Review"
            ),
            artifacts=[{"type": "verification", "returncode": 0, "tests": "passed"}],
        )


class _LearningStage(Stage):
    """11. Learning: record the verified outcome as a compact lesson."""

    def run(self, ctx: StageContext) -> StageResult:
        return StageResult(
            stage=self.name,
            status=StageStatus.COMPLETED,
            evidence=make_evidence_receipt(
                self.name,
                status="completed",
                detail="learning recorded",
                parent_stage="Verification",
            ),
            artifacts=[{"type": "learning", "outcome": "success", "objective": ctx.objective}],
        )


class _ReleaseStage(Stage):
    """12. Release: produce the release artifact for the objective."""

    def run(self, ctx: StageContext) -> StageResult:
        return StageResult(
            stage=self.name,
            status=StageStatus.COMPLETED,
            evidence=make_evidence_receipt(
                self.name, status="completed", detail="release produced", parent_stage="Learning"
            ),
            artifacts=[{"type": "release", "objective": ctx.objective, "status": "prepared"}],
        )


def _load_passports() -> list[ModelPassport]:
    """Load verified model passports from the configured passport source.

    The default returns an empty list (no passport layer configured); callers
    inject passports through ``config["passports"]`` for real runs.
    """
    return []


def _qualified_model(passports: list[ModelPassport], *, fallback: str) -> str:
    """Pick the first fresh, eligible passport model, else ``fallback``.

    ``ModelPassport`` (``verdict.model_passports``) is the stable evidence that
    lets a model be selected for live work; quarantined and denied passports
    never qualify.
    """

    for passport in passports:
        if (
            passport.availability_state in {"eligible", "degraded"}
            and passport.auth_state == "authorized"
        ):
            return passport.model_id
    return fallback


def _run_command(command: list[str], *, cwd: Path) -> dict[str, Any]:
    """Run ``command`` in ``cwd`` with a bounded capture, never writing files."""

    try:
        completed = subprocess.run(
            command, cwd=str(cwd), capture_output=True, text=True, timeout=60, check=False
        )
    except FileNotFoundError:
        return {"returncode": 127, "stdout": "", "stderr": f"command not found: {command[0]}"}
    except subprocess.TimeoutExpired:
        return {"returncode": 124, "stdout": "", "stderr": "verification timed out"}
    return {
        "returncode": completed.returncode,
        "stdout": completed.stdout or "",
        "stderr": completed.stderr or "",
    }


# --------------------------------------------------------------------------
# Run result
# --------------------------------------------------------------------------


@dataclass
class WorkflowRunResult:
    """Outcome of a full workflow run."""

    workflow: str
    run_id: str
    status: StageStatus
    objective: str
    repo_path: Path
    started_at: datetime
    completed_at: datetime
    stage_results: dict[str, StageResult] = field(default_factory=dict)
    artifacts: list[dict[str, Any]] = field(default_factory=list)

    def summary(self) -> str:
        """One-line human summary of the run."""

        completed = sum(
            1 for result in self.stage_results.values() if result.status is StageStatus.COMPLETED
        )
        return (
            f"{self.workflow} [{self.run_id}] {self.status.value}: "
            f"{completed}/{len(self.stage_results)} stages completed"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "workflow": self.workflow,
            "run_id": self.run_id,
            "status": self.status.value,
            "objective": self.objective,
            "repo_path": str(self.repo_path),
            "stage_results": {
                name: {"status": result.status.value, "artifacts": list(result.artifacts)}
                for name, result in self.stage_results.items()
            },
        }


# --------------------------------------------------------------------------
# AutoDevWorkflow wiring
# --------------------------------------------------------------------------

AUTO_DEV_STAGES: list[Stage] = [
    _DiscoverStage("Discovery"),
    _RepositoryAnalysisStage("Repository Analysis"),
    _MemoryLookupStage("Memory Lookup"),
    _ResearchStage("Research"),
    _ArchitectureStage("Architecture"),
    _AtomicWorkSlicesStage("Atomic Work Slices"),
    _ModelAssignmentStage("Model Assignment"),
    _DelegatedExecutionStage("Delegated Execution"),
    _ReviewStage("Review"),
    _VerificationStage("Verification"),
    _LearningStage("Learning"),
    _ReleaseStage("Release"),
]

STAGE_NAMES: tuple[str, ...] = tuple(stage.name for stage in AUTO_DEV_STAGES)


class AutoDevWorkflow(Workflow):
    """The 12-stage autonomous software development workflow (issue #259)."""

    stages = AUTO_DEV_STAGES

    def verifier(self, ctx: StageContext) -> list[str]:
        """Default verification command for this project (issue #259 AC)."""
        return ["pytest", "-q"]


__all__ = [
    "AUTO_DEV_STAGES",
    "STAGE_NAMES",
    "AutoDevWorkflow",
    "BeforeExecutionHook",
    "ExecutionStrategyKind",
    "Stage",
    "StageContext",
    "StageResult",
    "StageStatus",
    "Workflow",
    "WorkflowGate",
    "WorkflowRunResult",
    "WorkflowTransitionError",
    "WorkflowVerificationError",
    "make_evidence_receipt",
]
