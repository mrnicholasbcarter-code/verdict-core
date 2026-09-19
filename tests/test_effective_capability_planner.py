"""BOD-120 Effective Capability Planner — proof fixtures.

Deterministic planning over explicit task requirements, candidate capability
evidence, candidate-specific ContextPack coverage, tools/MCP surfaces, and
decomposition/proof contracts. Not a learned agentic score.
"""

from __future__ import annotations

from typing import Any

from verdict.capability_gate import TaskRequirements
from verdict.effective_capability import (
    SCHEMA_VERSION,
    AssistanceCost,
    CandidateCapabilityEvidence,
    ContextEvidenceSnapshot,
    DecompositionProposal,
    ProvenanceClaim,
    Sufficiency,
    TaskSlice,
    ToolSurfaceSnapshot,
    plan_effective_capability,
)


def _slice(
    *,
    slice_id: str = "parent",
    objective: str = "Implement repo-specific auth fix",
    ac: tuple[str, ...] = ("returns 401 for bad token",),
    proof: tuple[str, ...] = ("pytest tests/test_auth.py -q",),
) -> TaskSlice:
    return TaskSlice(
        slice_id=slice_id, objective=objective, acceptance_criteria=ac, proof_criteria=proof
    )


def _cheap_candidate(
    *,
    tools: bool | None = True,
    vision: bool | None = False,
    hard_excluded: bool = False,
    hard_reason: str | None = None,
    context_window: int | None = 32_000,
    digest: str = "sha256:" + "a" * 64,
) -> CandidateCapabilityEvidence:
    return CandidateCapabilityEvidence(
        candidate_id="cheap/local-coder",
        intrinsic_capabilities={"tools": tools, "vision": vision, "structured": True},
        context_window=context_window,
        hard_gate_excluded=hard_excluded,
        hard_gate_reason=hard_reason,
        evidence_digest=digest,
        observed_at="2026-09-19T04:00:00Z",
        freshness="fresh",
    )


def _tools(
    available: frozenset[str],
    *,
    surface: dict[str, str] | None = None,
    digest: str = "sha256:" + "b" * 64,
    fresh: bool = True,
) -> ToolSurfaceSnapshot:
    return ToolSurfaceSnapshot(
        available_capabilities=available,
        selected_surface=surface or {cap: f"native.{cap}" for cap in sorted(available)},
        evidence_digest=digest,
        observed_at="2026-09-19T04:00:00Z",
        fresh=fresh,
    )


def _context(
    *,
    slots: frozenset[str],
    evidence_keys: frozenset[str],
    omitted: frozenset[str] = frozenset(),
    budget: int = 4096,
    used: int = 800,
    digest: str = "sha256:" + "c" * 64,
    fresh: bool = True,
    hydrated: bool = True,
) -> ContextEvidenceSnapshot:
    return ContextEvidenceSnapshot(
        available_slots=slots,
        available_evidence_keys=evidence_keys,
        omitted_required=omitted,
        token_budget=budget,
        used_tokens=used,
        evidence_digest=digest,
        observed_at="2026-09-19T04:00:00Z",
        fresh=fresh,
        hydrated=hydrated,
    )


# ── Proof 1: unaided fail / assisted pass + delta ────────────────────────────


def test_cheap_model_fails_unaided_passes_with_complete_contextpack() -> None:
    task = _slice()
    candidate = _cheap_candidate()
    required_slots = ("evidence", "instructions")
    required_evidence = ("adr:auth", "symbol:verify_token")
    required_tools = ("code.symbols",)

    unaided = plan_effective_capability(
        task_slice=task,
        candidate=candidate,
        requirements=TaskRequirements(names=("tools",), min_context=8000),
        context=None,  # no assistance
        tools=_tools(frozenset(required_tools)),
        required_context_slots=required_slots,
        required_context_evidence=required_evidence,
        required_tool_capabilities=required_tools,
    )
    assert unaided.result == "insufficient"
    assert unaided.intrinsic_sufficient is False
    assert unaided.assisted_sufficient is False
    assert any("context" in r or "assistance" in r for r in unaided.reasons)

    complete = _context(slots=frozenset(required_slots), evidence_keys=frozenset(required_evidence))
    assisted = plan_effective_capability(
        task_slice=task,
        candidate=candidate,
        requirements=TaskRequirements(names=("tools",), min_context=8000),
        context=complete,
        tools=_tools(frozenset(required_tools)),
        required_context_slots=required_slots,
        required_context_evidence=required_evidence,
        required_tool_capabilities=required_tools,
    )
    assert assisted.result == "sufficient"
    assert assisted.intrinsic_sufficient is False
    assert assisted.assisted_sufficient is True
    assert assisted.assistance_delta
    assert any("context" in d for d in assisted.assistance_delta)
    # Receipt explains intrinsic vs assisted
    receipt = assisted.to_dict()
    assert receipt["intrinsic_sufficient"] is False
    assert receipt["assisted_sufficient"] is True
    assert receipt["result"] == "sufficient"


