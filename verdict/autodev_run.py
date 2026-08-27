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
import json
import os
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, cast

from verdict.context_pack import (
    ContextPack,
    ContextPackCompiler,
    ContextPlan,
    ContextUnit,
    SlotType,
)
from verdict.decomposer import DEFAULT_ORCHESTRATOR_MODEL, Decomposer, DecompositionConfig
from verdict.execution_packet import ExecutionPacket, capture_source_binding
from verdict.patch_executor import (
    DEFAULT_BASE_URL,
    PatchAttempt,
    PatchExecutor,
    PatchExecutorConfig,
    TokenUsage,
)
from verdict.receipt_store import ReceiptStore
from verdict.work_unit import WorkUnit, normalize_owned_path

# No model name is hardcoded as policy: the default executor route is resolved
# dynamically from live gateway availability + the eligibility gate. This
# constant is only the last-resort fallback when no gateway is reachable.
DEFAULT_EXECUTOR_MODEL = "cc/claude-sonnet-5"


def _resolve_default_executor_model() -> str:
    """Resolve the cheap-executor route from live gateway evidence, fail-open to the fallback."""
    try:
        from verdict.subagent_models import select_model_for_role

        model = select_model_for_role("scout", dev_mode=True)
        if model is not None and model.id:
            return model.id
    except Exception:
        pass
    return DEFAULT_EXECUTOR_MODEL


def _resolve_default_orchestrator_model() -> str:
    """Resolve the orchestrator route from live gateway evidence, fail-open to the fallback."""
    try:
        from verdict.subagent_models import select_model_for_role

        model = select_model_for_role("oracle", dev_mode=True)
        if model is not None and model.id:
            return model.id
    except Exception:
        pass
    return DEFAULT_ORCHESTRATOR_MODEL


AUTODEV_SCOPE = "autodev"
_STABLE_CONTEXT_OBSERVED_AT = "1970-01-01T00:00:00Z"
# Measured token counts, not secrets: without this the receipt store's
# ``token``/``prompt``/``completion`` key patterns redact the very numbers the
# expensive/cheap split is supposed to be evidence for.
_USAGE_ALLOWLIST = ("usage.prompt_tokens", "usage.completion_tokens", "usage.total_tokens")
_PACKET_RECEIPT_ALLOWLIST = (
    *_USAGE_ALLOWLIST,
    "token_budget",
    "used_tokens",
)


def _context_text(value: Any) -> str:
    """Render request values in a stable, prompt-safe representation."""
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        return json.dumps(dict(value), sort_keys=True, separators=(",", ":"))
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return "\n".join(_context_text(item) for item in value)
    return str(value)


def _worker_context_unit(
    *, unit_id: str, slot_type: str, key: str, content: str, source_uri: str
) -> ContextUnit:
    """Create a source-attributed unit for the deterministic worker pack."""
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    return ContextUnit(
        unit_id=unit_id,
        slot_type=cast(SlotType, slot_type),
        key=key,
        content=content,
        source_uri=source_uri,
        source_digest=f"sha256:{digest}",
        revision="request",
        trust="caller-provided",
        authority="execution-request",
        sensitivity="standard",
        observed_at=_STABLE_CONTEXT_OBSERVED_AT,
        created_at=0.0,
    )


