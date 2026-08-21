"""Run the orchestrate → execute → verify loop over one objective.

One expensive orchestrator call decomposes the objective into
:class:`~verdict.work_unit.WorkUnit` slices.  Each unit is then executed by the
cheapest tier that can satisfy it and verified by running its own command:

1. **mechanical** — a deterministic fixer, no model, zero tokens;
2. **model** — a cheap route returning a patch Verdict applies within the
   unit's file boundary.

Verification is mechanical throughout: a unit counts as done only when its own
command exits zero *and* the files it changed are a subset of the ones it owns.
Both checks run after execution, so an escape is caught even if the boundary
check somehow let it through.

Every unit's outcome is written to a :class:`~verdict.receipt_store.ReceiptStore`
under the ``autodev`` scope, so the record survives the process.  The reported
expensive/cheap token split comes from provider ``usage`` blocks; units whose
provider omitted usage are counted separately rather than estimated.
"""

from __future__ import annotations

import hashlib
import subprocess
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from verdict.decomposer import DEFAULT_ORCHESTRATOR_MODEL, Decomposer, DecompositionConfig
from verdict.patch_executor import (
    DEFAULT_BASE_URL,
    PatchAttempt,
    PatchExecutor,
    PatchExecutorConfig,
    TokenUsage,
)
from verdict.receipt_store import ReceiptStore
from verdict.work_unit import WorkUnit, normalize_owned_path

DEFAULT_EXECUTOR_MODEL = "cc/claude-sonnet-5"
AUTODEV_SCOPE = "autodev"
# Measured token counts, not secrets: without this the receipt store's
# ``token``/``prompt``/``completion`` key patterns redact the very numbers the
# expensive/cheap split is supposed to be evidence for.
_USAGE_ALLOWLIST = ("usage.prompt_tokens", "usage.completion_tokens", "usage.total_tokens")


class AutodevError(RuntimeError):
    """Raised when a run cannot proceed."""


@dataclass(frozen=True)
class UnitOutcome:
    """What happened to one unit, and what it cost.

    ``tier`` is ``mechanical`` (no model) or ``model``.  ``verified`` is true
    only when the unit's own command exited zero and the change stayed inside
    its boundary.
    """

    unit_id: str
    tier: str
    model: str
    verified: bool
    reason: str = ""
    changed_files: tuple[str, ...] = ()
    usage: TokenUsage = field(default_factory=TokenUsage)
    latency_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "unit_id": self.unit_id,
            "tier": self.tier,
            "model": self.model,
            "verified": self.verified,
            "reason": self.reason,
            "changed_files": list(self.changed_files),
            "usage": self.usage.to_dict(),
            "latency_ms": self.latency_ms,
        }


@dataclass(frozen=True)
class AutodevReport:
    """The measured result of one run. Claims nothing the receipts do not hold."""

    objective: str
    repo_path: str
    orchestrator_model: str
    executor_model: str
    units_planned: int
    outcomes: tuple[UnitOutcome, ...]
    orchestrator_usage: TokenUsage
    decomposition_latency_ms: int = 0
    receipt_ids: tuple[str, ...] = ()

    @property
    def verified(self) -> tuple[UnitOutcome, ...]:
        return tuple(o for o in self.outcomes if o.verified)

    @property
    def failed(self) -> tuple[UnitOutcome, ...]:
        return tuple(o for o in self.outcomes if not o.verified)

    @property
    def mechanical(self) -> tuple[UnitOutcome, ...]:
        return tuple(o for o in self.outcomes if o.tier == "mechanical")

    @property
    def executor_usage(self) -> TokenUsage:
        prompt = sum(o.usage.prompt_tokens for o in self.outcomes)
        completion = sum(o.usage.completion_tokens for o in self.outcomes)
        reported = any(o.usage.reported for o in self.outcomes)
        return TokenUsage(prompt_tokens=prompt, completion_tokens=completion, reported=reported)

    @property
    def unreported_units(self) -> tuple[str, ...]:
        """Units that used a model but whose provider omitted a usage block."""
        return tuple(o.unit_id for o in self.outcomes if o.tier == "model" and not o.usage.reported)

    def to_dict(self) -> dict[str, Any]:
        orchestrator = self.orchestrator_usage.total_tokens
        executor = self.executor_usage.total_tokens
        total = orchestrator + executor
        return {
            "objective": self.objective,
            "repo_path": self.repo_path,
            "units": {
                "planned": self.units_planned,
                "attempted": len(self.outcomes),
                "verified": len(self.verified),
                "failed": len(self.failed),
                "mechanical": len(self.mechanical),
            },
            "tokens": {
                "orchestrator_model": self.orchestrator_model,
                "executor_model": self.executor_model,
                "orchestrator": self.orchestrator_usage.to_dict(),
                "executor": self.executor_usage.to_dict(),
                "total": total,
                "expensive_share": round(orchestrator / total, 4) if total else None,
                "units_without_reported_usage": list(self.unreported_units),
            },
            "decomposition_latency_ms": self.decomposition_latency_ms,
            "outcomes": [o.to_dict() for o in self.outcomes],
            "receipt_ids": list(self.receipt_ids),
        }

    def summary(self) -> str:
        """Human-readable summary. Every number here comes from a receipt."""
        data = self.to_dict()
        tokens = data["tokens"]
        lines = [
            f"objective: {self.objective}",
            (
                f"units: {len(self.verified)} verified, {len(self.failed)} failed, "
                f"of {self.units_planned} planned"
            ),
            f"  mechanical (zero tokens): {len(self.mechanical)}",
            f"  model ({self.executor_model}): {len(self.outcomes) - len(self.mechanical)}",
            (
                f"tokens: orchestrator {tokens['orchestrator']['total_tokens']} "
                f"({self.orchestrator_model}), executor {tokens['executor']['total_tokens']}"
            ),
        ]
        share = tokens["expensive_share"]
        if share is not None:
            lines.append(f"  expensive share: {share:.1%} of {tokens['total']} measured tokens")
        if self.unreported_units:
            lines.append(
                f"  {len(self.unreported_units)} unit(s) had no provider usage block; "
                "their tokens are unknown, not estimated"
            )
        for outcome in self.failed:
            lines.append(f"  FAILED {outcome.unit_id}: {outcome.reason}")
        return "\n".join(lines)