# ── Proof 2: missing mandatory tool ──────────────────────────────────────────


def test_missing_mandatory_tool_insufficient_despite_rich_context() -> None:
    rich = _context(
        slots=frozenset({"evidence", "instructions", "memory", "policy", "state"}),
        evidence_keys=frozenset({"adr:auth", "symbol:verify_token", "docs:readme", "git:diff"}),
        used=3500,
    )
    plan = plan_effective_capability(
        task_slice=_slice(),
        candidate=_cheap_candidate(tools=True),
        requirements=TaskRequirements(names=("tools",)),
        context=rich,
        tools=_tools(frozenset({"docs.project"})),  # missing code.symbols
        required_context_slots=("evidence", "instructions"),
        required_context_evidence=("adr:auth",),
        required_tool_capabilities=("code.symbols",),
    )
    assert plan.result == "insufficient"
    assert any("tool" in r for r in plan.reasons)
    assert plan.assisted_sufficient is False


# ── Proof 3: frontier decomposition preserves parent AC/proof ────────────────


def test_frontier_decomposition_preserves_parent_ac_and_proof_union() -> None:
    parent = _slice(
        objective="Ship auth hardening end-to-end",
        ac=("returns 401 for bad token", "logs audit event on failure"),
        proof=("pytest tests/test_auth.py -q", "pytest tests/test_audit.py -q"),
    )
    children = (
        TaskSlice(
            slice_id="child-auth",
            objective="Fix token verification",
            acceptance_criteria=("returns 401 for bad token",),
            proof_criteria=("pytest tests/test_auth.py -q",),
            parent_slice_id=parent.slice_id,
        ),
        TaskSlice(
            slice_id="child-audit",
            objective="Emit audit log on auth failure",
            acceptance_criteria=("logs audit event on failure",),
            proof_criteria=("pytest tests/test_audit.py -q",),
            parent_slice_id=parent.slice_id,
        ),
    )
    proposal = DecompositionProposal(children=children, planner_role="frontier")
    plan = plan_effective_capability(
        task_slice=parent,
        candidate=_cheap_candidate(),
        requirements=TaskRequirements(),
        context=_context(
            slots=frozenset({"evidence", "instructions"}), evidence_keys=frozenset({"adr:auth"})
        ),
        tools=_tools(frozenset()),
        required_context_slots=("evidence",),
        required_context_evidence=("adr:auth",),
        required_tool_capabilities=(),
        decomposition=proposal,
    )
    assert plan.decomposition.required is True
    assert plan.decomposition.preserves_parent_acceptance is True
    assert plan.decomposition.preserves_parent_proof is True
    assert len(plan.decomposition.child_slices) == 2
    # Union of child AC/proof equals parent
    child_ac = {c for child in plan.decomposition.child_slices for c in child.acceptance_criteria}
    child_proof = {c for child in plan.decomposition.child_slices for c in child.proof_criteria}
    assert child_ac == set(parent.acceptance_criteria)
    assert child_proof == set(parent.proof_criteria)


def test_decomposition_that_drops_parent_ac_is_rejected() -> None:
    parent = _slice(
        ac=("returns 401 for bad token", "logs audit event on failure"),
        proof=("pytest tests/test_auth.py -q",),
    )
    incomplete = DecompositionProposal(
        children=(
            TaskSlice(
                slice_id="child-only-auth",
                objective="partial",
                acceptance_criteria=("returns 401 for bad token",),  # drops audit AC
                proof_criteria=("pytest tests/test_auth.py -q",),
                parent_slice_id=parent.slice_id,
            ),
        ),
        planner_role="frontier",
    )
    plan = plan_effective_capability(
        task_slice=parent,
        candidate=_cheap_candidate(),
        requirements=TaskRequirements(),
        context=_context(slots=frozenset({"evidence"}), evidence_keys=frozenset({"adr:auth"})),
        tools=_tools(frozenset()),
        required_context_slots=("evidence",),
        required_context_evidence=("adr:auth",),
        required_tool_capabilities=(),
        decomposition=incomplete,
    )
    assert plan.decomposition.preserves_parent_acceptance is False
    assert plan.result in {"insufficient", "unknown"}
    assert any("decomposition" in r or "acceptance" in r for r in plan.reasons)


# ── Proof 4: tight budget omits required evidence ────────────────────────────