def compile_worker_context(
    *,
    objective: str,
    non_goals: Sequence[str],
    acceptance: Sequence[str],
    authority: Mapping[str, Any],
    owned_source: Mapping[str, str],
    repository_instructions: Sequence[str],
    relevant_examples: Sequence[str],
    governing_docs: Sequence[str],
    symbol_relationship: str | None = None,
    token_budget: int = 4096,
) -> ContextPack:
    """Compile the bounded deterministic context package for one worker.

    This is deliberately a composition seam over :class:`ContextPackCompiler`:
    it does no retrieval, decomposition, or model-specific formatting.  Each
    request field becomes a source-attributed unit and the compiler owns
    ordering, sanitization, budgeting, and omission decisions.
    """
    units: list[ContextUnit] = []

    def add(slot_type: str, key: str, content: Any, source_uri: str) -> None:
        rendered = _context_text(content)
        if not rendered.strip():
            return
        units.append(
            _worker_context_unit(
                unit_id=f"autodev:{key}",
                slot_type=slot_type,
                key=key,
                content=rendered,
                source_uri=source_uri,
            )
        )

    add("instructions", "objective", objective, "urn:verdict:autodev:objective")
    add("policy", "non_goals", non_goals, "urn:verdict:autodev:non_goals")
    add("policy", "acceptance", acceptance, "urn:verdict:autodev:acceptance")
    add("policy", "authority", authority, "urn:verdict:autodev:authority")
    for path, source in owned_source.items():
        add("evidence", f"owned_source:{path}", source, path)
    add(
        "instructions",
        "repository_instructions",
        repository_instructions,
        "urn:verdict:autodev:repository_instructions",
    )
    add("examples", "relevant_examples", relevant_examples, "urn:verdict:autodev:relevant_examples")
    add("policy", "governing_docs", governing_docs, "urn:verdict:autodev:governing_docs")
    if symbol_relationship is not None:
        add(
            "evidence",
            "symbol_relationship",
            symbol_relationship,
            "urn:verdict:autodev:symbol_relationship",
        )

    plan = ContextPlan(
        plan_id="autodev:worker-context",
        candidate_id="autodev-worker",
        token_budget=token_budget,
        created_at=_STABLE_CONTEXT_OBSERVED_AT,
    )
    pack = ContextPackCompiler(default_token_budget=token_budget).compile_units(units, plan)
    return replace(pack, created_at=0.0)


def compile_packet_context(
    packet: ExecutionPacket,
    repo_path: str | Path,
    *,
    repository_instruction_paths: Sequence[str] = ("AGENTS.md",),
    governing_doc_paths: Sequence[str] = (),
    relevant_example_paths: Sequence[str] = (),
    symbol_relationship: str | None = None,
    token_budget: int = 4096,
    store: ReceiptStore | None = None,
) -> ContextPack:
    """Compile and optionally receipt the deterministic worker input for a packet.

    Retrieval is intentionally bounded to explicit packet-owned paths and
    caller-selected repository documents. Missing optional documents are omitted;
    no placeholder content or whole-repository scan enters the worker prompt.
    """
    repo = Path(repo_path).resolve()

    def read_selected(paths: Sequence[str]) -> dict[str, str]:
        selected: dict[str, str] = {}
        for raw_path in paths:
            normalized = normalize_owned_path(raw_path)
            target = repo / normalized
            try:
                selected[normalized] = target.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
        return selected

    owned_source = read_selected(tuple(str(path) for path in packet.authority["owned_paths"]))
    instructions = read_selected(repository_instruction_paths)
    examples = read_selected(relevant_example_paths)
    governing_docs = read_selected(governing_doc_paths)
    verification = "\n".join(str(arg) for arg in packet.verification["argv"])
    pack = compile_worker_context(
        objective=str(packet.intent["goal"]),
        non_goals=tuple(str(value) for value in packet.intent["non_goals"]),
        acceptance=tuple(str(value) for value in packet.intent["acceptance"]),
        authority=packet.authority,
        owned_source=owned_source,
        repository_instructions=tuple(instructions.values()),
        relevant_examples=(*examples.values(), verification),
        governing_docs=tuple(governing_docs.values()),
        symbol_relationship=symbol_relationship,
        token_budget=token_budget,
    )
    if store is not None:
        store.put_receipt(
            "context",
            "operational-loop",
            {
                "packet_id": packet.packet_id,
                "context_digest": pack.digest,
                "context_receipt": pack.receipt.to_dict(),
                "compiled_prompt": pack.compiled_prompt,
                "used_tokens": pack.used_tokens,
                "token_budget": pack.token_budget,
                "omissions": [
                    decision.to_dict()
                    for decision in pack.decisions
                    if decision.action == "exclude"
                ],
            },
            provenance={"source": "verdict.autodev_run", "authority": "compiled"},
            idempotency_key=f"packet-context:{packet.packet_id}:{pack.digest}",
            allowlist=_PACKET_RECEIPT_ALLOWLIST,
        )
    return pack


