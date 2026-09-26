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

import datetime
import errno
import hashlib
import json
import os
import subprocess
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, field, replace
from pathlib import Path, PurePosixPath
from typing import Any, cast

from verdict.context_pack import (
    ContextDecision,
    ContextPack,
    ContextPackCompiler,
    ContextPlan,
    ContextUnit,
    SlotType,
)
from verdict.decomposer import Decomposer, DecompositionConfig
from verdict.execution_packet import ExecutionPacket, capture_source_binding
from verdict.patch_executor import (
    DEFAULT_BASE_URL,
    PatchAttempt,
    PatchExecutor,
    PatchExecutorConfig,
    TokenUsage,
)
from verdict.receipt_store import ReceiptConflictError, ReceiptStore
from verdict.repository_files import (
    UnsafeRepositoryPathError,
    hold_repository_dirs,
    read_repository_bytes,
    read_repository_text,
)
from verdict.repository_files import _split as _split_repository_path
from verdict.work_unit import WorkUnit, normalize_owned_path

# Compatibility constant for legacy reporting only. Automatic launch paths do
# not use a default: they require a concrete BOD-104 selected route.
DEFAULT_EXECUTOR_MODEL = "unresolved/executor"


def _resolve_default_executor_model() -> str:
    """Reject legacy default selection; callers must provide BOD-104 authority."""
    raise AutodevError("automatic executor selection is disabled; provide an ExecutionPathDecision")


def _resolve_default_orchestrator_model() -> str:
    """Reject legacy default selection; callers must provide BOD-104 authority."""
    raise AutodevError(
        "automatic orchestrator selection is disabled; provide an ExecutionPathDecision"
    )


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
    "worker_self_report.outcome",
    "trusted_verification.decided",
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
    prior_verified_outcomes: Sequence[str] = (),
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
    add(
        "memory",
        "prior_verified_outcomes",
        prior_verified_outcomes,
        "urn:verdict:autodev:prior_verified_outcomes",
    )
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


def _enforce_delegation_floor(
    delegation: str | None,
    admitted_route: Mapping[str, Any],
    candidate_routes: Sequence[Mapping[str, Any]] | None,
    *,
    undelegable_reason: str | None,
) -> None:
    """Legwork must not spend subscription capacity when a free route is admitted (FR-031).

    This is a floor, not a preference: the whole point of delegating is that scarce paid
    frontier capacity is reserved for units that genuinely need it. A unit classified as a
    decision may use a primary route, but must name the capability that made it undelegable
    so the choice is auditable rather than self-granted.
    """
    if delegation is None:
        return
    if delegation == "decision":
        if not (undelegable_reason or "").strip():
            raise AutodevError(
                "a unit classified as a decision must name the capability that makes it "
                "undelegable before it may consume primary-subscription capacity"
            )
        return
    if delegation != "legwork":
        raise AutodevError(f"unknown delegation classification: {delegation!r}")
    if not admitted_route.get("primary"):
        return
    alternatives = [
        str(route.get("requested_identity") or route.get("actual_identity") or "")
        for route in candidate_routes or ()
        if route.get("admitted", True) and not route.get("primary")
    ]
    if alternatives:
        raise AutodevError(
            "delegable legwork may not consume primary-subscription capacity while "
            f"qualified non-primary routes are admitted: {sorted(filter(None, alternatives))}"
        )


def _require_failing_criterion(
    repo: Path, packet: ExecutionPacket, *, verification_runner: Any
) -> None:
    """Refuse a unit whose acceptance criterion is not red before the change (FR-037).

    Runs before any gateway request. A criterion that already passes cannot demonstrate
    the work happened, and one that cannot execute is not a criterion at all — dispatching
    either spends a worker on a unit whose completion could never be proven.
    """
    argv = [str(arg) for arg in packet.verification["argv"]]
    try:
        baseline = verification_runner(
            argv,
            cwd=str(repo),
            timeout=packet.verification["timeout_seconds"],
            check=False,
            capture_output=True,
            text=True,
            env={**os.environ, "PYTHONPATH": str(repo)},
        )
    except OSError as exc:
        raise AutodevError(
            f"acceptance criterion is not executable, so red-green cannot be shown: {exc}"
        ) from exc
    if baseline.returncode == 0:
        raise AutodevError(
            "acceptance criterion is already passing before the change; "
            "a red-green criterion is required before dispatching a worker"
        )


def discover_governing_docs(repo: Path, *, limit: int = 4) -> tuple[str, ...]:
    """Governing decision records for a repository, newest identifier first (FR-032).

    Reuses the shipped ADR predicate in :mod:`verdict.documentation_preflight` rather than
    inventing a second notion of what counts as authoritative. Bounded because the compiler
    budget, not this function, decides what finally fits.
    """
    from verdict.documentation_preflight import _is_adr_path

    root = repo / "docs" / "adr"
    if not root.is_dir():
        return ()
    found = sorted(
        (path for path in root.rglob("*.md") if _is_adr_path(path)),
        key=lambda path: path.name,
        reverse=True,
    )
    return tuple(str(path.relative_to(repo)) for path in found[:limit])