def test_tight_budget_omitting_required_evidence_is_not_hydrated_success() -> None:
    plan = plan_effective_capability(
        task_slice=_slice(),
        candidate=_cheap_candidate(),
        requirements=TaskRequirements(names=("tools",)),
        context=_context(
            slots=frozenset({"instructions"}),
            evidence_keys=frozenset(),  # nothing landed
            omitted=frozenset({"adr:auth", "symbol:verify_token"}),
            budget=256,
            used=256,
            hydrated=False,
        ),
        tools=_tools(frozenset({"code.symbols"})),
        required_context_slots=("evidence", "instructions"),
        required_context_evidence=("adr:auth", "symbol:verify_token"),
        required_tool_capabilities=("code.symbols",),
    )
    assert plan.result in {"insufficient", "unknown"}
    assert plan.assisted_sufficient is False
    assert any("evidence" in r or "budget" in r or "omitted" in r for r in plan.reasons)


# ── Proof 5: tool availability change invalidates prior plan ─────────────────


def test_tool_availability_change_invalidates_plan_by_evidence_digest() -> None:
    task = _slice()
    candidate = _cheap_candidate()
    ctx = _context(
        slots=frozenset({"evidence", "instructions"}), evidence_keys=frozenset({"adr:auth"})
    )
    tools_v1 = _tools(frozenset({"code.symbols"}), digest="sha256:" + "1" * 64)
    plan_v1 = plan_effective_capability(
        task_slice=task,
        candidate=candidate,
        requirements=TaskRequirements(names=("tools",)),
        context=ctx,
        tools=tools_v1,
        required_context_slots=("evidence",),
        required_context_evidence=("adr:auth",),
        required_tool_capabilities=("code.symbols",),
    )
    assert plan_v1.result == "sufficient"

    tools_v2 = _tools(
        frozenset(),  # tools gone
        digest="sha256:" + "2" * 64,
        fresh=False,
    )
    plan_v2 = plan_effective_capability(
        task_slice=task,
        candidate=candidate,
        requirements=TaskRequirements(names=("tools",)),
        context=ctx,
        tools=tools_v2,
        required_context_slots=("evidence",),
        required_context_evidence=("adr:auth",),
        required_tool_capabilities=("code.symbols",),
    )
    assert plan_v1.evidence_digest != plan_v2.evidence_digest
    assert plan_v2.result == "insufficient"
    assert any(not c.fresh for c in plan_v2.provenance if c.kind == "tools") or any(
        "tool" in r for r in plan_v2.reasons
    )


# ── Proof 6: assistance cost fields for BOD-54/104 ───────────────────────────


def test_assistance_plan_exposes_cost_fields_for_downstream_optimizers() -> None:
    plan = plan_effective_capability(
        task_slice=_slice(),
        candidate=_cheap_candidate(),
        requirements=TaskRequirements(names=("tools",)),
        context=_context(
            slots=frozenset({"evidence", "instructions"}),
            evidence_keys=frozenset({"adr:auth"}),
            used=1200,
            budget=4096,
        ),
        tools=_tools(frozenset({"code.symbols"})),
        required_context_slots=("evidence",),
        required_context_evidence=("adr:auth",),
        required_tool_capabilities=("code.symbols",),
    )
    cost = plan.assistance_cost
    assert isinstance(cost, AssistanceCost)
    assert cost.context_tokens >= 0
    assert cost.tool_tokens >= 0
    assert cost.planning_tokens >= 0
    assert cost.verification_tokens >= 0
    assert cost.total_tokens == (
        cost.context_tokens + cost.tool_tokens + cost.planning_tokens + cost.verification_tokens
    )
    payload = plan.to_dict()
    assert "assistance_cost" in payload
    assert set(payload["assistance_cost"]) >= {
        "context_tokens",
        "tool_tokens",
        "planning_tokens",
        "verification_tokens",
        "total_tokens",
    }


# ── Additional invariants ────────────────────────────────────────────────────


def test_hard_gate_excluded_never_enters_assisted_qualification() -> None:
    rich = _context(
        slots=frozenset({"evidence", "instructions", "memory"}),
        evidence_keys=frozenset({"adr:auth", "symbol:verify_token"}),
    )
    plan = plan_effective_capability(
        task_slice=_slice(),
        candidate=_cheap_candidate(hard_excluded=True, hard_reason="capability_mismatch:vision"),
        requirements=TaskRequirements(names=("vision",)),
        context=rich,
        tools=_tools(frozenset({"code.symbols"})),
        required_context_slots=("evidence",),
        required_context_evidence=("adr:auth",),
        required_tool_capabilities=("code.symbols",),
    )
    assert plan.result == "insufficient"
    assert plan.assisted_sufficient is False
    assert any("hard_gate" in r for r in plan.reasons)


