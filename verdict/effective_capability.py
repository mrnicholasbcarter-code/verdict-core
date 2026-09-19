"""Effective Capability Planner — model + context + tools + decomposition (BOD-120).

For a concrete task slice, effective capability is approximately:

    model capability + supplied context + available tools
    + decomposition quality + verification/recovery support

This planner sits after Candidate Pool Intelligence (BOD-122) and Context
Intelligence (BOD-123), and before expected-cost / strategy selection (BOD-104).

It is deterministic planning over explicit requirements and evidence — never a
learned agentic score. ``unknown`` never becomes ``sufficient`` by optimism.
Hard-gate-excluded candidates never enter assisted qualification.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final, Literal

from verdict.capability_gate import TaskRequirements

SCHEMA_VERSION: Final[str] = "effective-capability/v1"

Sufficiency = Literal["sufficient", "insufficient", "unknown"]

# Intrinsic caps that context/tools cannot repair for the same candidate step.
_HARD_INTRINSIC: Final[frozenset[str]] = frozenset({"vision", "attachment"})

# Soft / assistable deficits (repo knowledge, docs, memory) — ContextPack may fix.
_ASSISTABLE_CONTEXT_HINTS: Final[frozenset[str]] = frozenset(
    {"evidence", "instructions", "memory", "policy", "state", "examples", "history"}
)

_DEFAULT_PLANNING_TOKENS: Final[int] = 256
_DEFAULT_VERIFICATION_TOKENS: Final[int] = 128
_DEFAULT_TOOL_TOKENS_PER_CAP: Final[int] = 64


class EffectiveCapabilityError(ValueError):
    """Raised when planner inputs violate the contract."""


@dataclass(frozen=True)
class TaskSlice:
    """Bounded work unit with acceptance and proof criteria."""

    slice_id: str
    objective: str
    acceptance_criteria: tuple[str, ...] = ()
    proof_criteria: tuple[str, ...] = ()
    parent_slice_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.slice_id, str) or not self.slice_id.strip():
            raise EffectiveCapabilityError("slice_id must be a non-empty string")
        if not isinstance(self.objective, str) or not self.objective.strip():
            raise EffectiveCapabilityError("objective must be a non-empty string")
        object.__setattr__(
            self, "acceptance_criteria", tuple(str(item) for item in self.acceptance_criteria)
        )
        object.__setattr__(self, "proof_criteria", tuple(str(item) for item in self.proof_criteria))

    def to_dict(self) -> dict[str, Any]:
        return {
            "slice_id": self.slice_id,
            "objective": self.objective,
            "acceptance_criteria": list(self.acceptance_criteria),
            "proof_criteria": list(self.proof_criteria),
            "parent_slice_id": self.parent_slice_id,
        }


@dataclass(frozen=True)
class CandidateCapabilityEvidence:
    """Authoritative intrinsic capability evidence for one shortlisted candidate."""

    candidate_id: str
    intrinsic_capabilities: Mapping[str, bool | None]
    evidence_digest: str
    context_window: int | None = None
    hard_gate_excluded: bool = False
    hard_gate_reason: str | None = None
    observed_at: str | None = None
    freshness: str = "unknown"

    def __post_init__(self) -> None:
        if not isinstance(self.candidate_id, str) or not self.candidate_id.strip():
            raise EffectiveCapabilityError("candidate_id must be a non-empty string")
        if not isinstance(self.evidence_digest, str) or not self.evidence_digest.strip():
            raise EffectiveCapabilityError("evidence_digest must be a non-empty string")
        object.__setattr__(self, "intrinsic_capabilities", dict(self.intrinsic_capabilities))

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "intrinsic_capabilities": dict(self.intrinsic_capabilities),
            "context_window": self.context_window,
            "hard_gate_excluded": self.hard_gate_excluded,
            "hard_gate_reason": self.hard_gate_reason,
            "evidence_digest": self.evidence_digest,
            "observed_at": self.observed_at,
            "freshness": self.freshness,
        }


@dataclass(frozen=True)
class ContextEvidenceSnapshot:
    """What a candidate-specific ContextPack actually supplies right now."""

    available_slots: frozenset[str]
    available_evidence_keys: frozenset[str]
    omitted_required: frozenset[str] = frozenset()
    token_budget: int = 4096
    used_tokens: int = 0
    evidence_digest: str = ""
    observed_at: str | None = None
    fresh: bool = True
    hydrated: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "available_slots", frozenset(self.available_slots))
        object.__setattr__(self, "available_evidence_keys", frozenset(self.available_evidence_keys))
        object.__setattr__(self, "omitted_required", frozenset(self.omitted_required))
        if self.token_budget < 1:
            raise EffectiveCapabilityError("token_budget must be positive")
        if self.used_tokens < 0:
            raise EffectiveCapabilityError("used_tokens must be non-negative")

    def to_dict(self) -> dict[str, Any]:
        return {
            "available_slots": sorted(self.available_slots),
            "available_evidence_keys": sorted(self.available_evidence_keys),
            "omitted_required": sorted(self.omitted_required),
            "token_budget": self.token_budget,
            "used_tokens": self.used_tokens,
            "evidence_digest": self.evidence_digest,
            "observed_at": self.observed_at,
            "fresh": self.fresh,
            "hydrated": self.hydrated,
        }


@dataclass(frozen=True)
class ToolSurfaceSnapshot:
    """Available semantic tool/MCP capabilities and concrete selected surface."""

    available_capabilities: frozenset[str]
    selected_surface: Mapping[str, str] = field(default_factory=dict)
    evidence_digest: str = ""
    observed_at: str | None = None
    fresh: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "available_capabilities", frozenset(self.available_capabilities))
        object.__setattr__(self, "selected_surface", dict(self.selected_surface))

    def to_dict(self) -> dict[str, Any]:
        return {
            "available_capabilities": sorted(self.available_capabilities),
            "selected_surface": dict(self.selected_surface),
            "evidence_digest": self.evidence_digest,
            "observed_at": self.observed_at,
            "fresh": self.fresh,
        }


@dataclass(frozen=True)
class DecompositionProposal:
    """Frontier planner proposal: child slices that must preserve parent contract."""

    children: tuple[TaskSlice, ...]
    planner_role: str = "frontier"

    def __post_init__(self) -> None:
        object.__setattr__(self, "children", tuple(self.children))


@dataclass(frozen=True)
class DecompositionRequirement:
    required: bool
    child_slices: tuple[TaskSlice, ...] = ()
    preserves_parent_acceptance: bool = True
    preserves_parent_proof: bool = True
    reason: str = ""
    planner_role: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "required": self.required,
            "child_slices": [child.to_dict() for child in self.child_slices],
            "preserves_parent_acceptance": self.preserves_parent_acceptance,
            "preserves_parent_proof": self.preserves_parent_proof,
            "reason": self.reason,
            "planner_role": self.planner_role,
        }


@dataclass(frozen=True)
class VerificationStrategy:
    kind: str
    proof_criteria: tuple[str, ...] = ()
    commands: tuple[tuple[str, ...], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "proof_criteria": list(self.proof_criteria),
            "commands": [list(cmd) for cmd in self.commands],
        }


@dataclass(frozen=True)
class AssistanceCost:
    """Visible assistance cost for BOD-54 / BOD-104 expected-cost consumers."""

    context_tokens: int = 0
    tool_tokens: int = 0
    planning_tokens: int = 0
    verification_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return (
            self.context_tokens + self.tool_tokens + self.planning_tokens + self.verification_tokens
        )

    def to_dict(self) -> dict[str, int]:
        return {
            "context_tokens": self.context_tokens,
            "tool_tokens": self.tool_tokens,
            "planning_tokens": self.planning_tokens,
            "verification_tokens": self.verification_tokens,
            "total_tokens": self.total_tokens,
        }


@dataclass(frozen=True)
class ProvenanceClaim:
    """Provenance/freshness for one capability or evidence claim."""

    kind: str
    claim: str
    source: str
    digest: str | None = None
    observed_at: str | None = None
    freshness: str = "unknown"
    fresh: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "claim": self.claim,
            "source": self.source,
            "digest": self.digest,
            "observed_at": self.observed_at,
            "freshness": self.freshness,
            "fresh": self.fresh,
        }


@dataclass(frozen=True)
class AssistancePlan:
    """Inspectable effective-capability assistance plan for one candidate."""

    plan_id: str
    candidate_id: str
    task_slice: TaskSlice
    required_intrinsic_capabilities: tuple[str, ...]
    required_context_slots: tuple[str, ...]
    required_context_evidence: tuple[str, ...]
    required_tool_capabilities: tuple[str, ...]
    selected_tool_surface: tuple[str, ...]
    decomposition: DecompositionRequirement
    verification: VerificationStrategy
    assistance_cost: AssistanceCost
    result: Sufficiency
    reasons: tuple[str, ...]
    intrinsic_sufficient: bool
    assisted_sufficient: bool
    assistance_delta: tuple[str, ...]
    provenance: tuple[ProvenanceClaim, ...]
    evidence_digest: str
    context_plan_requirements: Mapping[str, Any] = field(default_factory=dict)
    schema_version: str = SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "plan_id": self.plan_id,
            "candidate_id": self.candidate_id,
            "task_slice": self.task_slice.to_dict(),
            "required_intrinsic_capabilities": list(self.required_intrinsic_capabilities),
            "required_context_slots": list(self.required_context_slots),
            "required_context_evidence": list(self.required_context_evidence),
            "required_tool_capabilities": list(self.required_tool_capabilities),
            "selected_tool_surface": list(self.selected_tool_surface),
            "decomposition": self.decomposition.to_dict(),
            "verification": self.verification.to_dict(),
            "assistance_cost": self.assistance_cost.to_dict(),
            "result": self.result,
            "reasons": list(self.reasons),
            "intrinsic_sufficient": self.intrinsic_sufficient,
            "assisted_sufficient": self.assisted_sufficient,
            "assistance_delta": list(self.assistance_delta),
            "provenance": [item.to_dict() for item in self.provenance],
            "evidence_digest": self.evidence_digest,
            "context_plan_requirements": dict(self.context_plan_requirements),
        }


def plan_effective_capability(
    *,
    task_slice: TaskSlice,
    candidate: CandidateCapabilityEvidence,
    requirements: TaskRequirements,
    context: ContextEvidenceSnapshot | None,
    tools: ToolSurfaceSnapshot,
    required_context_slots: Sequence[str] = (),
    required_context_evidence: Sequence[str] = (),
    required_tool_capabilities: Sequence[str] = (),
    decomposition: DecompositionProposal | None = None,
) -> AssistancePlan:
    """Produce a deterministic assistance plan for one candidate/strategy.

    Rules (fail-closed):
    - Hard-gate-excluded → ``insufficient``; never assisted qualification.
    - Unknown intrinsic evidence for a required hard/soft cap → ``unknown``.
    - Missing mandatory tool → ``insufficient`` even with rich context.
    - Required context omitted / unaided when needed → not ``sufficient``.
    - Decomposition must preserve parent AC/proof union or plan fails closed.
    """
    req_slots = tuple(str(s).strip() for s in required_context_slots if str(s).strip())
    req_evidence = tuple(str(e).strip() for e in required_context_evidence if str(e).strip())
    req_tools = tuple(str(t).strip() for t in required_tool_capabilities if str(t).strip())
    intrinsic_names = tuple(requirements.names)

    provenance = _build_provenance(candidate, context, tools)
    decomp = _evaluate_decomposition(task_slice, decomposition)
    verification = _verification_strategy(task_slice)
    cost = _assistance_cost(context, req_tools, decomp)
    context_plan_reqs = {
        "candidate_id": candidate.candidate_id,
        "required_slot_types": list(req_slots),
        "required_evidence_keys": list(req_evidence),
        "token_budget": context.token_budget if context is not None else None,
    }

    reasons: list[str] = []
    assistance_delta: list[str] = []

    # 1) Hard-gate exclusion — never assisted.
    if candidate.hard_gate_excluded:
        reasons.append(f"hard_gate_excluded:{candidate.hard_gate_reason or 'unnamed'}")
        return _finalize(
            task_slice=task_slice,
            candidate=candidate,
            intrinsic_names=intrinsic_names,
            req_slots=req_slots,
            req_evidence=req_evidence,
            req_tools=req_tools,
            tools=tools,
            context=context,
            decomp=decomp,
            verification=verification,
            cost=cost,
            context_plan_reqs=context_plan_reqs,
            provenance=provenance,
            result="insufficient",
            reasons=tuple(reasons),
            intrinsic_sufficient=False,
            assisted_sufficient=False,
            assistance_delta=(),
        )

    # 2) Intrinsic hard capabilities (vision/attachment) — not assistable.
    hard_fail, hard_unknown, hard_reasons = _check_intrinsic(
        candidate, intrinsic_names, hard_only=True
    )
    reasons.extend(hard_reasons)
    if hard_fail:
        return _finalize(
            task_slice=task_slice,
            candidate=candidate,
            intrinsic_names=intrinsic_names,
            req_slots=req_slots,
            req_evidence=req_evidence,
            req_tools=req_tools,
            tools=tools,
            context=context,
            decomp=decomp,
            verification=verification,
            cost=cost,
            context_plan_reqs=context_plan_reqs,
            provenance=provenance,
            result="insufficient",
            reasons=tuple(reasons),
            intrinsic_sufficient=False,
            assisted_sufficient=False,
            assistance_delta=(),
        )
    if hard_unknown:
        return _finalize(
            task_slice=task_slice,
            candidate=candidate,
            intrinsic_names=intrinsic_names,
            req_slots=req_slots,
            req_evidence=req_evidence,
            req_tools=req_tools,
            tools=tools,
            context=context,
            decomp=decomp,
            verification=verification,
            cost=cost,
            context_plan_reqs=context_plan_reqs,
            provenance=provenance,
            result="unknown",
            reasons=tuple(reasons),
            intrinsic_sufficient=False,
            assisted_sufficient=False,
            assistance_delta=(),
        )

    # 3) Soft intrinsic (tools/structured/context window) — unknown stays unknown.
    soft_fail, soft_unknown, soft_reasons = _check_intrinsic(
        candidate, intrinsic_names, hard_only=False
    )
    soft_reasons = [r for r in soft_reasons if r not in hard_reasons]
    reasons.extend(soft_reasons)
    if soft_unknown:
        return _finalize(
            task_slice=task_slice,
            candidate=candidate,
            intrinsic_names=intrinsic_names,
            req_slots=req_slots,
            req_evidence=req_evidence,
            req_tools=req_tools,
            tools=tools,
            context=context,
            decomp=decomp,
            verification=verification,
            cost=cost,
            context_plan_reqs=context_plan_reqs,
            provenance=provenance,
            result="unknown",
            reasons=tuple(reasons),
            intrinsic_sufficient=False,
            assisted_sufficient=False,
            assistance_delta=(),
        )

    # Context window hard floor when declared.
    if requirements.min_context is not None:
        window = candidate.context_window
        if window is None:
            reasons.append("unknown_context_window")
            return _finalize(
                task_slice=task_slice,
                candidate=candidate,
                intrinsic_names=intrinsic_names,
                req_slots=req_slots,
                req_evidence=req_evidence,
                req_tools=req_tools,
                tools=tools,
                context=context,
                decomp=decomp,
                verification=verification,
                cost=cost,
                context_plan_reqs=context_plan_reqs,
                provenance=provenance,
                result="unknown",
                reasons=tuple(reasons),
                intrinsic_sufficient=False,
                assisted_sufficient=False,
                assistance_delta=(),
            )
        if window < requirements.min_context:
            reasons.append(f"intrinsic_context_window:{window}<{requirements.min_context}")
            # Context window shortfall is intrinsic hard for this candidate.
            return _finalize(
                task_slice=task_slice,
                candidate=candidate,
                intrinsic_names=intrinsic_names,
                req_slots=req_slots,
                req_evidence=req_evidence,
                req_tools=req_tools,
                tools=tools,
                context=context,
                decomp=decomp,
                verification=verification,
                cost=cost,
                context_plan_reqs=context_plan_reqs,
                provenance=provenance,
                result="insufficient",
                reasons=tuple(reasons),
                intrinsic_sufficient=False,
                assisted_sufficient=False,
                assistance_delta=(),
            )

    # Soft tool-calling false is intrinsic insufficient (model cannot invoke tools).
    if soft_fail:
        # tools=False is hard for tool-required tasks; not repaired by context.
        return _finalize(
            task_slice=task_slice,
            candidate=candidate,
            intrinsic_names=intrinsic_names,
            req_slots=req_slots,
            req_evidence=req_evidence,
            req_tools=req_tools,
            tools=tools,
            context=context,
            decomp=decomp,
            verification=verification,
            cost=cost,
            context_plan_reqs=context_plan_reqs,
            provenance=provenance,
            result="insufficient",
            reasons=tuple(reasons),
            intrinsic_sufficient=False,
            assisted_sufficient=False,
            assistance_delta=(),
        )

    # 4) Mandatory tool/MCP capabilities — missing → insufficient despite context.
    missing_tools = tuple(cap for cap in req_tools if cap not in tools.available_capabilities)
    if missing_tools:
        reasons.append(f"missing_mandatory_tool:{','.join(missing_tools)}")
        return _finalize(
            task_slice=task_slice,
            candidate=candidate,
            intrinsic_names=intrinsic_names,
            req_slots=req_slots,
            req_evidence=req_evidence,
            req_tools=req_tools,
            tools=tools,
            context=context,
            decomp=decomp,
            verification=verification,
            cost=cost,
            context_plan_reqs=context_plan_reqs,
            provenance=provenance,
            result="insufficient",
            reasons=tuple(reasons),
            intrinsic_sufficient=False,
            assisted_sufficient=False,
            assistance_delta=(),
        )
    if req_tools and not tools.fresh:
        reasons.append("tool_surface_stale")

    selected_surface = tuple(
        tools.selected_surface[cap] for cap in req_tools if cap in tools.selected_surface
    )

    # 5) Decomposition integrity.
    if decomp.required and (
        not decomp.preserves_parent_acceptance or not decomp.preserves_parent_proof
    ):
        reasons.append(decomp.reason or "decomposition_does_not_preserve_parent_contract")
        return _finalize(
            task_slice=task_slice,
            candidate=candidate,
            intrinsic_names=intrinsic_names,
            req_slots=req_slots,
            req_evidence=req_evidence,
            req_tools=req_tools,
            tools=tools,
            context=context,
            decomp=decomp,
            verification=verification,
            cost=cost,
            context_plan_reqs=context_plan_reqs,
            provenance=provenance,
            result="insufficient",
            reasons=tuple(reasons),
            intrinsic_sufficient=False,
            assisted_sufficient=False,
            assistance_delta=(),
            selected_surface=selected_surface,
        )

    # 6) Context assistance — distinguish unaided vs assisted.
    needs_context = bool(req_slots or req_evidence)
    context_ok = True
    if needs_context:
        if context is None:
            context_ok = False
            reasons.append("missing_context_assistance")
        else:
            missing_slots = tuple(s for s in req_slots if s not in context.available_slots)
            missing_ev = tuple(e for e in req_evidence if e not in context.available_evidence_keys)
            omitted = tuple(sorted(context.omitted_required))
            if omitted:
                context_ok = False
                reasons.append(f"omitted_required_evidence:{','.join(omitted)}")
                if not context.hydrated or context.used_tokens >= context.token_budget:
                    reasons.append("context_budget_omission")
            if missing_slots:
                context_ok = False
                reasons.append(f"missing_context_slots:{','.join(missing_slots)}")
            if missing_ev:
                context_ok = False
                reasons.append(f"missing_context_evidence:{','.join(missing_ev)}")
            if not context.fresh:
                context_ok = False
                reasons.append("context_evidence_stale")
            if context_ok:
                assistance_delta.append("context_pack_requirement_complete")
                for slot in req_slots:
                    if slot in _ASSISTABLE_CONTEXT_HINTS:
                        assistance_delta.append(f"context_slot:{slot}")
                for key in req_evidence:
                    assistance_delta.append(f"context_evidence:{key}")

    # Intrinsic sufficiency: no context assistance needed AND all intrinsic ok.
    intrinsic_sufficient = (not needs_context) and not soft_fail and not hard_fail

    if needs_context and not context_ok:
        # Unaided / incomplete assistance — never optimistic success.
        # Budget omissions without hydration → unknown; explicit gaps → insufficient.
        gap_result: Sufficiency = "insufficient"
        if (
            context is not None
            and not context.hydrated
            and context.omitted_required
            and not context.available_evidence_keys
        ):
            gap_result = "unknown"
            if "unknown_context_coverage" not in reasons:
                reasons.append("unknown_context_coverage")
        return _finalize(
            task_slice=task_slice,
            candidate=candidate,
            intrinsic_names=intrinsic_names,
            req_slots=req_slots,
            req_evidence=req_evidence,
            req_tools=req_tools,
            tools=tools,
            context=context,
            decomp=decomp,
            verification=verification,
            cost=cost,
            context_plan_reqs=context_plan_reqs,
            provenance=provenance,
            result=gap_result,
            reasons=tuple(reasons),
            intrinsic_sufficient=False,
            assisted_sufficient=False,
            assistance_delta=tuple(dict.fromkeys(assistance_delta)),
            selected_surface=selected_surface,
        )

    if req_tools:
        assistance_delta.append("tool_surface_available")
    if decomp.required and decomp.preserves_parent_acceptance and decomp.preserves_parent_proof:
        assistance_delta.append("decomposition_preserves_parent_contract")

    assisted_sufficient = True
    final_result: Sufficiency
    if intrinsic_sufficient and not assistance_delta:
        final_result = "sufficient"
    elif assisted_sufficient:
        final_result = "sufficient"
        if needs_context:
            # Became sufficient because of assistance, not intrinsically.
            intrinsic_sufficient = False
    else:
        final_result = "insufficient"

    if not reasons and final_result == "sufficient":
        if intrinsic_sufficient:
            reasons.append("intrinsically_sufficient")
        else:
            reasons.append("assisted_sufficient")

    return _finalize(
        task_slice=task_slice,
        candidate=candidate,
        intrinsic_names=intrinsic_names,
        req_slots=req_slots,
        req_evidence=req_evidence,
        req_tools=req_tools,
        tools=tools,
        context=context,
        decomp=decomp,
        verification=verification,
        cost=cost,
        context_plan_reqs=context_plan_reqs,
        provenance=provenance,
        result=final_result,
        reasons=tuple(reasons),
        intrinsic_sufficient=intrinsic_sufficient,
        assisted_sufficient=assisted_sufficient and final_result == "sufficient",
        assistance_delta=tuple(dict.fromkeys(assistance_delta)),
        selected_surface=selected_surface,
    )


# ── Internals ────────────────────────────────────────────────────────────────


def _check_intrinsic(
    candidate: CandidateCapabilityEvidence, names: Sequence[str], *, hard_only: bool
) -> tuple[bool, bool, list[str]]:
    """Return (failed, unknown, reasons) for intrinsic caps."""
    failed = False
    unknown = False
    reasons: list[str] = []
    for name in names:
        is_hard = name in _HARD_INTRINSIC
        if hard_only and not is_hard:
            continue
        if not hard_only and is_hard:
            continue
        observed = candidate.intrinsic_capabilities.get(name)
        if observed is None:
            # Cap required but not present in evidence map → unknown.
            if name not in candidate.intrinsic_capabilities:
                # Also treat missing key as unknown when listed in requirements.
                unknown = True
                reasons.append(f"unknown_intrinsic:{name}")
            else:
                unknown = True
                reasons.append(f"unknown_intrinsic:{name}")
            continue
        if observed is False:
            failed = True
            tag = "intrinsic_hard" if is_hard else "intrinsic"
            reasons.append(f"{tag}_deficit:{name}")
    return failed, unknown, reasons


def _evaluate_decomposition(
    parent: TaskSlice, proposal: DecompositionProposal | None
) -> DecompositionRequirement:
    if proposal is None or not proposal.children:
        return DecompositionRequirement(required=False, reason="no_decomposition")

    child_ac = {item for child in proposal.children for item in child.acceptance_criteria}
    child_proof = {item for child in proposal.children for item in child.proof_criteria}
    parent_ac = set(parent.acceptance_criteria)
    parent_proof = set(parent.proof_criteria)
    preserves_ac = parent_ac <= child_ac if parent_ac else True
    preserves_proof = parent_proof <= child_proof if parent_proof else True
    reason = ""
    if not preserves_ac:
        missing = sorted(parent_ac - child_ac)
        reason = f"decomposition_missing_acceptance:{','.join(missing)}"
    elif not preserves_proof:
        missing = sorted(parent_proof - child_proof)
        reason = f"decomposition_missing_proof:{','.join(missing)}"
    else:
        reason = "decomposition_preserves_parent_contract"
    return DecompositionRequirement(
        required=True,
        child_slices=proposal.children,
        preserves_parent_acceptance=preserves_ac,
        preserves_parent_proof=preserves_proof,
        reason=reason,
        planner_role=proposal.planner_role,
    )


def _verification_strategy(task_slice: TaskSlice) -> VerificationStrategy:
    proofs = task_slice.proof_criteria
    if not proofs:
        return VerificationStrategy(kind="none")
    commands: list[tuple[str, ...]] = []
    for proof in proofs:
        # Treat shell-like proof strings as argv-ish for cost consumers.
        parts = tuple(part for part in proof.split() if part)
        if parts:
            commands.append(parts)
    return VerificationStrategy(
        kind="proof_criteria", proof_criteria=proofs, commands=tuple(commands)
    )


def _assistance_cost(
    context: ContextEvidenceSnapshot | None,
    req_tools: Sequence[str],
    decomp: DecompositionRequirement,
) -> AssistanceCost:
    context_tokens = context.used_tokens if context is not None else 0
    tool_tokens = _DEFAULT_TOOL_TOKENS_PER_CAP * len(req_tools)
    planning_tokens = _DEFAULT_PLANNING_TOKENS if decomp.required else 0
    # Context/tool/planning assistance is never free for expected-cost consumers.
    if context is not None or req_tools or decomp.required:
        verification_tokens = _DEFAULT_VERIFICATION_TOKENS
    else:
        verification_tokens = 0
    return AssistanceCost(
        context_tokens=context_tokens,
        tool_tokens=tool_tokens,
        planning_tokens=planning_tokens,
        verification_tokens=verification_tokens,
    )


def _build_provenance(
    candidate: CandidateCapabilityEvidence,
    context: ContextEvidenceSnapshot | None,
    tools: ToolSurfaceSnapshot,
) -> tuple[ProvenanceClaim, ...]:
    claims: list[ProvenanceClaim] = [
        ProvenanceClaim(
            kind="candidate",
            claim=f"intrinsic:{candidate.candidate_id}",
            source="candidate_capability_evidence",
            digest=candidate.evidence_digest,
            observed_at=candidate.observed_at,
            freshness=candidate.freshness,
            fresh=candidate.freshness == "fresh",
        ),
        ProvenanceClaim(
            kind="intrinsic",
            claim=",".join(f"{k}={v}" for k, v in sorted(candidate.intrinsic_capabilities.items())),
            source="candidate_capability_evidence",
            digest=candidate.evidence_digest,
            observed_at=candidate.observed_at,
            freshness=candidate.freshness,
            fresh=candidate.freshness == "fresh",
        ),
        ProvenanceClaim(
            kind="tools",
            claim=",".join(sorted(tools.available_capabilities)) or "none",
            source="tool_surface_snapshot",
            digest=tools.evidence_digest or None,
            observed_at=tools.observed_at,
            freshness="fresh" if tools.fresh else "stale",
            fresh=tools.fresh,
        ),
    ]
    if context is not None:
        claims.append(
            ProvenanceClaim(
                kind="context",
                claim=(
                    f"slots={','.join(sorted(context.available_slots))};"
                    f"evidence={','.join(sorted(context.available_evidence_keys))}"
                ),
                source="context_evidence_snapshot",
                digest=context.evidence_digest or None,
                observed_at=context.observed_at,
                freshness="fresh" if context.fresh else "stale",
                fresh=context.fresh,
            )
        )
    return tuple(claims)


def _digest(payload: Mapping[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return f"sha256:{hashlib.sha256(canonical.encode()).hexdigest()}"


def _finalize(
    *,
    task_slice: TaskSlice,
    candidate: CandidateCapabilityEvidence,
    intrinsic_names: tuple[str, ...],
    req_slots: tuple[str, ...],
    req_evidence: tuple[str, ...],
    req_tools: tuple[str, ...],
    tools: ToolSurfaceSnapshot,
    context: ContextEvidenceSnapshot | None,
    decomp: DecompositionRequirement,
    verification: VerificationStrategy,
    cost: AssistanceCost,
    context_plan_reqs: Mapping[str, Any],
    provenance: tuple[ProvenanceClaim, ...],
    result: Sufficiency,
    reasons: tuple[str, ...],
    intrinsic_sufficient: bool,
    assisted_sufficient: bool,
    assistance_delta: tuple[str, ...],
    selected_surface: tuple[str, ...] | None = None,
) -> AssistancePlan:
    surface = selected_surface
    if surface is None:
        surface = tuple(
            tools.selected_surface[cap] for cap in req_tools if cap in tools.selected_surface
        )
    snapshot = {
        "schema_version": SCHEMA_VERSION,
        "candidate": candidate.to_dict(),
        "task_slice": task_slice.to_dict(),
        "intrinsic_names": list(intrinsic_names),
        "req_slots": list(req_slots),
        "req_evidence": list(req_evidence),
        "req_tools": list(req_tools),
        "tools": tools.to_dict(),
        "context": context.to_dict() if context is not None else None,
        "decomposition": decomp.to_dict(),
        "result": result,
        "reasons": list(reasons),
        "intrinsic_sufficient": intrinsic_sufficient,
        "assisted_sufficient": assisted_sufficient,
        "assistance_delta": list(assistance_delta),
    }
    evidence_digest = _digest(snapshot)
    plan_id = f"ecp:{evidence_digest[7:23]}"
    return AssistancePlan(
        plan_id=plan_id,
        candidate_id=candidate.candidate_id,
        task_slice=task_slice,
        required_intrinsic_capabilities=intrinsic_names,
        required_context_slots=req_slots,
        required_context_evidence=req_evidence,
        required_tool_capabilities=req_tools,
        selected_tool_surface=surface,
        decomposition=decomp,
        verification=verification,
        assistance_cost=cost,
        result=result,
        reasons=reasons,
        intrinsic_sufficient=intrinsic_sufficient,
        assisted_sufficient=assisted_sufficient,
        assistance_delta=assistance_delta,
        provenance=provenance,
        evidence_digest=evidence_digest,
        context_plan_requirements=dict(context_plan_reqs),
    )


__all__ = [
    "SCHEMA_VERSION",
    "AssistanceCost",
    "AssistancePlan",
    "CandidateCapabilityEvidence",
    "ContextEvidenceSnapshot",
    "DecompositionProposal",
    "DecompositionRequirement",
    "EffectiveCapabilityError",
    "ProvenanceClaim",
    "Sufficiency",
    "TaskSlice",
    "ToolSurfaceSnapshot",
    "VerificationStrategy",
    "plan_effective_capability",
]