def run_autodev(
    objective: str,
    repo_path: str | Path,
    *,
    orchestrator_model: str = DEFAULT_ORCHESTRATOR_MODEL,
    executor_model: str = DEFAULT_EXECUTOR_MODEL,
    base_url: str = DEFAULT_BASE_URL,
    api_key: str | None = None,
    store: ReceiptStore | None = None,
    decomposer: Decomposer | None = None,
    executor: PatchExecutor | None = None,
    evidence: str = "",
    mechanical: bool = True,
    runner: Any = subprocess.run,
) -> AutodevReport:
    """Decompose ``objective``, execute each unit, verify it, and record it."""
    repo = Path(repo_path).resolve()
    if not (repo / ".git").exists():
        raise AutodevError(f"not a git repository: {repo}")

    decomposer = decomposer or Decomposer(
        DecompositionConfig(model=orchestrator_model, base_url=base_url, api_key=api_key)
    )
    plan = decomposer.decompose(objective, repo_root=repo, evidence=evidence)

    executor = executor or PatchExecutor(
        repo, PatchExecutorConfig(model=executor_model, base_url=base_url, api_key=api_key)
    )
    ledger = store if store is not None else ReceiptStore(repo / ".verdict" / "receipts.db")

    outcomes: list[UnitOutcome] = []
    receipt_ids: list[str] = []
    for unit in plan.units:
        outcome = _run_unit(unit, repo, executor, mechanical=mechanical, runner=runner)
        outcomes.append(outcome)
        record = ledger.put_receipt(
            "outcome",
            AUTODEV_SCOPE,
            {
                "objective": objective,
                "orchestrator_model": plan.model,
                **outcome.to_dict(),
                "verification_command": list(unit.verification_command),
                "owned_files": list(unit.owned_files),
            },
            provenance={"source": "verdict.autodev", "authority": "observed"},
            allowlist=_USAGE_ALLOWLIST,
        )
        receipt_ids.append(record.receipt_id)

    return AutodevReport(
        objective=objective,
        repo_path=str(repo),
        orchestrator_model=plan.model,
        executor_model=executor.config.model,
        units_planned=len(plan.units),
        outcomes=tuple(outcomes),
        orchestrator_usage=plan.usage,
        decomposition_latency_ms=plan.latency_ms,
        receipt_ids=tuple(receipt_ids),
    )


def _run_unit(
    unit: WorkUnit, repo: Path, executor: PatchExecutor, *, mechanical: bool, runner: Any
) -> UnitOutcome:
    """Try the cheapest tier that can satisfy ``unit``, then verify mechanically."""
    started = time.monotonic()
    # Units run in sequence against one tree, so earlier units leave it dirty.
    # Attribution is the delta against this snapshot, not the whole dirty set.
    before = _tree_state(repo, runner=runner)

    if mechanical and _try_mechanical(unit, repo, runner=runner):
        touched = _touched_since(repo, before, runner=runner)
        ok, reason = _verify(unit, repo, touched, runner=runner)
        return UnitOutcome(
            unit_id=unit.unit_id,
            tier="mechanical",
            model="none",
            verified=ok,
            reason=reason,
            changed_files=touched,
            latency_ms=int((time.monotonic() - started) * 1000),
        )

    # The mechanical tier may have written a partial repair before declining the
    # unit. Those edits are on disk either way, so they belong in this unit's
    # attribution rather than going unreported or landing on a later unit.
    attempt = executor.execute_unit(unit)
    if not attempt.applied:
        return _from_attempt(
            attempt,
            verified=False,
            reason=attempt.reason,
            changed_files=_touched_since(repo, before, runner=runner),
        )

    touched = _touched_since(repo, before, runner=runner)
    ok, reason = _verify(unit, repo, touched, runner=runner)
    return _from_attempt(attempt, verified=ok, reason=reason, changed_files=touched)