def test_unknown_evidence_never_becomes_sufficient_by_optimism() -> None:
    plan = plan_effective_capability(
        task_slice=_slice(),
        candidate=_cheap_candidate(tools=None),  # unknown tools cap
        requirements=TaskRequirements(names=("tools",)),
        context=_context(slots=frozenset({"evidence"}), evidence_keys=frozenset({"adr:auth"})),
        tools=_tools(frozenset({"code.symbols"})),
        required_context_slots=("evidence",),
        required_context_evidence=("adr:auth",),
        required_tool_capabilities=("code.symbols",),
    )
    assert plan.result == "unknown"
    assert plan.assisted_sufficient is False
    assert any("unknown" in r for r in plan.reasons)


def test_intrinsic_hard_deficit_vision_not_assistable_by_context() -> None:
    plan = plan_effective_capability(
        task_slice=_slice(objective="Describe the screenshot UI defect"),
        candidate=_cheap_candidate(vision=False),
        requirements=TaskRequirements(names=("vision",)),
        context=_context(
            slots=frozenset({"evidence", "instructions", "memory"}),
            evidence_keys=frozenset({"image:screenshot-desc"}),
        ),
        tools=_tools(frozenset()),
        required_context_slots=("evidence",),
        required_context_evidence=("image:screenshot-desc",),
        required_tool_capabilities=(),
    )
    assert plan.result == "insufficient"
    assert any("vision" in r or "intrinsic" in r for r in plan.reasons)
    assert plan.assisted_sufficient is False


def test_plans_are_deterministic_from_same_evidence_snapshot() -> None:
    kwargs: dict[str, Any] = dict(
        task_slice=_slice(),
        candidate=_cheap_candidate(),
        requirements=TaskRequirements(names=("tools",)),
        context=_context(
            slots=frozenset({"evidence", "instructions"}), evidence_keys=frozenset({"adr:auth"})
        ),
        tools=_tools(frozenset({"code.symbols"})),
        required_context_slots=("evidence", "instructions"),
        required_context_evidence=("adr:auth",),
        required_tool_capabilities=("code.symbols",),
    )
    a = plan_effective_capability(**kwargs)
    b = plan_effective_capability(**kwargs)
    assert a.plan_id == b.plan_id
    assert a.evidence_digest == b.evidence_digest
    assert a.to_dict() == b.to_dict()
    assert a.schema_version == SCHEMA_VERSION


def test_provenance_attached_to_capability_and_evidence_claims() -> None:
    plan = plan_effective_capability(
        task_slice=_slice(),
        candidate=_cheap_candidate(),
        requirements=TaskRequirements(names=("tools",)),
        context=_context(slots=frozenset({"evidence"}), evidence_keys=frozenset({"adr:auth"})),
        tools=_tools(frozenset({"code.symbols"})),
        required_context_slots=("evidence",),
        required_context_evidence=("adr:auth",),
        required_tool_capabilities=("code.symbols",),
    )
    assert plan.provenance
    kinds = {claim.kind for claim in plan.provenance}
    assert "candidate" in kinds or "intrinsic" in kinds
    for claim in plan.provenance:
        assert isinstance(claim, ProvenanceClaim)
        assert claim.claim
        assert claim.source


def test_sufficiency_literal_contract() -> None:
    allowed: set[Sufficiency] = {"sufficient", "insufficient", "unknown"}
    assert allowed == {"sufficient", "insufficient", "unknown"}


def test_candidate_specific_context_requirements_differ_by_candidate() -> None:
    """Planner requests candidate-specific packs, not one universal pack."""
    shared_slice = _slice()
    cheap = plan_effective_capability(
        task_slice=shared_slice,
        candidate=_cheap_candidate(),
        requirements=TaskRequirements(),
        context=None,
        tools=_tools(frozenset()),
        required_context_slots=("evidence", "instructions", "memory"),
        required_context_evidence=("adr:auth", "symbol:verify_token"),
        required_tool_capabilities=(),
    )
    frontier = plan_effective_capability(
        task_slice=shared_slice,
        candidate=CandidateCapabilityEvidence(
            candidate_id="frontier/opus",
            intrinsic_capabilities={"tools": True, "vision": True, "structured": True},
            context_window=200_000,
            hard_gate_excluded=False,
            evidence_digest="sha256:" + "f" * 64,
            observed_at="2026-09-19T04:00:00Z",
            freshness="fresh",
        ),
        requirements=TaskRequirements(),
        context=None,
        tools=_tools(frozenset()),
        required_context_slots=("evidence",),  # frontier needs less assistance
        required_context_evidence=("adr:auth",),
        required_tool_capabilities=(),
    )
    assert cheap.required_context_slots != frontier.required_context_slots
    assert cheap.candidate_id != frontier.candidate_id
    assert cheap.context_plan_requirements["candidate_id"] == cheap.candidate_id
    assert frontier.context_plan_requirements["candidate_id"] == frontier.candidate_id
