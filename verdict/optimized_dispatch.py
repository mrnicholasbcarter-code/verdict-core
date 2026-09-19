"""BOD-67: hydrate proof-bound worker context and bind authorized dispatch.

Architecture boundary
---------------------
* BOD-104 owns strategy via ``optimize_execution_path`` / ``ExecutionPathDecision``.
* BOD-55 owns recovery.
* BOD-67 owns hydrate-before-dispatch and binding/executing an already-authorized
  route. This module never invents model/route selection and never revives
  chooser / live_routing / AdaptiveRanker / free_tier as strategy authority.

Hydration consumes BOD-123 ContextPack surfaces (``context_intelligence``,
``context_hydrate``, ``context_pack``). Dispatch binds via ``SwarmDispatcher``
and ``serve_path.match_candidate_to_selected_route``.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, MutableMapping, Sequence
from dataclasses import dataclass
from typing import Any

from verdict.context_budget import content_looks_secret
from verdict.context_pack import (
    ContextPack,
    ContextPackCompiler,
    ContextPlan,
    ContextUnit,
    estimate_tokens,
)
from verdict.contracts import AvailabilitySnapshot
from verdict.dispatcher import DispatchResult, SwarmDispatcher
from verdict.execution_path import (
    STRATEGY_AUTHORITY,
    ExecutionPathDecision,
    ExecutionPathError,
    ExecutionPathRequest,
    optimize_execution_path,
)
from verdict.serve_path import selected_route_dispatch_identity

__all__ = [
    "DispatchReceipt",
    "HydratedWorkerPack",
    "OptimizedDispatchError",
    "WorkerReturnContract",
    "execute_optimized_dispatch",
    "hydrate_worker_context",
    "validate_worker_return",
]


class OptimizedDispatchError(ValueError):
    """Fail-closed error for hydration or authorized-dispatch binding."""


@dataclass(frozen=True)
class HydratedWorkerPack:
    """Role-specific worker pack with cache/digest provenance."""

    pack: ContextPack
    pack_digest: str
    proof_criteria_retained: tuple[str, ...]
    omissions: tuple[dict[str, str], ...]
    role: str
    cache_hit: bool
    input_digest: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "pack_digest": self.pack_digest,
            "proof_criteria_retained": list(self.proof_criteria_retained),
            "omissions": [dict(item) for item in self.omissions],
            "role": self.role,
            "cache_hit": self.cache_hit,
            "input_digest": self.input_digest,
        }


@dataclass(frozen=True)
class DispatchReceipt:
    """Inspectable receipt for an authorized dispatch bind (planning contract)."""

    task_class: str
    candidates: tuple[str, ...]
    chosen_model: str
    chosen_provider: str
    chosen_pool: str
    reason: str
    fallback_chain: tuple[str, ...]
    budget: Mapping[str, Any] | None
    context_pack_digest: str
    strategy_authority: str
    decision_digest: str
    selected_strategy: str | None = None
    dry_run: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_class": self.task_class,
            "candidates": list(self.candidates),
            "chosen_model": self.chosen_model,
            "chosen_provider": self.chosen_provider,
            "chosen_pool": self.chosen_pool,
            "reason": self.reason,
            "fallback_chain": list(self.fallback_chain),
            "budget": None if self.budget is None else dict(self.budget),
            "context_pack_digest": self.context_pack_digest,
            "strategy_authority": self.strategy_authority,
            "decision_digest": self.decision_digest,
            "selected_strategy": self.selected_strategy,
            "dry_run": self.dry_run,
        }


@dataclass(frozen=True)
class WorkerReturnContract:
    """Required worker return surface for proof-bound handoff."""

    changed_files: tuple[str, ...]
    commit: str | None
    tests: tuple[str, ...]
    commands: tuple[str, ...]
    results: Mapping[str, Any]
    fulfilled_ac: tuple[str, ...]
    unfulfilled_ac: tuple[str, ...]
    evidence: tuple[str, ...]
    uncertainty: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "changed_files": list(self.changed_files),
            "commit": self.commit,
            "tests": list(self.tests),
            "commands": list(self.commands),
            "results": dict(self.results),
            "fulfilled_ac": list(self.fulfilled_ac),
            "unfulfilled_ac": list(self.unfulfilled_ac),
            "evidence": list(self.evidence),
            "uncertainty": list(self.uncertainty),
        }


def validate_worker_return(contract: WorkerReturnContract) -> None:
    """Reject incomplete worker returns; all proof-bound fields must be present."""

    missing: list[str] = []
    if not contract.changed_files:
        missing.append("changed_files")
    if not contract.commit or not str(contract.commit).strip():
        missing.append("commit")
    if not contract.tests:
        missing.append("tests")
    if not contract.commands:
        missing.append("commands")
    if not contract.results:
        missing.append("results")
    if not contract.fulfilled_ac and not contract.unfulfilled_ac:
        missing.append("acceptance_criteria")
    if not contract.evidence:
        missing.append("evidence")
    if not contract.uncertainty:
        missing.append("uncertainty")
    if missing:
        raise OptimizedDispatchError(
            f"incomplete worker return contract: missing {', '.join(missing)}"
        )


def _canonical_digest(payload: Mapping[str, Any]) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return f"sha256:{hashlib.sha256(raw.encode()).hexdigest()}"


def _input_digest(
    *,
    role: str,
    objective: str,
    acceptance_criteria: Sequence[str],
    proof_criteria: Sequence[str],
    token_budget: int,
    input_digests: Mapping[str, str] | None,
    unit_digests: Sequence[str],
) -> str:
    return _canonical_digest(
        {
            "role": role,
            "objective": objective,
            "acceptance_criteria": list(acceptance_criteria),
            "proof_criteria": list(proof_criteria),
            "token_budget": token_budget,
            "input_digests": dict(input_digests or {}),
            "unit_digests": list(unit_digests),
        }
    )


def _role_priority(role: str, slot_type: str) -> int:
    """Lower is higher priority. Role-specific without inventing strategy."""

    base = {
        "evidence": 0,
        "instructions": 2,
        "policy": 3,
        "state": 4,
        "tools": 5,
        "memory": 6,
        "examples": 7,
        "dynamic": 8,
        "history": 9,
        "system": 1,
        "receipt": 10,
    }
    priority = base.get(slot_type, 50)
    role_l = role.lower()
    if (
        role_l in {"reviewer", "architect", "planner"} and slot_type in {"policy", "instructions"}
    ) or (role_l in {"implementer", "worker", "coder"} and slot_type in {"evidence", "tools"}):
        priority -= 1
    return priority


def _reject_secrets(units: Sequence[ContextUnit]) -> None:
    for unit in units:
        if content_looks_secret(unit.content):
            raise OptimizedDispatchError(
                f"secret_or_private_data_detected in unit {unit.unit_id!r}"
            )


def _proof_unit(proof_criteria: Sequence[str], *, role: str) -> ContextUnit:
    content = "PROOF CRITERIA (must retain):\n" + "\n".join(f"- {item}" for item in proof_criteria)
    digest = f"sha256:{hashlib.sha256(content.encode()).hexdigest()}"
    return ContextUnit(
        unit_id=f"proof:{role}",
        slot_type="evidence",
        key="proof_criteria",
        content=content,
        source_uri="urn:verdict:proof-criteria",
        source_digest=digest,
        revision="bod-67",
        trust="controller",
        authority="task_slice",
        sensitivity="public",
        tenant_scope="default",
        project_scope="default",
        confidence=1.0,
    )


def _filter_session_history(session_history: Sequence[str] | None) -> tuple[ContextUnit, ...]:
    """Search/filter/dedupe history — never inject raw session dumps wholesale."""

    if not session_history:
        return ()
    seen: set[str] = set()
    kept: list[ContextUnit] = []
    for index, turn in enumerate(session_history):
        text = " ".join(str(turn).split())
        if len(text) > 240:
            text = text[:240] + "…"
        if not text or text in seen:
            continue
        if content_looks_secret(text):
            continue
        seen.add(text)
        # Cap: at most two condensed history snippets.
        if len(kept) >= 2:
            break
        digest = f"sha256:{hashlib.sha256(text.encode()).hexdigest()}"
        kept.append(
            ContextUnit(
                unit_id=f"history-filtered:{index}",
                slot_type="history",
                key=f"history:{index}",
                content=text,
                source_uri=f"urn:verdict:history:{index}",
                source_digest=digest,
                revision="filtered",
                trust="session",
                authority="filtered",
                sensitivity="standard",
                tenant_scope="default",
                project_scope="default",
                confidence=0.4,
                transform_lineage=("history_filtered_deduped",),
            )
        )
    return tuple(kept)


def hydrate_worker_context(
    *,
    role: str,
    objective: str,
    acceptance_criteria: Sequence[str],
    proof_criteria: Sequence[str],
    units: Sequence[ContextUnit] | None = None,
    context_pack: ContextPack | None = None,
    token_budget: int = 2_000,
    session_history: Sequence[str] | None = None,
    input_digests: Mapping[str, str] | None = None,
    cache: MutableMapping[str, HydratedWorkerPack] | None = None,
    candidate_id: str = "worker",
) -> HydratedWorkerPack:
    """Build a role-specific, digest/cache-aware worker pack.

    Under tight budget, proof criteria are retained first and omissions are
    recorded. Raw session history is never injected wholesale.
    """

    if not role.strip():
        raise OptimizedDispatchError("role is required")
    if not proof_criteria:
        raise OptimizedDispatchError("proof_criteria are required")

    source_units = list(units or ())
    _reject_secrets(source_units)

    unit_digests = [unit.source_digest for unit in source_units]
    digest = _input_digest(
        role=role,
        objective=objective,
        acceptance_criteria=acceptance_criteria,
        proof_criteria=proof_criteria,
        token_budget=token_budget,
        input_digests=input_digests,
        unit_digests=unit_digests,
    )

    if cache is not None and digest in cache:
        hit = cache[digest]
        return HydratedWorkerPack(
            pack=hit.pack,
            pack_digest=hit.pack_digest,
            proof_criteria_retained=hit.proof_criteria_retained,
            omissions=hit.omissions,
            role=hit.role,
            cache_hit=True,
            input_digest=hit.input_digest,
        )

    omissions: list[dict[str, str]] = []

    if context_pack is not None:
        _reject_secrets(context_pack.units)
        pack = context_pack
        retained = tuple(c for c in proof_criteria if c in pack.compiled_prompt)
        if not retained:
            # Re-compile with mandatory proof unit when caller pack omitted proof.
            source_units = list(context_pack.units)
        else:
            hydrated = HydratedWorkerPack(
                pack=pack,
                pack_digest=pack.digest,
                proof_criteria_retained=retained or tuple(proof_criteria),
                omissions=tuple(omissions),
                role=role,
                cache_hit=False,
                input_digest=digest,
            )
            if cache is not None:
                cache[digest] = hydrated
            return hydrated

    proof = _proof_unit(proof_criteria, role=role)
    history_units = _filter_session_history(session_history)

    # Role-ordered assembly: proof first, then caller units, then filtered history.
    ordered = sorted(
        source_units, key=lambda unit: (_role_priority(role, unit.slot_type), unit.unit_id)
    )
    assembled: list[ContextUnit] = [proof, *ordered, *history_units]

    # Pre-trim optional units under tight budget while keeping proof.
    proof_cost = estimate_tokens(proof.content)
    remaining = max(token_budget - proof_cost - 8, 1)
    kept: list[ContextUnit] = [proof]
    for unit in assembled[1:]:
        cost = estimate_tokens(unit.content)
        if cost > remaining:
            omissions.append(
                {"name": unit.unit_id, "reason": "token_budget_omission", "slot": unit.slot_type}
            )
            continue
        kept.append(unit)
        remaining -= cost

    # Record wholesale history refusal when raw history was supplied but filtered.
    if session_history and len(session_history) > len(history_units):
        omissions.append(
            {
                "name": "session_history",
                "reason": "raw_history_not_injected_wholesale",
                "slot": "history",
            }
        )

    plan = ContextPlan(
        plan_id=f"hydrate:{role}:{candidate_id}",
        candidate_id=candidate_id,
        token_budget=token_budget,
        output_token_reserve=0,
        tool_token_reserve=0,
    )
    pack = ContextPackCompiler(default_token_budget=token_budget).compile_units(tuple(kept), plan)

    for decision in pack.decisions:
        if decision.action == "exclude":
            omissions.append(
                {"name": decision.unit_id, "reason": decision.reason, "slot": "excluded"}
            )

    retained = tuple(c for c in proof_criteria if c in pack.compiled_prompt)
    if not retained:
        # Fail closed: proof must survive hydration.
        raise OptimizedDispatchError("proof_criteria omitted under hydration budget")

    # Scrub secret shapes from compiled prompt / receipt fields.
    if content_looks_secret(pack.compiled_prompt):
        raise OptimizedDispatchError("secret_or_private_data_detected in compiled pack")

    hydrated = HydratedWorkerPack(
        pack=pack,
        pack_digest=pack.digest,
        proof_criteria_retained=retained,
        omissions=tuple(omissions),
        role=role,
        cache_hit=False,
        input_digest=digest,
    )
    if cache is not None:
        cache[digest] = hydrated
    return hydrated


def _resolve_decision(
    *, decision: ExecutionPathDecision | None, execution_path_request: ExecutionPathRequest | None
) -> ExecutionPathDecision:
    if decision is not None:
        if not isinstance(decision, ExecutionPathDecision):
            raise OptimizedDispatchError(
                "decision must be an ExecutionPathDecision from optimize_execution_path"
            )
        return decision
    if execution_path_request is not None:
        return optimize_execution_path(execution_path_request)
    raise OptimizedDispatchError(
        "ExecutionPathDecision required; pass decision= or execution_path_request= "
        "(offers must be caller-supplied — dispatch never invents routes)"
    )


def _fallback_chain(decision: ExecutionPathDecision) -> tuple[str, ...]:
    chain: list[str] = []
    if decision.selected_candidate_id:
        chain.append(str(decision.selected_candidate_id))
    for rejected in decision.rejected:
        label = rejected.candidate_id or rejected.strategy
        if label and label not in chain:
            chain.append(str(label))
    return tuple(chain)


def execute_optimized_dispatch(
    *,
    snapshot: AvailabilitySnapshot | Mapping[str, Any],
    decision: ExecutionPathDecision | None = None,
    execution_path_request: ExecutionPathRequest | None = None,
    task_class: str = "implementation",
    is_child: bool = False,
    explicit_model: str | None = None,
    hydrated: HydratedWorkerPack | None = None,
    dry_run: bool = True,
    dispatcher: SwarmDispatcher | None = None,
) -> tuple[DispatchResult, DispatchReceipt]:
    """Hydrate-aware bind of an already-authorized BOD-104 route.

    Planning contract: ``dry_run`` defaults to True and no live provider invoke
    is performed. Missing explicit child models are rejected.
    """

    if is_child and (explicit_model is None or not str(explicit_model).strip()):
        raise OptimizedDispatchError(
            "missing explicit child model; child workers must not inherit parent model by omission"
        )

    resolved = _resolve_decision(decision=decision, execution_path_request=execution_path_request)
    if resolved.selected_route is None or resolved.selected_strategy == "blocked":
        raise OptimizedDispatchError(
            f"no authorized route in ExecutionPathDecision "
            f"(strategy={resolved.selected_strategy!r}, why={resolved.why_selected!r})"
        )

    identity = selected_route_dispatch_identity(resolved)
    active = dispatcher or SwarmDispatcher()
    dispatch_snapshot: AvailabilitySnapshot | dict[str, Any]
    if isinstance(snapshot, AvailabilitySnapshot):
        dispatch_snapshot = snapshot
    elif isinstance(snapshot, dict):
        dispatch_snapshot = snapshot
    else:
        dispatch_snapshot = dict(snapshot)

    try:
        result = active.dispatch(dispatch_snapshot, dry_run=dry_run, selected_route=resolved)
    except ExecutionPathError as exc:
        # Named eligibility / unmatched reasons from hard gates.
        raise OptimizedDispatchError(str(exc)) from exc

    if result.selected is None:
        named = result.reason or "no eligible candidates"
        detail_parts = [named]
        for explanation in result.explanations:
            if explanation.reasons:
                detail_parts.append(f"{explanation.runtime_id}:{'|'.join(explanation.reasons)}")
        raise OptimizedDispatchError(
            "authorized route ineligible or unmatched: " + "; ".join(detail_parts)
        )

    pack_digest = hydrated.pack_digest if hydrated is not None else ""
    if hydrated is not None and content_looks_secret(hydrated.pack.compiled_prompt):
        raise OptimizedDispatchError("secret_or_private_data_detected in hydrated pack")

    candidates = tuple(
        c.runtime_id if hasattr(c, "runtime_id") else str(c) for c in (result.eligible or ())
    )
    if not candidates and isinstance(snapshot, AvailabilitySnapshot):
        # Still record observed snapshot identities for the receipt.
        ids: list[str] = []
        for item in snapshot.candidates:
            if isinstance(item, Mapping):
                rid = item.get("runtime_id")
                if rid:
                    ids.append(str(rid))
            else:
                ids.append(str(item.runtime_id))
        candidates = tuple(ids)

    route = resolved.selected_route
    receipt = DispatchReceipt(
        task_class=task_class,
        candidates=candidates
        or tuple(filter(None, (identity.get("selected_candidate_id"), identity.get("model")))),
        chosen_model=str(identity.get("model") or route.model),
        chosen_provider=str(identity.get("provider") or route.provider),
        chosen_pool=str(route.credential_pool or ""),
        reason=result.reason or resolved.why_selected,
        fallback_chain=_fallback_chain(resolved),
        budget=None if resolved.budget_state is None else dict(resolved.budget_state),
        context_pack_digest=pack_digest,
        strategy_authority=STRATEGY_AUTHORITY,
        decision_digest=resolved.decision_digest,
        selected_strategy=resolved.selected_strategy,
        dry_run=bool(result.dry_run if dry_run else dry_run),
    )

    # Receipt must never carry secret-looking material.
    receipt_blob = json.dumps(receipt.to_dict(), default=str)
    if content_looks_secret(receipt_blob):
        raise OptimizedDispatchError("secret_or_private_data_detected in dispatch receipt")

    return result, receipt