@dataclass(frozen=True)
class PacketAttempt:
    """Redacted, source-bound result of one isolated worker attempt."""

    requested_identity: str
    actual_identity: str
    changed_files: tuple[str, ...] = ()
    artifact_digest: str = ""
    verified: bool = False
    reason: str = ""
    failure_class: str | None = None
    usage: TokenUsage = field(default_factory=TokenUsage)
    latency_ms: int = 0


@dataclass(frozen=True)
class PacketAutodevReport:
    terminal_state: str
    attempts: tuple[PacketAttempt, ...] = ()
    fallback_count: int = 0
    checkpoints: dict[str, str] = field(default_factory=dict)
    resumed: bool = False
    context_digest: str | None = None
    receipt_ids: tuple[str, ...] = ()


def _packet_event(store: ReceiptStore, payload: dict[str, Any], *, key: str | None = None) -> str:
    record = store.put_receipt(
        "execution",
        "operational-loop",
        payload,
        provenance={"source": "verdict.autodev_run", "authority": "observed"},
        idempotency_key=key,
        allowlist=_PACKET_RECEIPT_ALLOWLIST,
    )
    return record.receipt_id


def _attempt_files(repo: Path) -> tuple[str, ...]:
    tracked = subprocess.run(
        ["git", "-C", str(repo), "diff", "--name-only", "--relative"],
        capture_output=True, text=True, check=True,
    ).stdout.splitlines()
    untracked = subprocess.run(
        ["git", "-C", str(repo), "ls-files", "--others", "--exclude-standard"],
        capture_output=True, text=True, check=True,
    ).stdout.splitlines()
    return tuple(sorted(set(tracked + untracked)))


def _attempt_digest(repo: Path, paths: tuple[str, ...]) -> str:
    digest = hashlib.sha256()
    for path in paths:
        digest.update(path.encode())
        candidate = repo / path
        if candidate.is_file():
            digest.update(candidate.read_bytes())
    return f"sha256:{digest.hexdigest()}"


def _make_attempt_worktree(repo: Path, commit: str) -> Path:
    path = Path(tempfile.mkdtemp(prefix="verdict-autodev-attempt-"))
    path.rmdir()
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "add", "--detach", str(path), commit],
        capture_output=True, text=True, check=True,
    )
    return path


def _remove_attempt_worktree(repo: Path, attempt_repo: Path) -> None:
    """Remove a disposable attempt and its Git registration."""
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "remove", "--force", str(attempt_repo)],
        capture_output=True,
        text=True,
        check=False,
    )
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "prune"],
        capture_output=True,
        text=True,
        check=False,
    )


