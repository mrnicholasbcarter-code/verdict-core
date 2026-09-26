"""Autonomous delivery controller (proof → PR → CI → merge → Done).

Architecture boundary
---------------------
* BOD-104 owns route/strategy decisions.
* BOD-67 owns hydrate/dispatch contracts (consumed, not rewritten here).
* BOD-89 owns the proof DAG (``scripts/proof/``).
* ``scripts/prime_state`` owns CI/merge classification + bounded recovery helpers.
* ``scripts/prime_workflow`` owns packet/proof/review validators and lifecycle
  transitions.
* BOD-68 owns the PR / CI watch / repair / merge / post-merge / Linear Done
  lifecycle only.

Fail-closed rules
-----------------
* Local proof must pass before PR-open.
* Never force-push ``main``.
* Worker code-repair attempts are bounded (default 2); the next distinct
  failure escalates to Astra root-cause review; persistent failure becomes
  BLOCKED.
* Transient infra failures retry without rewriting application code.
* Red ``main`` after merge blocks advancing the next story.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Protocol

from scripts.prime_state import ci_recovery_action, classify_ci, classify_merge_state
from scripts.prime_workflow import transition, validate_packet, validate_proof, validate_review
from scripts.proof.contract import ProofContract, load_contract
from scripts.proof.runner import ProofReport, ProofRunner

__all__ = [
    "DEFAULT_MAX_INFRA_RETRIES",
    "DEFAULT_MAX_WORKER_REPAIR_ATTEMPTS",
    "DeliveryBlockedError",
    "DeliveryController",
    "DeliveryError",
    "GitHubPort",
    "LinearPort",
    "LocalProofResult",
    "MergeGateResult",
    "PostMergeResult",
    "ProofRunnerFactory",
    "RepairAction",
    "RepairDecision",
    "ReviewPacket",
    "build_pr_body",
    "build_review_packet",
    "classify_ci_failure",
    "decide_repair_action",
    "merge_gates_satisfied",
    "post_merge_verify",
    "run_local_proof",
]

DEFAULT_MAX_WORKER_REPAIR_ATTEMPTS = 2
DEFAULT_MAX_INFRA_RETRIES = 2


class DeliveryError(ValueError):
    """Fail-closed delivery policy or contract error."""


class DeliveryBlockedError(DeliveryError):
    """Delivery cannot proceed; story should enter BLOCKED or wait."""


class RepairAction(str, Enum):
    """Normalized repair/continue actions owned by autonomous delivery."""

    PROCEED_MERGE = "proceed_merge"
    WAIT_CI = "wait_ci"
    CODE_FIX = "code_fix"
    REBASE = "rebase"
    RETRY_INFRA = "retry_infra"
    ESCALATE = "escalate"
    BLOCK = "block"


@dataclass(frozen=True)
class LocalProofResult:
    """Outcome of running the local proof DAG contract for a packet."""

    ok: bool
    mode: str
    head_sha: str | None
    report: Mapping[str, Any]
    failures: tuple[dict[str, Any], ...]
    blocks_pr: bool
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "mode": self.mode,
            "head_sha": self.head_sha,
            "report": dict(self.report),
            "failures": [dict(item) for item in self.failures],
            "blocks_pr": self.blocks_pr,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class RepairDecision:
    """Bounded repair policy decision (never force-pushes main)."""

    action: RepairAction
    classification: str
    reason: str
    rewrite_code: bool
    force_push_main: bool = False
    worker_repair_attempts: int = 0
    infra_attempts: int = 0
    lifecycle_hint: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action.value,
            "classification": self.classification,
            "reason": self.reason,
            "rewrite_code": self.rewrite_code,
            "force_push_main": self.force_push_main,
            "worker_repair_attempts": self.worker_repair_attempts,
            "infra_attempts": self.infra_attempts,
            "lifecycle_hint": self.lifecycle_hint,
        }


@dataclass(frozen=True)
class MergeGateResult:
    """Whether merge gates are satisfied, with named blockers."""

    ok: bool
    blockers: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "blockers": list(self.blockers)}


@dataclass(frozen=True)
class PostMergeResult:
    """Post-merge main verification; red main blocks queue advance."""

    ok: bool
    main_sha: str | None
    classification: str
    blocks_next_transition: bool
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "main_sha": self.main_sha,
            "classification": self.classification,
            "blocks_next_transition": self.blocks_next_transition,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class ReviewPacket:
    """Compact Astra review packet — not the full worker trajectory."""

    issue: str
    objective: str
    acceptance_criteria: tuple[str, ...]
    proof_contract_summary: Mapping[str, Any]
    diff_summary: str
    evidence_refs: tuple[str, ...]
    architectural_constraints: tuple[str, ...]
    head_sha: str | None
    worktree: str
    branch: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "issue": self.issue,
            "objective": self.objective,
            "acceptance_criteria": list(self.acceptance_criteria),
            "proof_contract_summary": dict(self.proof_contract_summary),
            "diff_summary": self.diff_summary,
            "evidence_refs": list(self.evidence_refs),
            "architectural_constraints": list(self.architectural_constraints),
            "head_sha": self.head_sha,
            "worktree": self.worktree,
            "branch": self.branch,
        }


class GitHubPort(Protocol):
    """Injectable GitHub/gh seam — live network stays behind this boundary."""

    def create_pull_request(self, *, title: str, body: str, head: str, base: str) -> str: ...

    def list_required_checks(
        self, *, head_sha: str, pr_url: str | None = None
    ) -> list[dict[str, Any]]: ...

    def get_mergeability(self, *, pr_url: str) -> tuple[str, str]: ...

    def merge_pull_request(self, *, pr_url: str, method: str = "squash") -> str: ...

    def list_main_checks(self, *, main_sha: str) -> list[dict[str, Any]]: ...

    def list_unresolved_review_threads(self, *, pr_url: str) -> list[str]: ...


class LinearPort(Protocol):
    """Injectable Linear seam for Done/evidence closeout."""

    def record_delivery_evidence(
        self, *, issue: str, pr_url: str, merge_sha: str, evidence: Mapping[str, Any]
    ) -> None: ...

    def mark_done(self, *, issue: str) -> None: ...


ProofRunnerFactory = Callable[[ProofContract, Path], ProofRunner]


def classify_ci_failure(checks: Sequence[Mapping[str, Any]]) -> str:
    """Classify GitHub checks using ``scripts.prime_state.classify_ci``."""
    normalized = [dict(check) for check in checks]
    return classify_ci(normalized)


def decide_repair_action(
    *,
    ci_classification: str | None = None,
    merge_classification: str | None = None,
    worker_repair_attempts: int = 0,
    infra_attempts: int = 0,
    max_worker_attempts: int = DEFAULT_MAX_WORKER_REPAIR_ATTEMPTS,
    max_infra_attempts: int = DEFAULT_MAX_INFRA_RETRIES,
    escalated: bool = False,
) -> RepairDecision:
    """Map CI/merge classification to a bounded autonomous delivery repair action.

    Preference:
    1. Merge conflict / base drift → rebase + reproof (no force-push main).
    2. Transient infra → retry without rewriting code.
    3. Code/test/lint → worker code_fix for up to ``max_worker_attempts``.
    4. Next distinct code failure → escalate to Astra.
    5. Persistent failure after escalate → block.
    """
    if worker_repair_attempts < 0 or infra_attempts < 0:
        raise DeliveryError("attempt counters must be non-negative")
    if max_worker_attempts < 1 or max_infra_attempts < 1:
        raise DeliveryError("attempt budgets must be at least 1")

    if merge_classification in {"CONFLICT", "BEHIND"}:
        return RepairDecision(
            action=RepairAction.REBASE,
            classification=merge_classification,
            reason="base drift or merge conflict — rebase and rerun proof",
            rewrite_code=False,
            worker_repair_attempts=worker_repair_attempts,
            infra_attempts=infra_attempts,
            lifecycle_hint="IMPLEMENTING",
        )

    classification = ci_classification or "EMPTY"

    if classification == "GREEN":
        return RepairDecision(
            action=RepairAction.PROCEED_MERGE,
            classification=classification,
            reason="all required checks passed",
            rewrite_code=False,
            worker_repair_attempts=worker_repair_attempts,
            infra_attempts=infra_attempts,
            lifecycle_hint="CI_GREEN",
        )

    if classification == "PENDING":
        return RepairDecision(
            action=RepairAction.WAIT_CI,
            classification=classification,
            reason="required checks not concluded",
            rewrite_code=False,
            worker_repair_attempts=worker_repair_attempts,
            infra_attempts=infra_attempts,
            lifecycle_hint="PR_OPEN",
        )

    if classification in {"INFRA_FAILURE", "CANCELLED"}:
        if infra_attempts >= max_infra_attempts:
            return RepairDecision(
                action=RepairAction.BLOCK,
                classification=classification,
                reason="infrastructure retries exhausted",
                rewrite_code=False,
                worker_repair_attempts=worker_repair_attempts,
                infra_attempts=infra_attempts,
                lifecycle_hint="PR_OPEN",
            )
        return RepairDecision(
            action=RepairAction.RETRY_INFRA,
            classification=classification,
            reason="transient infrastructure failure — retry without code rewrite",
            rewrite_code=False,
            worker_repair_attempts=worker_repair_attempts,
            infra_attempts=infra_attempts,
            lifecycle_hint="PR_OPEN",
        )

    if classification == "CODE_FAILURE":
        if worker_repair_attempts < max_worker_attempts:
            return RepairDecision(
                action=RepairAction.CODE_FIX,
                classification=classification,
                reason="required check failed on the PR head — worker repair",
                rewrite_code=True,
                worker_repair_attempts=worker_repair_attempts,
                infra_attempts=infra_attempts,
                lifecycle_hint="IMPLEMENTING",
            )
        if not escalated:
            return RepairDecision(
                action=RepairAction.ESCALATE,
                classification=classification,
                reason=("worker repair attempts exhausted — escalate to Astra root-cause review"),
                rewrite_code=False,
                worker_repair_attempts=worker_repair_attempts,
                infra_attempts=infra_attempts,
                lifecycle_hint="PR_OPEN",
            )
        return RepairDecision(
            action=RepairAction.BLOCK,
            classification=classification,
            reason="persistent code failure after Astra escalation",
            rewrite_code=False,
            worker_repair_attempts=worker_repair_attempts,
            infra_attempts=infra_attempts,
            lifecycle_hint="PR_OPEN",
        )

    # Align UNKNOWN / EMPTY / MISSING_REQUIRED with prime_state recovery.
    plan = ci_recovery_action(classification, worker_repair_attempts, max_worker_attempts)
    if plan["action"] == "BLOCKED":
        return RepairDecision(
            action=RepairAction.BLOCK,
            classification=classification,
            reason=str(plan["reason"]),
            rewrite_code=False,
            worker_repair_attempts=worker_repair_attempts,
            infra_attempts=infra_attempts,
            lifecycle_hint=str(plan.get("state") or "PR_OPEN"),
        )
    return RepairDecision(
        action=RepairAction.BLOCK,
        classification=classification,
        reason=f"unclassifiable delivery state: {classification}",
        rewrite_code=False,
        worker_repair_attempts=worker_repair_attempts,
        infra_attempts=infra_attempts,
        lifecycle_hint="PR_OPEN",
    )


def merge_gates_satisfied(
    *,
    acceptance_complete: bool,
    local_proof_ok: bool,
    astra_approved: bool,
    ci_classification: str,
    mergeable: str,
    merge_state_status: str,
    unresolved_review_threads: Sequence[str] = (),
) -> MergeGateResult:
    """Merge only when AC, proof, Astra, CI, mergeability, and threads pass."""
    blockers: list[str] = []
    if not acceptance_complete:
        blockers.append("acceptance_criteria_incomplete")
    if not local_proof_ok:
        blockers.append("local_proof_incomplete")
    if not astra_approved:
        blockers.append("astra_approval_missing")
    if ci_classification != "GREEN":
        blockers.append(f"ci_not_green:{ci_classification}")
    merge_class = classify_merge_state(mergeable, merge_state_status)
    if merge_class != "MERGEABLE":
        blockers.append(f"merge_state:{merge_class}")
    if unresolved_review_threads:
        blockers.append(f"unresolved_review_threads:{len(unresolved_review_threads)}")
    return MergeGateResult(ok=not blockers, blockers=tuple(blockers))


def post_merge_verify(
    *, main_sha: str | None, main_checks: Sequence[Mapping[str, Any]]
) -> PostMergeResult:
    """Lightweight post-merge verification; red main blocks next transition."""
    if not main_sha:
        return PostMergeResult(
            ok=False,
            main_sha=None,
            classification="EMPTY",
            blocks_next_transition=True,
            reason="main SHA missing after merge",
        )
    classification = classify_ci_failure(main_checks)
    if classification == "GREEN":
        return PostMergeResult(
            ok=True,
            main_sha=main_sha,
            classification=classification,
            blocks_next_transition=False,
            reason="main required checks green",
        )
    if classification == "PENDING":
        return PostMergeResult(
            ok=False,
            main_sha=main_sha,
            classification=classification,
            blocks_next_transition=True,
            reason="main checks still pending — do not advance queue",
        )
    return PostMergeResult(
        ok=False,
        main_sha=main_sha,
        classification=classification,
        blocks_next_transition=True,
        reason=f"main is not green ({classification}) — block next-story transition",
    )


def run_local_proof(
    packet: Mapping[str, Any],
    *,
    mode: str = "full",
    targets: Sequence[str] | None = None,
    no_cache: bool = True,
    contract_path: Path | None = None,
    runner_factory: ProofRunnerFactory | None = None,
    validate_packet_contract: bool = True,
) -> LocalProofResult:
    """Run the local proof DAG for a hydrated packet; fail closed for PR-open.

    Intentional lint/test failures surface as ``ok=False`` / ``blocks_pr=True``.
    """
    if validate_packet_contract:
        validate_packet(dict(packet))

    worktree_raw = packet.get("worktree")
    if not isinstance(worktree_raw, str) or not worktree_raw.strip():
        raise DeliveryError("packet.worktree required for local proof")
    root = Path(worktree_raw)
    if not root.is_dir():
        raise DeliveryError(f"packet worktree is not a directory: {root}")

    path = contract_path or (root / "proof" / "contract.yaml")
    if not path.is_file():
        # Fall back to repo-relative contract when the worktree is a temp fixture.
        fallback = Path(__file__).resolve().parents[1] / "proof" / "contract.yaml"
        path = fallback if fallback.is_file() else path
    if not path.is_file():
        raise DeliveryError(f"proof contract missing: {path}")

    contract = load_contract(path)
    factory = runner_factory or (lambda c, r: ProofRunner(c, root=r))
    runner = factory(contract, root)
    report: ProofReport = runner.run(mode=mode, targets=list(targets or []), no_cache=no_cache)
    payload = report.to_dict()
    failures = tuple(dict(item) for item in payload.get("failures", []))
    ok = bool(report.ok)
    if ok:
        return LocalProofResult(
            ok=True,
            mode=mode,
            head_sha=report.head_sha,
            report=payload,
            failures=(),
            blocks_pr=False,
            reason="local proof contract passed",
        )
    reason = "local proof failed — PR-open blocked"
    if failures:
        first = failures[0]
        gate = first.get("gate") or first.get("classify") or "unknown"
        reason = f"local proof failed at gate {gate} — PR-open blocked"
    return LocalProofResult(
        ok=False,
        mode=mode,
        head_sha=report.head_sha,
        report=payload,
        failures=failures,
        blocks_pr=True,
        reason=reason,
    )


def build_review_packet(
    packet: Mapping[str, Any],
    *,
    proof_result: LocalProofResult | None = None,
    diff_summary: str,
    evidence_refs: Sequence[str] = (),
) -> ReviewPacket:
    """Build a compact Astra review packet (no full worker trajectory)."""
    criteria_raw = packet.get("acceptance_criteria") or []
    criteria: list[str] = []
    if isinstance(criteria_raw, list):
        for item in criteria_raw:
            if isinstance(item, dict) and item.get("id"):
                text = item.get("text") or ""
                criteria.append(f"{item['id']}: {text}".strip())
            elif isinstance(item, str) and item.strip():
                criteria.append(item.strip())

    constraints_raw = packet.get("constraints") or []
    constraints = tuple(
        str(item) for item in constraints_raw if isinstance(item, str) and item.strip()
    )

    proof_summary: dict[str, Any] = {"requirements": packet.get("proof_requirements") or {}}
    if proof_result is not None:
        proof_summary["local_proof_ok"] = proof_result.ok
        proof_summary["mode"] = proof_result.mode
        proof_summary["failures"] = [dict(f) for f in proof_result.failures]

    return ReviewPacket(
        issue=str(packet.get("issue") or ""),
        objective=str(packet.get("objective") or ""),
        acceptance_criteria=tuple(criteria),
        proof_contract_summary=proof_summary,
        diff_summary=diff_summary.strip(),
        evidence_refs=tuple(evidence_refs),
        architectural_constraints=constraints,
        head_sha=proof_result.head_sha if proof_result else None,
        worktree=str(packet.get("worktree") or ""),
        branch=str(packet.get("branch") or ""),
    )


def build_pr_body(
    packet: Mapping[str, Any],
    *,
    proof_result: LocalProofResult,
    review: Mapping[str, Any] | None = None,
    known_limitations: Sequence[str] = (),
    lean_spec_paths: Sequence[str] = (),
    historical_github_source: str | None = None,
) -> str:
    """PR body linking Linear ID, AC checklist, proof evidence, models, gaps."""
    issue = str(packet.get("issue") or "")
    issue_url = str(packet.get("issue_url") or "")
    lines = [
        "## Summary",
        f"Autonomous delivery for [{issue}]({issue_url}).",
        "",
        "## Linear",
        f"- Issue: {issue}",
        f"- URL: {issue_url}",
    ]
    if historical_github_source:
        lines.append(f"- Historical GitHub source: {historical_github_source}")

    specs = list(lean_spec_paths) or [
        str(packet.get("spec") or ""),
        str(packet.get("plan") or ""),
        str(packet.get("tasks") or ""),
    ]
    specs = [s for s in specs if s]
    if specs:
        lines.extend(["", "## Lean Spec"])
        lines.extend(f"- `{path}`" for path in specs)

    lines.extend(["", "## Acceptance criteria"])
    for item in packet.get("acceptance_criteria") or []:
        if isinstance(item, dict):
            cid = item.get("id") or "?"
            text = item.get("text") or ""
            lines.append(f"- [x] {cid}: {text}")
        elif isinstance(item, str):
            lines.append(f"- [x] {item}")

    lines.extend(
        [
            "",
            "## Validation / evidence",
            f"- Local proof: {'PASS' if proof_result.ok else 'FAIL'} ({proof_result.mode})",
            f"- Head SHA: `{proof_result.head_sha or 'unknown'}`",
        ]
    )
    if proof_result.failures:
        lines.append(f"- Failures: {len(proof_result.failures)}")

    worker = f"{packet.get('provider')}/{packet.get('model')}"
    reviewer = (review or {}).get("reviewer") or "pending"
    lines.extend(["", "## Models", f"- Worker: `{worker}`", f"- Reviewer: `{reviewer}`"])

    lines.extend(["", "## Known limitations"])
    if known_limitations:
        lines.extend(f"- {item}" for item in known_limitations)
    else:
        lines.append("- None recorded")

    lines.extend(
        [
            "",
            "## Delivery policy (BOD-68)",
            "- Fail closed on local proof before PR-open",
            "- Bounded worker repairs then Astra escalate; never force-push main",
        ]
    )
    return "\n".join(lines) + "\n"


@dataclass
class DeliveryController:
    """Orchestrates local proof → review → PR → CI repair → merge → Done.

    Live ``gh`` / Linear / network calls stay behind injectable ports. Unit
    tests exercise policy without network.
    """

    github: GitHubPort | None = None
    linear: LinearPort | None = None
    max_worker_attempts: int = DEFAULT_MAX_WORKER_REPAIR_ATTEMPTS
    max_infra_attempts: int = DEFAULT_MAX_INFRA_RETRIES
    runner_factory: ProofRunnerFactory | None = None
    _worker_repair_attempts: int = field(default=0, init=False, repr=False)
    _infra_attempts: int = field(default=0, init=False, repr=False)
    _escalated: bool = field(default=False, init=False, repr=False)

    def run_local_proof(
        self,
        packet: Mapping[str, Any],
        *,
        mode: str = "full",
        targets: Sequence[str] | None = None,
        no_cache: bool = True,
        contract_path: Path | None = None,
        validate_packet_contract: bool = True,
    ) -> LocalProofResult:
        return run_local_proof(
            packet,
            mode=mode,
            targets=targets,
            no_cache=no_cache,
            contract_path=contract_path,
            runner_factory=self.runner_factory,
            validate_packet_contract=validate_packet_contract,
        )

    def classify_ci_failure(self, checks: Sequence[Mapping[str, Any]]) -> str:
        return classify_ci_failure(checks)

    def decide_repair_action(
        self,
        *,
        ci_classification: str | None = None,
        merge_classification: str | None = None,
        checks: Sequence[Mapping[str, Any]] | None = None,
        mergeable: str | None = None,
        merge_state_status: str | None = None,
    ) -> RepairDecision:
        merge_class = merge_classification
        if merge_class is None and mergeable is not None and merge_state_status is not None:
            merge_class = classify_merge_state(mergeable, merge_state_status)
        ci_class = ci_classification
        if ci_class is None and checks is not None:
            ci_class = classify_ci_failure(checks)
        return decide_repair_action(
            ci_classification=ci_class,
            merge_classification=merge_class,
            worker_repair_attempts=self._worker_repair_attempts,
            infra_attempts=self._infra_attempts,
            max_worker_attempts=self.max_worker_attempts,
            max_infra_attempts=self.max_infra_attempts,
            escalated=self._escalated,
        )

    def record_repair_outcome(self, decision: RepairDecision) -> None:
        """Advance attempt counters after a repair cycle completes unsuccessfully."""
        if decision.action == RepairAction.CODE_FIX:
            self._worker_repair_attempts += 1
        elif decision.action == RepairAction.RETRY_INFRA:
            self._infra_attempts += 1
        elif decision.action == RepairAction.ESCALATE:
            self._escalated = True

    def merge_gates_satisfied(
        self,
        *,
        acceptance_complete: bool,
        local_proof_ok: bool,
        astra_approved: bool,
        ci_classification: str,
        mergeable: str,
        merge_state_status: str,
        unresolved_review_threads: Sequence[str] = (),
    ) -> MergeGateResult:
        return merge_gates_satisfied(
            acceptance_complete=acceptance_complete,
            local_proof_ok=local_proof_ok,
            astra_approved=astra_approved,
            ci_classification=ci_classification,
            mergeable=mergeable,
            merge_state_status=merge_state_status,
            unresolved_review_threads=unresolved_review_threads,
        )

    def post_merge_verify(
        self, *, main_sha: str | None, main_checks: Sequence[Mapping[str, Any]]
    ) -> PostMergeResult:
        return post_merge_verify(main_sha=main_sha, main_checks=main_checks)

    def assert_pr_open_allowed(self, proof: LocalProofResult) -> None:
        """Fail closed: refuse PR-open when local proof did not pass."""
        if proof.blocks_pr or not proof.ok:
            raise DeliveryBlockedError(proof.reason)

    def open_pull_request(
        self,
        packet: Mapping[str, Any],
        *,
        proof: LocalProofResult,
        title: str,
        body: str,
        base: str = "main",
        review: Mapping[str, Any] | None = None,
        head_sha: str | None = None,
    ) -> str:
        """Open a PR only after local proof (and optional review) pass."""
        self.assert_pr_open_allowed(proof)
        if review is not None:
            sha = head_sha or proof.head_sha
            if not sha:
                raise DeliveryBlockedError("review validation requires head SHA")
            validate_review(dict(review), sha)
        if self.github is None:
            raise DeliveryError("GitHubPort required to open a pull request")
        branch = str(packet.get("branch") or "")
        if not branch or branch == base:
            raise DeliveryBlockedError("PR requires a non-base issue branch")
        return self.github.create_pull_request(title=title, body=body, head=branch, base=base)

    def watch_and_decide(
        self, *, checks: Sequence[Mapping[str, Any]], mergeable: str, merge_state_status: str
    ) -> RepairDecision:
        """Classify live CI/merge state and decide the next repair action."""
        return self.decide_repair_action(
            checks=checks, mergeable=mergeable, merge_state_status=merge_state_status
        )

    def merge_if_ready(
        self,
        *,
        pr_url: str,
        acceptance_complete: bool,
        local_proof_ok: bool,
        astra_approved: bool,
        checks: Sequence[Mapping[str, Any]],
        mergeable: str,
        merge_state_status: str,
        unresolved_review_threads: Sequence[str] = (),
    ) -> str:
        """Merge only when all gates pass; never force-push main."""
        if self.github is None:
            raise DeliveryError("GitHubPort required to merge")
        ci_class = classify_ci_failure(checks)
        gates = self.merge_gates_satisfied(
            acceptance_complete=acceptance_complete,
            local_proof_ok=local_proof_ok,
            astra_approved=astra_approved,
            ci_classification=ci_class,
            mergeable=mergeable,
            merge_state_status=merge_state_status,
            unresolved_review_threads=unresolved_review_threads,
        )
        if not gates.ok:
            raise DeliveryBlockedError(f"merge gates not satisfied: {', '.join(gates.blockers)}")
        return self.github.merge_pull_request(pr_url=pr_url, method="squash")

    def closeout_linear(
        self,
        *,
        issue: str,
        pr_url: str,
        merge_sha: str,
        post_merge: PostMergeResult,
        evidence: Mapping[str, Any] | None = None,
    ) -> None:
        """Record evidence and mark Done only after main verification passes."""
        if not post_merge.ok or post_merge.blocks_next_transition:
            raise DeliveryBlockedError(
                f"refusing Linear Done while post-merge verification failed: {post_merge.reason}"
            )
        if self.linear is None:
            raise DeliveryError("LinearPort required for Done closeout")
        payload = dict(evidence or {})
        payload.setdefault("main_sha", post_merge.main_sha)
        payload.setdefault("post_merge_classification", post_merge.classification)
        self.linear.record_delivery_evidence(
            issue=issue, pr_url=pr_url, merge_sha=merge_sha, evidence=payload
        )
        self.linear.mark_done(issue=issue)

    def advance_lifecycle(self, current: str, target: str, *, evidence: Mapping[str, Any]) -> str:
        """Delegate to ``prime_workflow.transition`` (no skipped states)."""
        return transition(current, target, dict(evidence))

    def validate_proof_artifact(self, proof: Mapping[str, Any], head_sha: str) -> None:
        """Reuse prime_workflow proof validator for durable proof JSON."""
        validate_proof(dict(proof), head_sha)