def compile_packet_context(
    packet: ExecutionPacket,
    repo_path: str | Path,
    *,
    repository_instruction_paths: Sequence[str] = ("AGENTS.md",),
    governing_doc_paths: Sequence[str] = (),
    relevant_example_paths: Sequence[str] = (),
    symbol_relationship: str | None = None,
    prior_verified_outcomes: Sequence[str] = (),
    token_budget: int = 4096,
    store: ReceiptStore | None = None,
    family_id: str | None = None,
) -> ContextPack:
    """Compile and optionally receipt the deterministic worker input for a packet.

    Retrieval is intentionally bounded to explicit packet-owned paths and
    caller-selected repository documents. Missing optional documents are omitted;
    no placeholder content or whole-repository scan enters the worker prompt.
    """
    repo = Path(repo_path).resolve()
    denied_paths = {
        normalize_owned_path(str(path)) for path in packet.authority.get("denied_paths", ())
    }
    denied_names = {PurePosixPath(path).name for path in denied_paths}

    source_omissions: list[tuple[str, str]] = []

    def read_selected(paths: Sequence[str], *, requested: bool = True) -> dict[str, str]:
        """Read sources, disclosing why any caller-requested one is missing (FR-032).

        ``requested`` distinguishes a source the caller named — whose absence is a broken
        promise the worker must be told about — from a default probe such as ``AGENTS.md``,
        whose absence in a repository that has none carries no information.
        """
        selected: dict[str, str] = {}
        for raw_path in paths:
            normalized = normalize_owned_path(raw_path)
            if normalized in denied_paths or PurePosixPath(normalized).name in denied_names:
                continue  # authority boundary, not a retrieval failure
            try:
                selected[normalized] = read_repository_text(repo, normalized)
            except UnsafeRepositoryPathError as exc:
                # Missing files have always been ordinary omissions, even when
                # their first parent directory does not exist. Preserve that
                # contract while surfacing symlinks and other unsafe paths.
                try:
                    (repo / normalized).lstat()
                except FileNotFoundError:
                    if requested:
                        source_omissions.append((normalized, "absent: no such file"))
                    continue
                # A symlinked source would inline host bytes into the worker
                # prompt; disclose the omission instead of following it.
                source_omissions.append((normalized, f"unsafe: {exc.reason}"))
            except FileNotFoundError:
                if requested:
                    source_omissions.append((normalized, "absent: no such file"))
            except UnicodeDecodeError:
                source_omissions.append((normalized, "unreadable: not valid utf-8"))
            except OSError as exc:
                source_omissions.append((normalized, f"unreadable: {exc.strerror or 'os error'}"))
        return selected

    owned_source = read_selected(tuple(str(path) for path in packet.authority["owned_paths"]))
    instructions = read_selected(repository_instruction_paths, requested=False)
    examples = read_selected(relevant_example_paths)
    governing_docs = read_selected(governing_doc_paths or discover_governing_docs(repo))
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
        prior_verified_outcomes=tuple(prior_verified_outcomes),
        token_budget=token_budget,
    )
    if not prior_verified_outcomes:
        # FR-032 (2026-08-27 clarification): this category has no deterministic
        # default location, so it may stay caller-supplied-only, but that gap
        # must be named rather than silently treated as complete.
        pack = replace(
            pack,
            decisions=(
                *pack.decisions,
                ContextDecision(
                    unit_id="autodev:limitation:prior_verified_outcomes",
                    action="exclude",
                    reason=(
                        "prior verified outcomes has no deterministic default location; "
                        "this run received none from the caller"
                    ),
                    input_tokens=0,
                    output_tokens=0,
                ),
            ),
        )
    if source_omissions:
        # A source the worker was meant to receive but did not is recorded, never dropped:
        # the package must state what is missing and why (FR-032).
        pack = replace(
            pack,
            decisions=(
                *pack.decisions,
                *(
                    ContextDecision(
                        unit_id=f"autodev:source:{path}",
                        action="exclude",
                        reason=reason,
                        input_tokens=0,
                        output_tokens=0,
                    )
                    for path, reason in source_omissions
                ),
            ),
        )
    if store is not None:
        with suppress(ReceiptConflictError):
            store.put_receipt(
                "context",
                "operational-loop",
                {
                    "packet_id": packet.packet_id,
                    "family_id": family_id,
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
                idempotency_key=f"packet-context:{packet.packet_id}:{family_id or '-'}:{pack.digest}",
                allowlist=_PACKET_RECEIPT_ALLOWLIST,
            )
    return pack


def _owned_path_names(packet: ExecutionPacket) -> set[str]:
    names: set[str] = set()
    for path in packet.authority.get("owned_paths", ()):
        text = str(path).strip()
        if not text:
            continue
        names.add(text)
        names.add(PurePosixPath(text).name)
    return names


def _inventory_unowned_paths(packet: ExecutionPacket, packs: Sequence[ContextPack]) -> list[str]:
    owned = _owned_path_names(packet)
    denied = [str(path) for path in packet.authority.get("denied_paths", ()) if str(path).strip()]
    found: list[str] = []

    def add(item: str) -> None:
        if item and item not in found:
            found.append(item)

    for pack in packs:
        authority_text = ""
        for unit in pack.units:
            if unit.key == "authority":
                authority_text = unit.content
                continue
            if str(unit.slot_type) not in {"evidence", "examples"}:
                continue
            candidates: list[str] = []
            if unit.source_uri and not unit.source_uri.startswith("urn:"):
                candidates.append(unit.source_uri)
            if unit.key.startswith("owned_source:"):
                candidates.append(unit.key.removeprefix("owned_source:"))
            for candidate in candidates:
                name = PurePosixPath(candidate).name
                if candidate in owned or name in owned:
                    continue
                add(name or candidate)
        prompt = pack.compiled_prompt
        if authority_text:
            prompt = prompt.replace(authority_text, "")
        for path in denied:
            base = PurePosixPath(path).name
            if path in prompt or (base and base in prompt):
                add(path)
    return found


def context_ablation_payload(
    packet: ExecutionPacket,
    pack_a: ContextPack,
    pack_b: ContextPack,
    *,
    trusted_verified_a: bool | None = None,
    trusted_verified_b: bool | None = None,
) -> dict[str, Any]:
    """Paired context packs for one packet; denied paths and identical digests refuse."""
    if pack_a.digest == pack_b.digest:
        raise AutodevError("ablation requires distinct context digests")
    denied = tuple(str(path) for path in packet.authority.get("denied_paths", ()))
    denied_names = {PurePosixPath(path).name for path in denied if path}
    for pack in (pack_a, pack_b):
        for unit in pack.units:
            if str(unit.slot_type) not in {"evidence", "examples"}:
                continue
            haystack = f"{unit.key}\n{unit.source_uri or ''}\n{unit.content}"
            if any(path and path in haystack for path in (*denied, *denied_names)):
                raise AutodevError("context pack contains denied or unowned paths")
    unowned_paths = _inventory_unowned_paths(packet, (pack_a, pack_b))
    left, right = sorted((pack_a.digest, pack_b.digest))
    material = json.dumps(
        {"a": left, "b": right, "digest": packet.integrity_digest},
        separators=(",", ":"),
        sort_keys=True,
    )

    def _leg(pack: ContextPack) -> dict[str, Any]:
        return {
            "context_digest": pack.digest,
            "used_tokens": pack.used_tokens,
            "token_budget": pack.token_budget,
            "omission_count": pack.truncated_count,
        }

    if trusted_verified_a is None or trusted_verified_b is None:
        delta = "UNKNOWN"
    elif trusted_verified_a == trusted_verified_b:
        delta = "unchanged"
    elif trusted_verified_b and not trusted_verified_a:
        delta = "improved"
    else:
        delta = "regressed"
    return {
        "pair_id": "sha256:" + hashlib.sha256(material.encode("utf-8")).hexdigest(),
        "packet_integrity_digest": packet.integrity_digest,
        "pack_a": _leg(pack_a),
        "pack_b": _leg(pack_b),
        "unowned_paths": unowned_paths,
        "unowned_paths_present": bool(unowned_paths),
        "verified_a": trusted_verified_a,
        "verified_b": trusted_verified_b,
        "success_delta": delta,
    }


def shadow_learning_report(episodes: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Advisory ranking from trusted verification labels only."""
    counts: dict[str, dict[str, Any]] = {}
    labeled = 0
    bindings: set[str] = set()
    for episode in episodes:
        digest = str(episode.get("packet_integrity_digest") or "")
        if digest:
            bindings.add(digest)
    source_binding = next(iter(sorted(bindings))) if len(bindings) == 1 else None
    for episode in episodes:
        if (
            source_binding is None
            or str(episode.get("packet_integrity_digest") or "") != source_binding
        ):
            continue
        trusted = episode.get("trusted_verification")
        if not isinstance(trusted, Mapping) or trusted.get("role") == "advisory":
            continue
        decided = trusted.get("decided")
        if not isinstance(decided, bool):
            continue
        labeled += 1
        identity = str(episode.get("actual_identity") or episode.get("requested_identity") or "")
        bucket = counts.setdefault(identity, {"identity": identity, "losses": 0, "wins": 0})
        if decided:
            bucket["wins"] += 1
        else:
            bucket["losses"] += 1
    ranking = sorted(
        counts.values(),
        key=lambda item: (-int(item["wins"]), int(item["losses"]), str(item["identity"])),
    )
    return {
        "admission_unchanged": True,
        "advisory_ranking": ranking,
        "episode_count": labeled,
        "labeled_from": "trusted_verification",
        "source_binding": source_binding,
    }


def net_savings_report(
    attempts: Sequence[Mapping[str, Any]], *, frontier_tokens: int
) -> dict[str, Any]:
    """Net extension of paid capacity: free tokens used minus frontier overhead (FR-033).

    Counting only what ran non-primary and ignoring what the frontier spent
    orchestrating and validating it would let a net loss report as a saving.
    """

    def _tokens(row: Mapping[str, Any]) -> int:
        usage = row.get("usage")
        if not isinstance(usage, Mapping):
            return 0
        return int(usage.get("prompt_tokens") or 0) + int(usage.get("completion_tokens") or 0)

    non_primary = sum(_tokens(a) for a in attempts if not a.get("primary"))
    primary = sum(_tokens(a) for a in attempts if a.get("primary"))
    return {
        "non_primary_tokens": non_primary,
        "primary_tokens": primary,
        "frontier_tokens": frontier_tokens,
        "net_savings_tokens": non_primary - frontier_tokens,
    }


def compare_execution_topologies(
    single: Sequence[Mapping[str, Any]], multi: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    """Trusted-label compare. Benefit only if multi wins more and does not cost more tokens."""

    def _tally(rows: Sequence[Mapping[str, Any]]) -> tuple[int, int]:
        success = 0
        tokens = 0
        for row in rows:
            trusted = row.get("trusted_verification")
            if (
                isinstance(trusted, Mapping)
                and trusted.get("role") != "advisory"
                and trusted.get("decided") is True
            ):
                success += 1
            usage = row.get("usage")
            if isinstance(usage, Mapping):
                with suppress(TypeError, ValueError):
                    tokens += int(usage.get("total_tokens") or 0)
        return success, tokens

    single_success, single_tokens = _tally(single)
    multi_success, multi_tokens = _tally(multi)
    return {
        "single_success": single_success,
        "multi_success": multi_success,
        "single_tokens": single_tokens,
        "multi_tokens": multi_tokens,
        "benefit": multi_success > single_success and multi_tokens <= single_tokens,
        "labeled_from": "trusted_verification",
    }


def apply_shadow_canary(admitted: Sequence[str], report: Mapping[str, Any]) -> dict[str, Any]:
    """Pick the top advisory identity that is already admitted. Does not change the gate."""
    baseline = admitted[0] if admitted else ""
    allowed = set(admitted)
    chosen = baseline
    picked = False
    wins: dict[str, int] = {}
    for row in report.get("advisory_ranking") or ():
        if not isinstance(row, Mapping):
            continue
        identity = str(row.get("identity") or "")
        wins[identity] = int(row.get("wins") or 0)
        if not picked and identity in allowed:
            chosen = identity
            picked = True
    improvement = chosen != baseline and wins.get(chosen, 0) > wins.get(baseline, 0)
    return {
        "active": True,
        "admission_unchanged": True,
        "baseline": baseline,
        "chosen": chosen,
        "improvement": improvement,
    }


def rollback_shadow_canary(canary: Mapping[str, Any]) -> dict[str, Any]:
    """Restore the pre-canary baseline choice."""
    baseline = str(canary.get("baseline") or "")
    return {
        "active": False,
        "admission_unchanged": True,
        "baseline": baseline,
        "chosen": baseline,
        "improvement": False,
    }


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
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    untracked = subprocess.run(
        ["git", "-C", str(repo), "ls-files", "--others", "--exclude-standard"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    return tuple(sorted(set(tracked + untracked)))


def _attempt_digest(repo: Path, paths: tuple[str, ...]) -> str:
    digest = hashlib.sha256()
    for path in paths:
        digest.update(path.encode())
        # A path deleted by the attempt still contributes its label; only its
        # bytes are absent.
        with suppress(FileNotFoundError):
            digest.update(read_repository_bytes(repo, path))
    return f"sha256:{digest.hexdigest()}"


def _make_attempt_worktree(repo: Path, commit: str) -> Path:
    path = Path(tempfile.mkdtemp(prefix="verdict-autodev-attempt-"))
    path.rmdir()
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "add", "--detach", str(path), commit],
        capture_output=True,
        text=True,
        check=True,
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
        ["git", "-C", str(repo), "worktree", "prune"], capture_output=True, text=True, check=False
    )


def _replay_attempt(attempt_repo: Path, repo: Path) -> None:
    untracked = subprocess.run(
        ["git", "-C", str(attempt_repo), "ls-files", "-z", "--others", "--exclude-standard"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split("\0")
    untracked = [relpath for relpath in untracked if relpath]
    # Validate every untracked source and destination parent BEFORE any byte
    # lands in the main repo: a mid-replay refusal must not leave a partially
    # replayed attempt behind.
    replayed: list[tuple[str, bytes]] = []
    for relpath in untracked:
        try:
            payload = read_repository_bytes(attempt_repo, relpath)
        except UnsafeRepositoryPathError as exc:
            raise AutodevError(
                f"refusing to replay untracked file {relpath}: {exc.reason}"
            ) from exc
        except FileNotFoundError:
            continue  # vanished between listing and read; nothing to replay
        replayed.append((relpath, payload))
    for relpath, _ in replayed:
        _preflight_replay_destination(repo, relpath)

    diff = subprocess.run(
        ["git", "-C", str(attempt_repo), "diff", "--binary", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    if diff:
        subprocess.run(
            ["git", "-C", str(repo), "apply", "--whitespace=nowarn", "-"],
            input=diff,
            capture_output=True,
            text=True,
            check=True,
        )
    # `git diff` never contains untracked files; a worker-created test file was
    # verified in the attempt and must not silently vanish on replay.
    for relpath, payload in replayed:
        _create_confined_file(repo, relpath, payload)


def _preflight_replay_destination(repo: Path, relpath: str) -> None:
    """Refuse a replay destination before any byte lands in the main repo.

    Existing parent components must be real directories (a symlinked parent
    would redirect the write outside the repo); components that do not exist
    yet are fine because :func:`_create_confined_file` creates them confined.
    The leaf must not exist at all: replay only ever adds worker-created
    files, so a pre-existing destination is a refusal, not an overwrite.
    """
    _split_repository_path(relpath)
    try:
        with hold_repository_dirs(repo, relpath) as (parent_fd, leaf, _):
            try:
                existing = os.open(leaf, getattr(os, "O_PATH", 0) | os.O_NOFOLLOW, dir_fd=parent_fd)
            except OSError as exc:
                if exc.errno == errno.ENOENT:
                    return  # the only acceptable destination state
                raise UnsafeRepositoryPathError(relpath, f"cannot inspect leaf {leaf!r}") from exc
            os.close(existing)
            raise UnsafeRepositoryPathError(relpath, f"destination {leaf!r} already exists")
    except FileNotFoundError:
        return  # a missing parent/leaf is created only at confined write time


def _create_confined_file(repo: Path, relpath: str, payload: bytes) -> None:
    """Create ``relpath`` under ``repo`` without following any symlink.

    Missing parent directories are created component-wise between held
    descriptors, and the leaf is opened O_CREAT|O_EXCL so a pre-existing
    file or a symlink swapped in after validation can never be overwritten.
    """
    parts = _split_repository_path(relpath)
    fds: list[int] = []
    try:
        fds.append(os.open(str(repo), getattr(os, "O_PATH", 0) | os.O_DIRECTORY))
        for component in parts[:-1]:
            try:
                fds.append(
                    os.open(
                        component,
                        getattr(os, "O_PATH", 0) | os.O_DIRECTORY | os.O_NOFOLLOW,
                        dir_fd=fds[-1],
                    )
                )
            except OSError as exc:
                if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                    # ENOTDIR: O_NOFOLLOW met a symlinked directory component.
                    raise UnsafeRepositoryPathError(
                        relpath, f"symlink in path component {component!r}", symlink=True
                    ) from exc
                if exc.errno == errno.ENOENT:
                    os.mkdir(component, 0o755, dir_fd=fds[-1])
                    fds.append(
                        os.open(
                            component,
                            getattr(os, "O_PATH", 0) | os.O_DIRECTORY | os.O_NOFOLLOW,
                            dir_fd=fds[-1],
                        )
                    )
                    continue
                raise UnsafeRepositoryPathError(
                    relpath, f"cannot open path component {component!r}"
                ) from exc
        leaf = parts[-1]
        try:
            fd = os.open(
                leaf, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644, dir_fd=fds[-1]
            )
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise UnsafeRepositoryPathError(
                    relpath, f"symlink at leaf {leaf!r}", symlink=True
                ) from exc
            if exc.errno == errno.EEXIST:
                raise UnsafeRepositoryPathError(
                    relpath, f"destination {leaf!r} already exists"
                ) from exc
            raise
        try:
            os.write(fd, payload)
        finally:
            os.close(fd)
    finally:
        for fd in reversed(fds):
            os.close(fd)


def designated_primary_fallback(
    route_id: str,
    *,
    evidence_digest: str,
    actual_identity: str | None = None,
    admitted: bool = True,
) -> dict[str, Any]:
    """Build the one allowed primary-subscription fallback record.

    The caller designates which live route currently occupies that role.
    The record always carries ``primary=True``; brand prefixes are not used.
    """
    if not route_id.strip():
        raise ValueError("primary fallback requires a concrete route identity")
    if not evidence_digest.startswith("sha256:"):
        raise ValueError("primary fallback requires source-linked evidence digest")
    return {
        "requested_identity": route_id,
        "actual_identity": actual_identity or route_id,
        "admitted": admitted,
        "evidence_digest": evidence_digest,
        "primary": True,
    }


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
    execution_path_decision: Any = None,
    executor_factory: Any = _default_packet_executor_factory,
    store: ReceiptStore | None = None,
    verification_runner: Any = subprocess.run,
    fallback_route: Mapping[str, Any] | None = None,
    refresh_fallback: Callable[[PacketAttempt], Any] | None = None,
    replan_execution_path: Callable[[PacketAttempt], Any] | None = None,
    classify_failure: Callable[[PacketAttempt], str | None] = _default_failure_class,
    resume: bool = False,
    worker_evidence: Mapping[str, Any] | None = None,
    token_budget: int = 4096,
    symbol_relationship: str | None = None,
    catalog_rows: Sequence[Mapping[str, Any]] | None = None,
    probe_transport: Any = None,
    candidate_routes: Sequence[Mapping[str, Any]] | None = None,
    canary_state: Mapping[str, Any] | None = None,
    require_red_green: bool = False,
    delegation: str | None = None,
    undelegable_reason: str | None = None,
    frontier_review: Callable[[PacketAttempt], str | None] | None = None,
) -> PacketAutodevReport:
    """Run one packet task only on the concrete BOD-104-authorized route."""
    decision = _require_launch_decision(execution_path_decision, surface="packet autodev")
    selected_route = decision.selected_route
    assert selected_route is not None  # established by _require_launch_decision
    selected_model = selected_route.model
    route_identity_bound = bool(
        admitted_route.get("requested_identity") or admitted_route.get("model")
    )
    _require_route_identity(admitted_route, selected_model, surface="packet autodev")
    route_provider = str(admitted_route.get("provider") or "")
    if route_provider and route_provider != selected_route.provider:
        raise AutodevError(
            "packet autodev: admitted route provider does not exactly match BOD-104 decision"
        )
    route_gateway = str(admitted_route.get("gateway") or "")
    if route_gateway and route_gateway != selected_route.gateway:
        raise AutodevError("packet autodev: admitted route gateway does not match BOD-104 decision")
    endpoint = str(admitted_route.get("base_url") or "")
    if endpoint and endpoint.rstrip("/") != selected_route.gateway.rstrip("/"):
        raise AutodevError("packet autodev: executor endpoint must be the BOD-104 selected gateway")
    admitted_route = {
        **dict(admitted_route),
        "model": selected_model,
        "provider": selected_route.provider,
        "gateway": selected_route.gateway,
        "base_url": selected_route.gateway,
        "execution_path_decision_digest": decision.decision_digest,
    }
    _enforce_delegation_floor(
        delegation, admitted_route, candidate_routes, undelegable_reason=undelegable_reason
    )
    if require_red_green:
        _require_failing_criterion(
            Path(repo_path).resolve(), packet, verification_runner=verification_runner
        )
    pending_keep: list[str] = []
    if catalog_rows is not None and probe_transport is not None and not route_identity_bound:
        from verdict.free_route_harvest import harvest_live_route

        harvested = harvest_live_route(catalog_rows, probe_transport)
        pending_keep = [str(item) for item in harvested.get("pending_keep") or ()]
        admitted_route = {**harvested, **{k: v for k, v in admitted_route.items() if v}}
        if candidate_routes is None and harvested.get("candidate_routes"):
            candidate_routes = list(harvested["candidate_routes"])
    from verdict.autodev_routing import packet_admission_inventory

    floor_routes = list(candidate_routes) if candidate_routes is not None else [admitted_route]
    try:
        admission_inventory = packet_admission_inventory(floor_routes)
    except ValueError:
        admission_inventory = {"admitted_ids": [], "ranked_ids": []}
    canary_overlay_ignored = False
    if canary_state:
        admitted = set(admission_inventory.get("admitted_ids") or ())
        overlay = str(
            (
                canary_state.get("chosen")
                if canary_state.get("active") is True
                else canary_state.get("baseline")
            )
            or ""
        )
        # Canary evidence can be recorded and audited, but it is not a route
        # selector. The exact BOD-104 route remains the only launch identity.
        if overlay in admitted and overlay not in {selected_route.route_id, selected_route.model}:
            canary_overlay_ignored = True

    def drain_remaining() -> None:
        if pending_keep and probe_transport is not None:
            from verdict.free_route_harvest import drain_keep_probes

            drain_keep_probes(pending_keep, probe_transport)

    repo = Path(repo_path).resolve()
    ledger = store or ReceiptStore(repo / ".verdict" / "receipts.db")
    from verdict.autodev_routing import family_from_base_url

    family_id = family_from_base_url(
        str(admitted_route.get("base_url") or DEFAULT_BASE_URL)
    ).family_id
    pack_tag = json.dumps(
        {"symbol": symbol_relationship or "", "token_budget": token_budget},
        separators=(",", ":"),
        sort_keys=True,
    )

    def emit(payload: dict[str, Any], *, key: str | None = None) -> str:
        from verdict.autodev_routing import unobserved_quota_headroom

        stamped = {
            **unobserved_quota_headroom(),
            **payload,
            "family_id": family_id,
            "pack_tag": pack_tag,
            "strategy_authority": "verdict.execution_path.optimize_execution_path",
            "execution_path_decision_digest": decision.decision_digest,
            "execution_path_trajectory_id": decision.trajectory_id,
            "execution_path_task_slice_id": decision.task_slice_id,
            "execution_path_strategy": decision.selected_strategy,
        }
        namespaced = None if key is None else f"{key}:{family_id}:{pack_tag}"
        return _packet_event(ledger, stamped, key=namespaced)

    records = [
        record
        for record in ledger.query_receipts(scope="operational-loop")
        if record.payload.get("packet_id") == packet.packet_id
        and record.payload.get("family_id") == family_id
        and record.payload.get("pack_tag") == pack_tag
    ]
    if resume:
        terminal = next(
            (r.payload.get("terminal_state") for r in records if r.payload.get("terminal_state")),
            None,
        )
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
        repo,
        repository=str(packet.source["repository"]),
        lock_paths=tuple(packet.source["lock_digests"]),
    )
    if dict(current) != dict(packet.source):
        emit(
            {
                "packet_id": packet.packet_id,
                "terminal_state": "drifted",
                "reason": "source binding mismatch",
            },
            key=f"packet:{packet.packet_id}:drifted",
        )
        return PacketAutodevReport("drifted")
    if not _route_is_admitted(admitted_route):
        emit(
            {
                "packet_id": packet.packet_id,
                "terminal_state": "no_eligible_route",
                "reason": "route was not admitted or was opaque",
            }
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
            emit(
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
            receipt_id = emit(blocked_payload, key=f"packet:{packet.packet_id}:blocked")
            return PacketAutodevReport(
                "blocked_no_qualified_worker",
                checkpoints={},
                context_digest=None,
                receipt_ids=(receipt_id,),
            )

    context = compile_packet_context(
        packet,
        repo,
        store=ledger,
        family_id=family_id,
        token_budget=token_budget,
        symbol_relationship=symbol_relationship,
    )
    unit = _packet_work_unit(packet, context.compiled_prompt)

    checkpoint = emit(
        {
            "packet_id": packet.packet_id,
            "checkpoint": "before_inference",
            "state": "attempt_started",
            "canary_ignored": canary_overlay_ignored,
        },
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
        # Re-check at the final provider boundary. Harvest/canary/fallback data
        # may enrich evidence, but it cannot change BOD-104's exact route.
        identity = str(
            route.get("model")
            or route.get("actual_identity")
            or route.get("requested_identity")
            or ""
        )
        provider = str(route.get("provider") or "")
        gateway = str(route.get("gateway") or route.get("base_url") or "")
        if identity != selected_route.model:
            raise AutodevError("packet autodev: launch model drifted from BOD-104 decision")
        if provider != selected_route.provider:
            raise AutodevError("packet autodev: launch provider drifted from BOD-104 decision")
        if gateway.rstrip("/") != selected_route.gateway.rstrip("/"):
            raise AutodevError("packet autodev: launch gateway drifted from BOD-104 decision")
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
            worker_outcome = str(getattr(result, "outcome", "") or "unknown")
            worker_claimed_applied = not outside and worker_outcome == "applied"
            verified = worker_claimed_applied
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
            if verified and frontier_review is not None:
                # Reject-only (FR-013): a review may only turn a pass into a failure by
                # naming a reason. It can never manufacture success from a failed attempt,
                # and trusted verification remains the sole decider of a pass.
                rejection = frontier_review(
                    PacketAttempt(
                        str(route.get("requested_identity", "")),
                        str(route.get("actual_identity", "")),
                        (),
                        "",
                        verified,
                        reason,
                    )
                )
                if rejection:
                    verified = False
                    reason = rejection
            changed_owned = tuple(sorted(set(changed) & set(owned)))
            requested_identity = str(
                route.get("requested_identity", getattr(result, "model", "unknown"))
            )
            served = getattr(result, "resolved_model", None)
            if isinstance(served, str) and served.strip():
                actual_identity = served
            else:
                actual_identity = str(
                    route.get("actual_identity") or getattr(result, "model", None) or "unknown"
                )
            from verdict.relay import fatal_identity_mismatch

            if fatal_identity_mismatch(selected_route.model, actual_identity):
                verified = False
                reason = (
                    "provider identity mismatch: BOD-104 selected "
                    f"{selected_route.model!r}, served {actual_identity!r}"
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
            attempt = PacketAttempt(**{**provisional.__dict__, "failure_class": failure_class})
            attempts.append(attempt)
            from verdict.autodev_routing import unobserved_quota_headroom

            emit(
                {
                    "packet_id": packet.packet_id,
                    "packet_integrity_digest": packet.integrity_digest,
                    "attempt": index + 1,
                    "requested_identity": attempt.requested_identity,
                    "actual_identity": attempt.actual_identity,
                    "changed_files": list(attempt.changed_files),
                    "artifact_digest": attempt.artifact_digest,
                    "verified": verified,
                    "worker_self_report": {"outcome": worker_outcome, "role": "advisory"},
                    "trusted_verification": {"decided": verified, "role": "deciding"},
                    "reason": reason,
                    "failure_class": failure_class,
                    "usage": attempt.usage.to_dict(),
                    "latency_ms": attempt.latency_ms,
                    "route": dict(route),
                    "admitted_ids": list(admission_inventory["admitted_ids"]),
                    "ranked_ids": list(admission_inventory["ranked_ids"]),
                    **unobserved_quota_headroom(),
                }
            )
            if verified:
                replay_source = capture_source_binding(
                    repo,
                    repository=str(packet.source["repository"]),
                    lock_paths=tuple(packet.source["lock_digests"]),
                )
                if dict(replay_source) != dict(packet.source):
                    drain_remaining()
                    receipt_id = emit(
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
                drain_remaining()
                receipt_id = emit(
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

        if index == 0 and failure_class is not None and replan_execution_path is not None:
            # Recovery cannot append a selector-picked fallback. The caller must
            # obtain a fresh BOD-104 decision after classifying the failure.
            replan_started_at = datetime.datetime.now(datetime.timezone.utc)
            refreshed = replan_execution_path(attempt)
            if isinstance(refreshed, tuple) and len(refreshed) == 2:
                next_decision_raw, next_route_raw = refreshed
                next_decision = _require_launch_decision(
                    next_decision_raw, surface="packet autodev recovery"
                )
                next_selected = next_decision.selected_route
                assert next_selected is not None
                if next_selected.route_id == selected_route.route_id:
                    raise AutodevError("packet recovery decision must select a new route")
                if (
                    next_decision.trajectory_id != decision.trajectory_id
                    or next_decision.task_slice_id != decision.task_slice_id
                ):
                    raise AutodevError(
                        "packet recovery decision must stay bound to the failed task trajectory"
                    )
                if not _decision_is_fresher(next_decision, decision, after=replan_started_at):
                    raise AutodevError(
                        "packet recovery decision must be a fresh re-plan issued after "
                        "the failed decision"
                    )
                next_route = dict(next_route_raw)
                if not _route_is_admitted(next_route, fallback=True):
                    # A recovery candidate that fails the primary-role fallback
                    # floor is refused, never launched: bounded recovery ends in
                    # truthful failure (main-era semantics; BOD-55).
                    index += 1
                    continue
                _require_route_identity(
                    next_route, next_selected.model, surface="packet autodev recovery"
                )
                if str(next_route.get("provider") or "") != next_selected.provider:
                    raise AutodevError(
                        "packet recovery provider does not match fresh BOD-104 decision"
                    )
                if str(next_route.get("gateway") or next_route.get("base_url") or "").rstrip(
                    "/"
                ) != next_selected.gateway.rstrip("/"):
                    raise AutodevError(
                        "packet recovery gateway does not match fresh BOD-104 decision"
                    )
                next_route.update(
                    {
                        "model": next_selected.model,
                        "provider": next_selected.provider,
                        "gateway": next_selected.gateway,
                        "base_url": next_selected.gateway,
                        "execution_path_decision_digest": next_decision.decision_digest,
                    }
                )
                # The next loop turn uses the newly authorized exact route.
                routes.append(next_route)
                fallback_count = 1
                decision = next_decision
                selected_route = next_selected
                with suppress(ValueError):
                    admission_inventory = packet_admission_inventory(routes)
        index += 1

    drain_remaining()
    receipt_id = emit(
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


def refuse_opaque_family_route(route: Mapping[str, Any]) -> None:
    """Family runs require a concrete route, not auto/combo aliases."""
    from verdict.availability import is_opaque_route_id

    identities = (
        str(route.get("requested_identity") or ""),
        str(route.get("actual_identity") or ""),
    )
    if any(value and is_opaque_route_id(value) for value in identities):
        raise AutodevError("family run refuses opaque auto/combo identities")
    if str(route.get("owned_by") or "").strip().lower() == "combo":
        raise AutodevError("family run refuses owned_by=combo identities")


def packet_family_run_payload(
    packet: ExecutionPacket, report: PacketAutodevReport, base_url: str
) -> dict[str, Any]:
    """Paired-family JSON: same packet digest, family taken from base_url."""
    from verdict.autodev_routing import family_from_base_url, unobserved_quota_headroom

    family = family_from_base_url(base_url)
    attempt = report.attempts[-1] if report.attempts else None
    return {
        "packet_integrity_digest": packet.integrity_digest,
        "family_id": family.family_id,
        "base_url": family.base_url,
        "adapter_id": family.adapter_id,
        "adapter_version": family.adapter_version,
        "protocol": family.protocol,
        "terminal_state": report.terminal_state,
        "fallback_count": report.fallback_count,
        "receipt_ids": list(report.receipt_ids),
        "requested_identity": "" if attempt is None else attempt.requested_identity,
        "actual_identity": "" if attempt is None else attempt.actual_identity,
        "proof_level": "live-proven" if report.terminal_state == "completed" else "not-completed",
        **unobserved_quota_headroom(),
        "resumed": report.resumed,
        "checkpoints": report.checkpoints,
    }


_FAMILY_LEG_KEYS = (
    "family_id",
    "base_url",
    "adapter_id",
    "adapter_version",
    "protocol",
    "terminal_state",
    "fallback_count",
    "receipt_ids",
    "requested_identity",
    "actual_identity",
    "proof_level",
    "quota",
    "headroom",
)


def compare_family_runs(
    family_a: Mapping[str, Any],
    family_b: Mapping[str, Any],
    *,
    packet: ExecutionPacket | None = None,
) -> dict[str, Any]:
    """Compare two family-run JSON objects; refuse digest mismatch or same URL."""
    digest_a = str(family_a.get("packet_integrity_digest") or "")
    digest_b = str(family_b.get("packet_integrity_digest") or "")
    if not digest_a or digest_a != digest_b:
        raise AutodevError("paired family runs require the same packet_integrity_digest")
    if packet is not None and packet.integrity_digest != digest_a:
        raise AutodevError("pair digest does not match packet")
    url_a = str(family_a.get("base_url") or "")
    url_b = str(family_b.get("base_url") or "")
    if not url_a or url_a == url_b:
        raise AutodevError("paired family runs require distinct base_url values")
    for leg in (family_a, family_b):
        if int(leg.get("fallback_count") or 0) > 1:
            raise AutodevError("paired family runs require fallback_count <= 1")
    unknown: list[dict[str, Any]] = []
    for name in ("quota", "headroom"):
        status_a = str(family_a.get(name) or "UNKNOWN")
        status_b = str(family_b.get(name) or "UNKNOWN")
        if status_a != "observed" or status_b != "observed":
            unknown.append({"facet": name, "family_a": status_a, "family_b": status_b})
    left, right = sorted((url_a, url_b))
    material = json.dumps(
        {"a": left, "b": right, "digest": digest_a}, separators=(",", ":"), sort_keys=True
    )
    return {
        "pair_id": "sha256:" + hashlib.sha256(material.encode("utf-8")).hexdigest(),
        "packet_integrity_digest": digest_a,
        "family_a": {key: family_a.get(key) for key in _FAMILY_LEG_KEYS},
        "family_b": {key: family_b.get(key) for key in _FAMILY_LEG_KEYS},
        "unknown_facets": unknown,
        "parity_claimed": not unknown,
    }


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


def _require_route_identity(
    record: Mapping[str, Any], selected_model: str, *, surface: str
) -> None:
    """Check all concrete identities; a matching alias cannot mask a mismatch."""

    for field_name in ("model", "actual_identity"):
        value = str(record.get(field_name) or "")
        if value and value != selected_model:
            raise AutodevError(
                f"{surface}: admitted route {field_name} does not exactly match "
                f"BOD-104 selected model {selected_model!r}"
            )
    requested_alias = str(record.get("requested_identity") or "")
    served_identity = str(record.get("actual_identity") or "")
    if requested_alias and requested_alias != selected_model and served_identity != selected_model:
        raise AutodevError(
            f"{surface}: admitted route model does not exactly match "
            f"BOD-104 selected model {selected_model!r}"
        )


def _decision_is_fresher(
    candidate: Any, previous: Any, *, after: datetime.datetime | None = None
) -> bool:
    """Require a recovery decision timestamp after both authority and re-plan start."""

    def parse(value: Any) -> datetime.datetime:
        return datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00"))

    try:
        candidate_at = parse(candidate.freshness.get("decision_at"))
        previous_at = parse(previous.freshness.get("decision_at"))
        if after is not None:
            if after.tzinfo is None or candidate_at <= after:
                return False
            # Optimizer timestamps come from the caller's request. Reject cached
            # or fabricated far-future timestamps as well as pre-replan receipts.
            latest_allowed = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(
                seconds=5
            )
            if candidate_at > latest_allowed:
                return False
    except (AttributeError, TypeError, ValueError, OverflowError):
        return False
    return candidate_at > previous_at


def _require_launch_decision(decision: Any, *, surface: str) -> Any:
    """Require BOD-104 authority and return its concrete launch route."""
    from verdict.execution_path import ExecutionPathDecision, ExecutionPathError
    from verdict.serve_path import require_serve_path_decision
    from verdict.session_economics import ConcreteRoute

    if not isinstance(decision, ExecutionPathDecision):
        raise AutodevError(
            f"{surface}: missing ExecutionPathDecision; automatic workers may launch only "
            "from a trusted BOD-104 decision"
        )
    try:
        trusted = require_serve_path_decision(decision, surface=surface)
    except ExecutionPathError as exc:
        raise AutodevError(str(exc)) from exc
    route = trusted.selected_route
    if not isinstance(route, ConcreteRoute):
        raise AutodevError(f"{surface}: decision must contain a concrete provider/model route")
    if route.hard_ineligible:
        raise AutodevError(f"{surface}: BOD-104 selected route is hard-ineligible")
    if trusted.selected_candidate_id != route.route_id:
        raise AutodevError(f"{surface}: decision candidate does not match its concrete route")
    if not route.provider.strip() or not route.model.strip() or not route.gateway.strip():
        raise AutodevError(f"{surface}: decision route requires exact gateway/provider/model")
    if not trusted.decision_digest or not trusted.trajectory_id or not trusted.task_slice_id:
        raise AutodevError(f"{surface}: decision is missing its digest or task binding")
    from verdict.execution_path import _digest

    decision_at = trusted.freshness.get("decision_at")
    expected_cost = trusted.expected_cost
    if not isinstance(decision_at, str) or expected_cost is None:
        raise AutodevError(f"{surface}: decision is missing optimizer receipt evidence")
    receipt_body = {
        "trajectory_id": trusted.trajectory_id,
        "task_slice_id": trusted.task_slice_id,
        "selected_strategy": trusted.selected_strategy,
        "selected_candidate_id": trusted.selected_candidate_id,
        "route": route.to_dict(),
        "assistance_plan_digest": trusted.assistance_plan_digest,
        "expected_cost": expected_cost.to_dict(),
        "rejected": [item.to_dict() for item in trusted.rejected],
        "selection_reason": trusted.strategy_selection_reason,
        "when": decision_at,
    }
    if _digest(receipt_body) != trusted.decision_digest:
        raise AutodevError(f"{surface}: ExecutionPathDecision receipt digest is invalid")
    return trusted


def _assert_exact_model(configured: str | None, selected: str, *, surface: str) -> None:
    # The decision itself supplies the mandatory exact model. A legacy CLI
    # argument may be omitted, but if supplied it cannot override that route.
    if configured is not None and configured != selected:
        raise AutodevError(
            f"{surface}: configured model {configured!r} does not match "
            f"BOD-104 selected model {selected!r}"
        )


def run_autodev(
    objective: str,
    repo_path: str | Path,
    *,
    execution_path_decision: Any = None,
    orchestrator_model: str | None = None,
    executor_model: str | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
    store: ReceiptStore | None = None,
    decomposer: Decomposer | None = None,
    executor: PatchExecutor | None = None,
    evidence: str = "",
    mechanical: bool = True,
    runner: Any = subprocess.run,
) -> AutodevReport:
    """Decompose ``objective``, execute each unit, verify it, and record it."""
    # BOD-104 is the only strategy/route authority for automatic worker launch.
    # Resolve the decision before constructing a provider-backed executor.
    decision = _require_launch_decision(execution_path_decision, surface="autodev")
    selected_route = decision.selected_route
    assert selected_route is not None  # established by _require_launch_decision
    _assert_exact_model(executor_model, selected_route.model, surface="autodev executor")
    executor_model = selected_route.model
    if base_url is not None and base_url.rstrip("/") != selected_route.gateway.rstrip("/"):
        raise AutodevError(
            "autodev: configured gateway does not match BOD-104 selected gateway "
            f"{selected_route.gateway!r}"
        )
    base_url = selected_route.gateway
    if executor is not None:
        _assert_exact_model(executor.config.model, selected_route.model, surface="autodev executor")
        if executor.config.base_url.rstrip("/") != selected_route.gateway.rstrip("/"):
            raise AutodevError("autodev: injected executor gateway does not match BOD-104 decision")
    if decomposer is not None:
        _assert_exact_model(
            decomposer.config.model, selected_route.model, surface="autodev orchestrator"
        )
        if decomposer.config.base_url.rstrip("/") != selected_route.gateway.rstrip("/"):
            raise AutodevError(
                "autodev: injected decomposer gateway does not match BOD-104 decision"
            )
    _assert_exact_model(orchestrator_model, selected_route.model, surface="autodev orchestrator")
    orchestrator_model = selected_route.model
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
                "strategy_authority": "verdict.execution_path.optimize_execution_path",
                "execution_path_decision_digest": decision.decision_digest,
                "execution_path_trajectory_id": decision.trajectory_id,
                "execution_path_task_slice_id": decision.task_slice_id,
                "execution_path_strategy": decision.selected_strategy,
                "execution_path_selected_route": decision.selected_route.to_dict(),
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


def _is_ruff_command(cmd: Sequence[str]) -> bool:
    """Return True if cmd is a ruff command (either 'ruff' or [python, '-m', 'ruff'])."""
    if not cmd:
        return False
    return cmd[0] == "ruff" or (len(cmd) >= 3 and cmd[1] == "-m" and cmd[2] == "ruff")


def _try_mechanical(unit: WorkUnit, repo: Path, *, runner: Any) -> bool:
    """Attempt a deterministic fix, returning whether the tier claims the unit.

    Only ``ruff``-verified units qualify, and the fixer is scoped to the files
    the unit owns so it cannot repair another unit's work.  Returning True means
    the tier ran and the unit's command now passes; verification still confirms
    it independently.
    """
    if not _is_ruff_command(unit.verification_command):
        return False
    # Build fixer argv using the same interpreter as the verification command
    cmd = unit.verification_command
    prefix = list(cmd[:3]) if len(cmd) >= 3 and cmd[1] == "-m" and cmd[2] == "ruff" else ["ruff"]
    fix = _run([*prefix, "check", "--fix", "--", *unit.owned_files], repo, runner=runner)
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
    "apply_shadow_canary",
    "collect_ruff_evidence",
    "compare_execution_topologies",
    "compile_packet_context",
    "compile_worker_context",
    "context_ablation_payload",
    "designated_primary_fallback",
    "rollback_shadow_canary",
    "run_autodev",
    "shadow_learning_report",
]