def _from_attempt(
    attempt: PatchAttempt, *, verified: bool, reason: str, changed_files: tuple[str, ...]
) -> UnitOutcome:
    return UnitOutcome(
        unit_id=attempt.unit_id,
        tier="model",
        model=attempt.model,
        verified=verified,
        reason=reason,
        changed_files=changed_files,
        usage=attempt.usage,
        latency_ms=attempt.latency_ms,
    )


def _try_mechanical(unit: WorkUnit, repo: Path, *, runner: Any) -> bool:
    """Attempt a deterministic fix, returning whether the tier claims the unit.

    Only ``ruff``-verified units qualify, and the fixer is scoped to the files
    the unit owns so it cannot repair another unit's work.  Returning True means
    the tier ran and the unit's command now passes; verification still confirms
    it independently.
    """
    if unit.verification_command[0] != "ruff":
        return False
    fix = _run(["ruff", "check", "--fix", "--", *unit.owned_files], repo, runner=runner)
    if fix.get("returncode") == 127:
        return False
    check = _run(list(unit.verification_command), repo, runner=runner)
    return bool(check.get("returncode") == 0)


def _verify(
    unit: WorkUnit, repo: Path, touched: tuple[str, ...], *, runner: Any
) -> tuple[bool, str]:
    """Run the unit's own command, then confirm the change stayed in bounds.

    This repeats the executor's pre-apply boundary check against what the tree
    actually shows, so an escape is still caught if a patch landed a path the
    header did not name.
    """
    escaped = unit.out_of_bounds(touched)
    if escaped:
        return False, f"changed files outside the unit boundary: {list(escaped)}"
    result = _run(list(unit.verification_command), repo, runner=runner)
    code = result.get("returncode")
    if code != 0:
        detail = (result.get("stderr") or result.get("stdout") or "").strip()[-300:]
        return False, f"verification exited {code}: {detail}"
    return True, ""


def _tree_state(repo: Path, *, runner: Any) -> dict[str, str]:
    """Map every dirty path to a status-and-content fingerprint.

    The content hash matters: two units editing the same file would both show
    status ``' M'``, and a status-only snapshot would attribute the second
    edit to nobody.
    """
    result = _run(["git", "status", "--porcelain", "--untracked-files=all"], repo, runner=runner)
    if result.get("returncode") != 0:
        return {}
    state: dict[str, str] = {}
    for line in (result.get("stdout") or "").splitlines():
        if len(line) < 4:
            continue
        code, raw = line[:2], line[3:].strip()
        if not raw:
            continue
        targets = raw.split(" -> ") if " -> " in raw else [raw]
        for part in targets:
            paths: set[str] = set()
            _add_path(paths, part)
            for path in paths:
                state[path] = f"{code}:{_content_hash(repo / path)}"
    return state


def _content_hash(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()[:16]
    except OSError:
        return "missing"


def _touched_since(repo: Path, before: dict[str, str], *, runner: Any) -> tuple[str, ...]:
    """Return paths whose git status changed since ``before`` was captured."""
    after = _tree_state(repo, runner=runner)
    changed = {path for path, code in after.items() if before.get(path) != code}
    changed |= {path for path in before if path not in after}
    return tuple(sorted(changed))


def _add_path(paths: set[str], raw: str) -> None:
    candidate = raw.strip().strip('"')
    if not candidate:
        return
    try:
        paths.add(normalize_owned_path(candidate))
    except Exception:
        paths.add(candidate)  # unnormalizable paths stay, so they fail the boundary check


def _run(command: Sequence[str], cwd: Path, *, runner: Any, timeout: int = 300) -> dict[str, Any]:
    try:
        completed = runner(
            list(command),
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError:
        return {"returncode": 127, "stdout": "", "stderr": f"command not found: {command[0]}"}
    except subprocess.TimeoutExpired:
        return {"returncode": 124, "stdout": "", "stderr": f"timed out after {timeout}s"}
    return {
        "returncode": completed.returncode,
        "stdout": completed.stdout or "",
        "stderr": completed.stderr or "",
    }


def collect_ruff_evidence(repo: Path, *, runner: Any = subprocess.run, limit: int = 8000) -> str:
    """Return current ruff output, so decomposition plans against real state."""
    result = _run(["ruff", "check", "--output-format", "concise", "."], repo, runner=runner)
    if result.get("returncode") == 127:
        return ""
    output = (result.get("stdout") or "") + (result.get("stderr") or "")
    return output[:limit]


__all__ = [
    "AUTODEV_SCOPE",
    "DEFAULT_EXECUTOR_MODEL",
    "AutodevError",
    "AutodevReport",
    "UnitOutcome",
    "collect_ruff_evidence",
    "run_autodev",
]