def _replay_attempt(attempt_repo: Path, repo: Path) -> None:
    diff = subprocess.run(
        ["git", "-C", str(attempt_repo), "diff", "--binary", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout
    if diff:
        subprocess.run(
            ["git", "-C", str(repo), "apply", "--whitespace=nowarn", "-"],
            input=diff, capture_output=True, text=True, check=True,
        )
    # `git diff` never contains untracked files; a worker-created test file was
    # verified in the attempt and must not silently vanish on replay.
    untracked = subprocess.run(
        ["git", "-C", str(attempt_repo), "ls-files", "--others", "--exclude-standard"],
        capture_output=True, text=True, check=True,
    ).stdout.split()
    for relpath in untracked:
        source = attempt_repo / relpath
        target = repo / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)


def _route_is_admitted(route: Mapping[str, Any] | None, *, fallback: bool = False) -> bool:
    if route is None or route.get("admitted") is not True:
        return False
    # Fresh source-linked evidence digest is part of admission; a bare
    # identity or a self-report alone never qualifies (AC-0.10).
    evidence_digest = str(route.get("evidence_digest", ""))
    if not evidence_digest.startswith("sha256:"):
        return False
    requested = str(route.get("requested_identity", ""))
    resolved = str(route.get("actual_identity", requested))
    if any(value.startswith("auto/") for value in (requested, resolved)):
        return False
    # Fallback must occupy the primary-subscription *role* from evidence.
    # Brand prefixes are observations, never admission policy (AC-P.1).
    if fallback:
        return route.get("primary") is True
    return True


def _default_failure_class(attempt: PacketAttempt) -> str | None:
    if attempt.verified:
        return None
    if "outside owned paths" in attempt.reason:
        return "policy_boundary"
    if attempt.reason.startswith("verification exited"):
        return "verification_failed"
    if attempt.reason:
        return "worker_failed"
    return "unknown_failure"


def _packet_work_unit(packet: ExecutionPacket, context_prompt: str) -> WorkUnit:
    task = packet.tasks[0]
    return WorkUnit(
        unit_id=str(task["task_id"]),
        objective=str(task["description"]),
        owned_files=tuple(str(path) for path in packet.authority["owned_paths"]),
        verification_command=tuple(str(arg) for arg in packet.verification["argv"]),
        context=context_prompt,
    )


def _default_packet_executor_factory(
    *, attempt_repo: Path, route: Mapping[str, Any], packet: ExecutionPacket
) -> PatchExecutor:
    del packet
    requested = str(route.get("requested_identity", route.get("model", "")))
    if not requested:
        raise AutodevError("admitted route has no requested model identity")
    return PatchExecutor(
        attempt_repo,
        PatchExecutorConfig(
            model=requested,
            base_url=str(route.get("base_url", DEFAULT_BASE_URL)),
            api_key=cast(str | None, route.get("api_key")),
            timeout_seconds=float(route.get("timeout_seconds", 120.0)),
        ),
    )


def worker_capability_report(
    required_capabilities: Sequence[str],
    candidate: Mapping[str, Any],
    evidence_by_worker: Mapping[str, Any],
    now: Any,
) -> dict[str, Any]:
    """Classify one handoff candidate strictly on fresh source-linked evidence.

    Name, model tier, reputation, historical ranking, and self-report never
    establish qualification (AC-0.10); only fresh evidence objects carrying a
    source link can.
    """
    alias = str(candidate.get("handoff_to") or candidate.get("requested_identity") or "")
    evidence = evidence_by_worker.get(alias)
    checked = [
        {"worker": alias, "evidence_source": getattr(evidence, "source", None), "fresh": None}
    ]
    unsatisfied: list[str] = []
    if evidence is None:
        unsatisfied.extend(required_capabilities)
        for name in ("name", "tier", "reputation", "ranking"):
            if name in candidate:
                checked.append({"rejected_input": name, "value": "<redacted>"})
        return {
            "qualified": False,
            "worker": alias,
            "unsatisfied_capabilities": sorted(set(unsatisfied)),
            "evidence_checked": checked,
        }
    capabilities = dict(getattr(evidence, "capabilities", {}))
    fresh = bool(evidence.is_fresh(now))
    if not fresh:
        unsatisfied.append("freshness")
    checked[0]["fresh"] = fresh
    for capability in required_capabilities:
        status = capabilities.get(capability, "unknown")
        if status != "observed":
            unsatisfied.append(capability)
    return {
        "qualified": not unsatisfied,
        "worker": alias,
        "unsatisfied_capabilities": sorted(set(unsatisfied)),
        "evidence_checked": checked,
    }


def run_packet_autodev(
    packet: ExecutionPacket,
    repo_path: str | Path,
    *,
    admitted_route: Mapping[str, Any],
    executor_factory: Any = _default_packet_executor_factory,
    store: ReceiptStore | None = None,
    verification_runner: Any = subprocess.run,
    fallback_route: Mapping[str, Any] | None = None,
    refresh_fallback: Callable[[PacketAttempt], Mapping[str, Any] | None] | None = None,
    classify_failure: Callable[[PacketAttempt], str | None] = _default_failure_class,
    resume: bool = False,
    worker_evidence: Mapping[str, Any] | None = None,
) -> PacketAutodevReport:
    """Run one admitted packet task in a clean worktree, with one fallback."""
    repo = Path(repo_path).resolve()
    ledger = store or ReceiptStore(repo / ".verdict" / "receipts.db")
    records = [
        record
        for record in ledger.query_receipts(scope="operational-loop")
        if record.payload.get("packet_id") == packet.packet_id
    ]
    if resume:
        terminal = next((r.payload.get("terminal_state") for r in records if r.payload.get("terminal_state")), None)
        if terminal in {"completed", "truthful_failure", "drifted"}:
            checkpoints = {
                str(record.payload["checkpoint"]): record.receipt_id
                for record in records
                if "checkpoint" in record.payload
            }
            context_digest = next(
                (
                    str(record.payload["context_digest"])
                    for record in records
                    if "context_digest" in record.payload
                ),
                None,
            )
            return PacketAutodevReport(
                str(terminal),
                checkpoints=checkpoints,
                resumed=True,
                context_digest=context_digest,
                receipt_ids=tuple(record.receipt_id for record in records),
            )

    current = capture_source_binding(
        repo, repository=str(packet.source["repository"]), lock_paths=tuple(packet.source["lock_digests"])
    )
    if dict(current) != dict(packet.source):
        _packet_event(ledger, {"packet_id": packet.packet_id, "terminal_state": "drifted", "reason": "source binding mismatch"}, key=f"packet:{packet.packet_id}:drifted")
        return PacketAutodevReport("drifted")
    if not _route_is_admitted(admitted_route):
        _packet_event(
            ledger,
            {
                "packet_id": packet.packet_id,
                "terminal_state": "no_eligible_route",
                "reason": "route was not admitted or was opaque",
            },
        )
        return PacketAutodevReport("no_eligible_route")

    required_capabilities = tuple(str(c) for c in admitted_route.get("required_capabilities", ()))
    handoff_to = admitted_route.get("handoff_to")
    if handoff_to:
        from datetime import datetime as _dt

        report_qual = worker_capability_report(
            required_capabilities, dict(admitted_route), dict(worker_evidence or {}), _dt.now()
        )
        preserved = {
            "packet_id": packet.packet_id,
            "packet_version": packet.packet_version,
            "authority": packet.authority,
            "intent_acceptance": packet.intent["acceptance"],
        }
        if report_qual["qualified"]:
            _packet_event(
                ledger,
                {
                    "event": "handoff",
                    "packet_id": packet.packet_id,
                    "to_worker": report_qual["worker"],
                    "integrity_digest": packet.integrity_digest,
                    "preserved": preserved,
                },
                key=f"packet:{packet.packet_id}:handoff",
            )
        else:
            blocked_payload = {
                "packet_id": packet.packet_id,
                "terminal_state": "blocked_no_qualified_worker",
                "unsatisfied_capabilities": report_qual["unsatisfied_capabilities"],
                "evidence_checked": report_qual["evidence_checked"],
                "resumable": True,
                "integrity_digest": packet.integrity_digest,
            }
            receipt_id = _packet_event(
                ledger, blocked_payload, key=f"packet:{packet.packet_id}:blocked"
            )
            return PacketAutodevReport(
                "blocked_no_qualified_worker",
                checkpoints={},
                context_digest=None,
                receipt_ids=(receipt_id,),
            )

    context = compile_packet_context(packet, repo, store=ledger)
    unit = _packet_work_unit(packet, context.compiled_prompt)

    checkpoint = _packet_event(
        ledger, {"packet_id": packet.packet_id, "checkpoint": "before_inference", "state": "attempt_started"},
        key=f"packet:{packet.packet_id}:before-inference",
    )
    checkpoints = {"before_inference": checkpoint}
    owned = tuple(str(path) for path in packet.authority["owned_paths"])
    attempts: list[PacketAttempt] = []
    routes: list[Mapping[str, Any]] = [admitted_route]
    fallback_count = 0
    index = 0
    while index < len(routes) and index < 2:
        route = routes[index]
        attempt_repo = _make_attempt_worktree(repo, str(packet.source["commit"]))
        try:
            executor = executor_factory(attempt_repo=attempt_repo, route=route, packet=packet)
            if hasattr(executor, "execute_packet_unit"):
                result = executor.execute_packet_unit(
                    packet=packet,
                    checkpoint_id=checkpoint,
                    route=route,
                    context_prompt=context.compiled_prompt,
                )
            else:
                result = executor.execute_unit(unit)
            changed = _attempt_files(attempt_repo)
            outside = tuple(sorted(set(changed) - set(owned)))
            reason = str(getattr(result, "reason", ""))
            if outside:
                reason = f"artifact touches files outside owned paths: {list(outside)}"
            verified = not outside and getattr(result, "outcome", "") == "applied"
            if verified:
                # The venv's editable-install .pth pins `verdict` to the
                # checkout the venv was created in; without this override the
                # isolated attempt worktree silently verifies the wrong tree.
                env = {**os.environ, "PYTHONPATH": str(attempt_repo)}
                checked = verification_runner(
                    list(packet.verification["argv"]),
                    cwd=str(attempt_repo),
                    timeout=packet.verification["timeout_seconds"],
                    check=False,
                    capture_output=True,
                    text=True,
                    env=env,
                )
                verified = checked.returncode == 0
                if not verified:
                    reason = f"verification exited {checked.returncode}"
            changed_owned = tuple(sorted(set(changed) & set(owned)))
            requested_identity = str(
                route.get("requested_identity", getattr(result, "model", "unknown"))
            )
            served = getattr(result, "resolved_model", None)
            if isinstance(served, str) and served.strip():
                actual_identity = served
            else:
                actual_identity = str(
                    route.get("actual_identity", getattr(result, "model", "unknown"))
                )
            provisional = PacketAttempt(
                requested_identity,
                actual_identity,
                changed_owned,
                _attempt_digest(attempt_repo, changed_owned),
                verified,
                reason,
                usage=getattr(result, "usage", TokenUsage()),
                latency_ms=int(getattr(result, "latency_ms", 0)),
            )
            failure_class = classify_failure(provisional)
            attempt = PacketAttempt(
                **{
                    **provisional.__dict__,
                    "failure_class": failure_class,
                }
            )
            attempts.append(attempt)
            _packet_event(
                ledger,
                {
                    "packet_id": packet.packet_id,
                    "attempt": index + 1,
                    "requested_identity": attempt.requested_identity,
                    "actual_identity": attempt.actual_identity,
                    "changed_files": list(attempt.changed_files),
                    "artifact_digest": attempt.artifact_digest,
                    "verified": verified,
                    "reason": reason,
                    "failure_class": failure_class,
                    "usage": attempt.usage.to_dict(),
                    "latency_ms": attempt.latency_ms,
                    "route": dict(route),
                },
            )
            if verified:
                replay_source = capture_source_binding(
                    repo,
                    repository=str(packet.source["repository"]),
                    lock_paths=tuple(packet.source["lock_digests"]),
                )
                if dict(replay_source) != dict(packet.source):
                    receipt_id = _packet_event(
                        ledger,
                        {
                            "packet_id": packet.packet_id,
                            "terminal_state": "drifted",
                            "reason": "source changed before verified patch replay",
                        },
                        key=f"packet:{packet.packet_id}:replay-drifted",
                    )
                    return PacketAutodevReport(
                        "drifted",
                        tuple(attempts),
                        fallback_count,
                        checkpoints,
                        context_digest=context.digest,
                        receipt_ids=(receipt_id,),
                    )
                _replay_attempt(attempt_repo, repo)
                receipt_id = _packet_event(
                    ledger,
                    {"packet_id": packet.packet_id, "terminal_state": "completed"},
                    key=f"packet:{packet.packet_id}:terminal",
                )
                return PacketAutodevReport(
                    "completed",
                    tuple(attempts),
                    fallback_count,
                    checkpoints,
                    context_digest=context.digest,
                    receipt_ids=(receipt_id,),
                )
        finally:
            _remove_attempt_worktree(repo, attempt_repo)

        if index == 0 and failure_class is not None:
            refreshed = refresh_fallback(attempt) if refresh_fallback is not None else fallback_route
            if _route_is_admitted(refreshed, fallback=True):
                assert refreshed is not None
                routes.append(refreshed)
                fallback_count = 1
        index += 1

    receipt_id = _packet_event(
        ledger,
        {"packet_id": packet.packet_id, "terminal_state": "truthful_failure"},
        key=f"packet:{packet.packet_id}:terminal",
    )
    return PacketAutodevReport(
        "truthful_failure",
        tuple(attempts),
        fallback_count,
        checkpoints,
        context_digest=context.digest,
        receipt_ids=(receipt_id,),
    )


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
    orchestrator_model: str | None = None,
    executor_model: str | None = None,
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
    # Model routes resolve from live gateway availability, never a hardcoded name.
    executor_model = executor_model or _resolve_default_executor_model()
    orchestrator_model = orchestrator_model or _resolve_default_orchestrator_model()
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
    "compile_packet_context",
    "compile_worker_context",
    "run_autodev",
]
