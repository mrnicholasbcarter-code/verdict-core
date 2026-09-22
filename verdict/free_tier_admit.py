"""Admit free-tier ∩ active-provider concrete identities for offloadable work.

Live OmniRoute surfaces used:

* ``GET /v1/models`` — concrete executable catalog identities
* ``GET /api/free-tier/summary`` — positively-free metadata (``perModel``)
* ``GET /api/providers`` — connections with ``isActive``

Metadata-only free-tier rows that cannot be resolved to a catalog identity, or
whose provider is inactive/unconnected, become *named drops* rather than fake
green. Opaque ``auto/*`` aliases are never admitted. An empty intersection
fails closed — the caller must not treat frontier-primary fallback as success.

Serve cheap-path callers then intersect this receipt with fresh prove-at-rest
passports and a budgeted confirm probe (see ``verdict.admit_prove_confirm``).

BOD-127: this module is a candidate *feed* only. ``chosen`` is an
advisory ranking of admitted free∩active identities — never production
serve-path strategy authority. Serve path must consume
``optimize_execution_path`` / ``ExecutionPathDecision``.
"""

from __future__ import annotations

import os
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, replace
from hashlib import sha256
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

from verdict.availability import is_opaque_route_id
from verdict.classifier import classify
from verdict.context_budget import (
    BudgetCandidateLimit,
    BudgetReceipt,
    BudgetUnit,
    ContextBudgetError,
    ContextBudgetGovernor,
)
from verdict.context_intelligence import ContextIntelligenceError
from verdict.context_pack import (
    ContextContractError,
    ContextPackCompiler,
    ContextPackSlot,
    ContextPlan,
    ContextUnit,
    unit_prompt_token_cost,
)
from verdict.context_trust import SourceKind, admit_external_evidence
from verdict.eligibility import EligibilityRecord, EligibilityResult, EligibilityVerdict
from verdict.free_route_harvest import free_status
from verdict.models import ModelInfo
from verdict.pack_state import PackState, classify_pack_state
from verdict.relay import fatal_identity_mismatch

REASON_OPAQUE_AUTO = "opaque_auto"
REASON_NOT_FREE_TIER = "not_free_tier"
REASON_INACTIVE_UNCONNECTED = "inactive_unconnected"
REASON_METADATA_GHOST = "metadata_ghost"
REASON_WORTHY_EXCLUDES_FREE = "worthy_excludes_free_for_cost"
REASON_CAPABILITY_MISMATCH = "capability_mismatch"
REASON_REQUIRED_UNKNOWN = "required_unknown"
REASON_UNMAPPED = "unmapped"
REASON_STALE = "stale"
REASON_PAID_FALLBACK = "paid_fallback"
REASON_TASK_INSTRUCTIONS_OMITTED = "task_instructions_omitted"
REASON_SPEND_POLICY_EXCLUDES_PAID = "spend_policy_excludes_paid"
REASON_SPEND_POLICY_REQUIRES_FRONTIER = "spend_policy_requires_frontier"
TASK_SOURCE_URI = "urn:verdict:task"
_COMBO_PREFIXES = frozenset({"claude", "combo"})
_ALIAS_PREFIXES = frozenset({"oc", "kr", "cf", "or", "nv"})
_SMALL_TOKENS = ("nano", "flash", "haiku", "mini", "small", "lite", "instant")

NO_ELIGIBLE_TARGET = "no_eligible_target"
FAIL_CLOSED_REASON = "fail_closed — empty free∩active ∩ fresh-passport ∩ confirmed intersection"


DEFAULT_CHEAP_PATH_TOKEN_BUDGET = 16_384


class LiveAdmitError(RuntimeError):
    """Raised when a required OmniRoute admit surface cannot be read."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ProviderConnection:
    provider: str
    is_active: bool
    test_status: str | None = None
    name: str | None = None


@dataclass(frozen=True)
class FreeTierModel:
    model_id: str
    provider: str
    free_type: str | None = None
    display_name: str | None = None


@dataclass(frozen=True)
class CatalogIdentity:
    identity_id: str
    provider: str


@dataclass(frozen=True)
class NamedDrop:
    model_id: str
    reason: str
    detail: str | None = None

    def to_dict(self) -> dict[str, str]:
        payload = {"model": self.model_id, "reason": self.reason}
        if self.detail:
            payload["detail"] = self.detail
        return payload


@dataclass(frozen=True)
class OmniRouteAdmitSnapshot:
    """Normalized live (or fixture) surfaces for free∩active admit."""

    catalog: tuple[CatalogIdentity, ...]
    free_tier: tuple[FreeTierModel, ...]
    connections: tuple[ProviderConnection, ...]

    @property
    def active_providers(self) -> frozenset[str]:
        return frozenset(row.provider for row in self.connections if row.is_active)

    @property
    def free_tier_providers(self) -> frozenset[str]:
        return frozenset(row.provider for row in self.free_tier if row.provider)


@dataclass(frozen=True)
class NamedOmission:
    """Something left out of a cheap-path context pack, with why."""

    name: str
    reason: str

    def to_dict(self) -> dict[str, str]:
        return {"name": self.name, "reason": self.reason}


@dataclass(frozen=True)
class IncludedProvenance:
    """A compiled cheap-path unit that landed in the pack, with provenance."""

    source_uri: str
    source_digest: str

    def to_dict(self) -> dict[str, str]:
        return {"source_uri": self.source_uri, "source_digest": self.source_digest}


@dataclass(frozen=True)
class CheapPathContextPack:
    """Compiled task pack used on the free∩active offload path."""

    pack_digest: str
    compiled_prompt: str
    omissions: tuple[NamedOmission, ...]
    pack_id: str
    plan_digest: str
    units: tuple[ContextUnit, ...] = ()
    included: tuple[IncludedProvenance, ...] = ()
    pack_state: PackState = "empty"
    # BOD-110 completeness contract: the task instructions must be packed, and
    # every task-required source must be included, before ``hydrated`` is possible.
    task_complete: bool = True
    required_sources: tuple[str, ...] = ()
    # BOD-128: BudgetReceipt + Context Trust feed for BOD-104 offers.
    budget_receipt: BudgetReceipt | None = None
    context_trust_admitted: bool = True
    capability_coverage: dict[str, Any] | None = None

    @property
    def included_sources(self) -> tuple[IncludedProvenance, ...]:
        """Receipt-facing alias of ``included`` (BOD-106 / QA smoke field)."""
        return self.included

    @property
    def missing_required_sources(self) -> tuple[str, ...]:
        included = {item.source_uri for item in self.included}
        return tuple(uri for uri in self.required_sources if uri not in included)

    @property
    def prompt_digest(self) -> str:
        """sha256 of the compiled prompt text — what an upstream actually receives."""
        return f"sha256:{sha256(self.compiled_prompt.encode('utf-8')).hexdigest()}"

    def to_dict(self) -> dict[str, Any]:
        sources = [item.to_dict() for item in self.included]
        return {
            "pack_digest": self.pack_digest,
            "prompt_digest": self.prompt_digest,
            "pack_id": self.pack_id,
            "plan_digest": self.plan_digest,
            "pack_state": self.pack_state,
            "task_complete": self.task_complete,
            "required_sources": list(self.required_sources),
            "missing_required_sources": list(self.missing_required_sources),
            "included": sources,
            "included_sources": list(sources),
            "omissions": [item.to_dict() for item in self.omissions],
            "budget_receipt": None
            if self.budget_receipt is None
            else self.budget_receipt.to_dict(),
            "context_trust_admitted": self.context_trust_admitted,
            "capability_coverage": self.capability_coverage,
        }


def build_cheap_path_context_pack(
    task: str,
    *,
    candidate_id: str,
    token_budget: int = DEFAULT_CHEAP_PATH_TOKEN_BUDGET,
    context_plan: ContextPlan | None = None,
    extra_slots: Sequence[ContextPackSlot] | None = None,
    workspace_root: Path | str | None = None,
    workspace_roots: Sequence[str] | None = None,
    mcp_root: Path | str | None = None,
    acceptance_criteria: Sequence[str] = (),
    proof_criteria: Sequence[str] = (),
    errors: Sequence[str] = (),
    memory_path: Path | str | None = None,
    use_context_fabric: bool = False,
) -> CheapPathContextPack:
    """Compile a provenance-rich context pack for cheap-path offload.

    Gather real workspace units (repo docs / architecture / ADRs / project docs,
    plus MCP only when a source is configured), admit every external unit through
    Context Trust (BOD-126), allocate via BudgetReceipt (BOD-125), then compile
    only the allocated set. High-value roots (ADR, architecture, README) remain
    preferred under budget. Missing sources become named omissions — never
    invented content. An empty gather still compiles the task and does not block
    execute.

    ``pack_state`` classifies the result for receipts (BOD-106). Savings stay
    blocked until ``hydrated``; empty/partial with a digest is still a hydrate
    FAIL. Hydrate/compiler errors stamp ``failed`` and still do not block execute.
    """
    if not isinstance(task, str) or not task.strip():
        raise ValueError("task must be a non-empty string")
    if not isinstance(candidate_id, str) or not candidate_id.strip():
        raise ValueError("candidate_id must be a non-empty string")
    if isinstance(token_budget, bool) or not isinstance(token_budget, int) or token_budget < 1:
        raise ValueError("token_budget must be a positive integer")
    if context_plan is not None:
        if context_plan.candidate_id != candidate_id:
            raise ValueError("context_plan candidate_id must match candidate_id")
        token_budget = context_plan.token_budget

    from verdict.context_hydrate import (
        CHEAP_PATH_EPOCH,
        cheap_path_unit_sort_key,
        gather_cheap_path_units,
    )

    task_slot = ContextPackSlot(
        slot_type="instructions",
        key="task",
        content=task,
        source="cheap_path",
        created_at=0.0,
        source_uri=TASK_SOURCE_URI,
    )
    slots = (task_slot, *(extra_slots or ()))
    units: list[ContextUnit] = []
    for slot in slots:
        unit = slot.to_unit()
        units.append(
            replace(
                unit, observed_at=CHEAP_PATH_EPOCH, retrieved_at=CHEAP_PATH_EPOCH, created_at=0.0
            )
        )
    capability_coverage: dict[str, Any] | None = None
    try:
        gathered = gather_cheap_path_units(
            task, workspace_root=workspace_root, roots=workspace_roots, mcp_root=mcp_root
        )
        units.extend(gathered.units)
        try:
            if not use_context_fabric:
                raise ContextIntelligenceError("disabled", "context fabric not requested")
            from verdict.context_hydrate import HydrateOmission, resolve_workspace_root
            from verdict.context_intelligence import execute_context_query, plan_context_query
            from verdict.documentation_preflight import shared_memory_path
            from verdict.memory_plane import MemoryPlane

            repo_root = resolve_workspace_root(workspace_root)
            plane_path = (
                Path(memory_path).expanduser()
                if memory_path is not None
                else shared_memory_path()
                if workspace_root is None
                else Path("/__verdict_no_memory__")
            )
            plane = MemoryPlane(plane_path) if plane_path.exists() else None
            try:
                fabric = execute_context_query(
                    plan_context_query(
                        task,
                        acceptance_criteria=acceptance_criteria,
                        proof_criteria=proof_criteria,
                        errors=errors,
                        token_budget=max(256, token_budget // 2),
                        max_units=8,
                        include_optional_graph=True,
                    ),
                    repo_root=repo_root,
                    plane=plane,
                )
            finally:
                if plane is not None:
                    plane.close()
            units.extend(fabric.units)
            capability_coverage = fabric.coverage.to_dict()
            gathered = replace(
                gathered,
                omissions=(
                    *gathered.omissions,
                    *(
                        HydrateOmission(name=f"capability:{item.capability_id}", reason=item.reason)
                        for item in fabric.coverage.omitted
                    ),
                ),
            )
        except (OSError, ValueError, ContextIntelligenceError) as exc:
            if isinstance(exc, ContextIntelligenceError) and exc.code == "disabled":
                capability_coverage = None
            else:
                capability_coverage = {
                    "requested": [],
                    "available": [],
                    "used": [],
                    "omitted": [
                        {
                            "capability_id": "context.fabric",
                            "reason": "provider_unavailable",
                            "provider_id": None,
                        }
                    ],
                }
        admitted_units, trust_omissions = _admit_external_units_for_compile(
            units, task_policy=task, epoch=CHEAP_PATH_EPOCH
        )
        budget_receipt, compile_units, budget_omissions = _allocate_compile_units(
            admitted_units,
            candidate_id=candidate_id,
            token_budget=token_budget,
            output_token_reserve=0 if context_plan is None else context_plan.output_token_reserve,
            tool_token_reserve=0 if context_plan is None else context_plan.tool_token_reserve,
        )
        plan = context_plan or ContextPlan(
            plan_id=f"cheap:{candidate_id}",
            candidate_id=candidate_id,
            token_budget=budget_receipt.usable_input_budget,
            created_at=CHEAP_PATH_EPOCH,
        )
        pack = ContextPackCompiler().compile_units(
            tuple(compile_units),
            plan,
            unit_sort_key=cheap_path_unit_sort_key(
                compile_units, token_budget=budget_receipt.usable_input_budget
            ),
        )
    except ContextBudgetError as exc:
        if exc.code == "mandatory_overflow":
            # Mandatory task cannot fit usable budget — BOD-110 failed pack.
            digest = f"sha256:{sha256(task.encode('utf-8')).hexdigest()}"
            return CheapPathContextPack(
                pack_digest=digest,
                compiled_prompt="",
                omissions=(
                    NamedOmission(name=TASK_SOURCE_URI, reason=REASON_TASK_INSTRUCTIONS_OMITTED),
                ),
                pack_id="failed",
                plan_digest=f"sha256:{sha256(candidate_id.encode('utf-8')).hexdigest()}",
                units=(),
                included=(),
                pack_state="failed",
                task_complete=False,
                budget_receipt=None,
                context_trust_admitted=True,
            )
        return _failed_cheap_path_pack(task, candidate_id=candidate_id, token_budget=token_budget)
    except (OSError, UnicodeError, ContextContractError):
        return _failed_cheap_path_pack(task, candidate_id=candidate_id, token_budget=token_budget)
    compiler_omissions = tuple(
        NamedOmission(name=_omission_name(decision), reason=decision.reason)
        for decision in pack.decisions
        if decision.action == "exclude"
    )
    gather_omissions = tuple(
        NamedOmission(name=item.name, reason=item.reason) for item in gathered.omissions
    )
    included = tuple(
        IncludedProvenance(source_uri=unit.source_uri, source_digest=unit.source_digest)
        for unit in pack.units
        if _is_workspace_provenance(unit.source_uri)
    )
    # The task slot must survive compilation. If the budget (or a safety gate)
    # dropped it, the compiled prompt no longer carries the instructions and
    # must be reported as failed — never silently executed as a hydrated pack.
    task_complete = any(unit.source_uri == TASK_SOURCE_URI for unit in pack.units)
    omissions = gather_omissions + trust_omissions + budget_omissions + compiler_omissions
    if not task_complete:
        omissions = (
            NamedOmission(name=TASK_SOURCE_URI, reason=REASON_TASK_INSTRUCTIONS_OMITTED),
            *omissions,
        )
    pack_state = classify_pack_state(
        included=included,
        gathered=gathered.units,
        omissions=omissions,
        required=gathered.required_uris,
        task_complete=task_complete,
    )
    return CheapPathContextPack(
        pack_digest=pack.digest,
        compiled_prompt=pack.compiled_prompt,
        omissions=omissions,
        pack_id=pack.pack_id,
        plan_digest=pack.plan_digest or plan.digest,
        units=pack.units,
        included=included,
        pack_state=pack_state,
        task_complete=task_complete,
        required_sources=gathered.required_uris,
        budget_receipt=budget_receipt,
        context_trust_admitted=True,
        capability_coverage=capability_coverage,
    )


def _external_source_kind(unit: ContextUnit) -> SourceKind | None:
    """Return a Context Trust source kind for external units; None if local/task."""
    uri = unit.source_uri
    if uri == TASK_SOURCE_URI or uri.startswith("urn:verdict:"):
        return None
    if uri.startswith("mcp:"):
        return "mcp_output"
    if uri.startswith(("http://", "https://")):
        return "web"
    if unit.trust in {"untrusted", "unverified"} and unit.authority in {"evidence", "unverified"}:
        return "unknown"
    return None


def _admit_external_units_for_compile(
    units: Sequence[ContextUnit], *, task_policy: str, epoch: str
) -> tuple[list[ContextUnit], tuple[NamedOmission, ...]]:
    """Run BOD-126 admit on every external unit before model-bound compile."""
    admitted: list[ContextUnit] = []
    omissions: list[NamedOmission] = []
    for unit in units:
        kind = _external_source_kind(unit)
        if kind is None:
            admitted.append(unit)
            continue
        result = admit_external_evidence(
            content=unit.content,
            source_kind=kind,
            source_uri=unit.source_uri,
            unit_id=unit.unit_id,
            key=unit.key,
            slot_type=unit.slot_type,
            task_policy=task_policy,
            observed_at=epoch,
            tenant_scope=unit.tenant_scope,
            project_scope=unit.project_scope,
        )
        if result.excluded or result.unit is None:
            reason = result.exclusion_reason or "excluded"
            omissions.append(
                NamedOmission(
                    name=unit.source_uri
                    if _is_workspace_provenance(unit.source_uri)
                    else unit.unit_id,
                    reason=f"context_trust_{reason}",
                )
            )
            continue
        admitted.append(replace(result.unit, observed_at=epoch, retrieved_at=epoch, created_at=0.0))
    return admitted, tuple(omissions)


def _budget_source_class(unit: ContextUnit) -> str:
    if unit.source_uri == TASK_SOURCE_URI or unit.slot_type == "instructions":
        return "task_spec"
    if unit.source_uri.startswith("mcp:") or unit.slot_type == "tools":
        return "mcp_tools"
    if unit.slot_type == "history":
        return "conversation_history"
    if unit.slot_type == "memory":
        return "memory"
    if unit.slot_type in {"system", "policy"}:
        return "system_harness"
    return "docs"


def _budget_value_score(unit: ContextUnit) -> float:
    from verdict.context_hydrate import unit_hydrate_class

    cls = unit_hydrate_class(unit)
    if cls < 0:
        return 1.0
    if unit.source_uri.startswith("mcp:"):
        return 0.55
    if unit.source_uri.startswith(("http://", "https://")):
        return 0.5
    scores = {0: 0.95, 1: 0.9, 2: 0.85, 3: 0.8}
    return scores.get(cls, 0.4)


def _context_units_to_budget_units(units: Sequence[ContextUnit]) -> list[BudgetUnit]:
    budget_units: list[BudgetUnit] = []
    for unit in units:
        mandatory = unit.source_uri == TASK_SOURCE_URI or unit.slot_type == "instructions"
        budget_units.append(
            BudgetUnit(
                unit_id=unit.unit_id,
                source_class=_budget_source_class(unit),  # type: ignore[arg-type]
                priority="mandatory" if mandatory else "optional",
                content=unit.content,
                token_count=unit_prompt_token_cost(unit),
                token_count_kind="estimated",
                provenance_uri=unit.source_uri,
                value_score=_budget_value_score(unit),
            )
        )
    return budget_units


def _allocate_compile_units(
    units: Sequence[ContextUnit],
    *,
    candidate_id: str,
    token_budget: int,
    output_token_reserve: int = 0,
    tool_token_reserve: int = 0,
) -> tuple[BudgetReceipt, list[ContextUnit], tuple[NamedOmission, ...]]:
    """Allocate via BudgetReceipt; return only included units for compile (no dual budget)."""
    governor = ContextBudgetGovernor()
    limit = BudgetCandidateLimit(
        candidate_id=candidate_id,
        context_limit=token_budget,
        output_reserve=output_token_reserve,
        tool_call_reserve=tool_token_reserve,
    )
    receipt = governor.allocate(_context_units_to_budget_units(units), limit)
    included_ids = {item.unit_id for item in receipt.included}
    compile_units = [unit for unit in units if unit.unit_id in included_ids]
    omissions = tuple(
        NamedOmission(
            name=om.provenance_uri if _is_workspace_provenance(om.provenance_uri) else om.unit_id,
            # Preserve governor reason strings (e.g. input_budget_exhausted) for
            # existing cheap-path receipt consumers; do not invent a parallel taxonomy.
            reason=om.reason,
        )
        for om in receipt.omitted
    )
    return receipt, compile_units, omissions


def _is_workspace_provenance(source_uri: str) -> bool:
    return bool(source_uri) and not source_uri.startswith("urn:")


def _omission_name(decision: Any) -> str:
    ref = getattr(decision, "reversible_ref", None) or ""
    if ref and not str(ref).startswith("urn:"):
        return str(ref)
    return str(decision.unit_id)


def _failed_cheap_path_pack(
    task: str, *, candidate_id: str, token_budget: int
) -> CheapPathContextPack:
    """Stamp ``pack_state=failed`` without blocking execute or inventing sources."""
    from verdict.context_hydrate import CHEAP_PATH_EPOCH

    task_slot = ContextPackSlot(
        slot_type="instructions",
        key="task",
        content=task,
        source="cheap_path",
        created_at=0.0,
        source_uri=TASK_SOURCE_URI,
    )
    try:
        unit = replace(
            task_slot.to_unit(),
            observed_at=CHEAP_PATH_EPOCH,
            retrieved_at=CHEAP_PATH_EPOCH,
            created_at=0.0,
        )
        plan = ContextPlan(
            plan_id=f"cheap:{candidate_id}",
            candidate_id=candidate_id,
            token_budget=token_budget,
            created_at=CHEAP_PATH_EPOCH,
        )
        pack = ContextPackCompiler().compile_units((unit,), plan)
        pack_digest = pack.digest
        compiled_prompt = pack.compiled_prompt
        pack_id = pack.pack_id
        plan_digest = pack.plan_digest or plan.digest
        units = pack.units
    except (OSError, UnicodeError, ContextContractError, ValueError):
        compiled_prompt = task
        pack_digest = f"sha256:{sha256(task.encode()).hexdigest()}"
        pack_id = "failed"
        plan_digest = f"sha256:{sha256(candidate_id.encode()).hexdigest()}"
        units = ()
    return CheapPathContextPack(
        pack_digest=pack_digest,
        compiled_prompt=compiled_prompt,
        omissions=(NamedOmission(name="hydrate", reason="compiler_error"),),
        pack_id=pack_id,
        plan_digest=plan_digest,
        units=units,
        included=(),
        pack_state="failed",
        budget_receipt=None,
        context_trust_admitted=True,
    )


@dataclass(frozen=True)
class FreeTierAdmitReceipt:
    admitted: tuple[str, ...]
    exclusions: tuple[NamedDrop, ...]
    chosen: str | None
    empty_intersection: bool
    active_providers: tuple[str, ...]
    free_tier_providers: tuple[str, ...]
    pack_digest: str | None = None
    omissions: tuple[NamedOmission, ...] = ()
    included: tuple[IncludedProvenance, ...] = ()
    pack_state: PackState | None = None
    passport: tuple[Any, ...] = ()
    confirm: tuple[Any, ...] = ()
    selected_because: str | None = None
    task_class: str | None = None
    class_reasons: tuple[str, ...] = ()
    requirements: tuple[str, ...] = ()
    capability_matches: tuple[dict[str, Any], ...] = ()
    free_admitted: tuple[str, ...] = ()
    paid_admitted: tuple[str, ...] = ()
    task_complete: bool | None = None
    required_sources: tuple[str, ...] = ()
    prompt_digest: str | None = None
    capability_coverage: dict[str, Any] | None = None
    execution_attempts: tuple[dict[str, Any], ...] = ()
    verification: dict[str, Any] | None = None
    receipt_id: str | None = None
    task_profile_digest: str | None = None
    spend_policy: str | None = None
    candidate_pool: dict[str, Any] | None = None
    context_plans: tuple[ContextPlan, ...] = ()

    @property
    def included_sources(self) -> tuple[IncludedProvenance, ...]:
        """Receipt-facing alias of ``included`` (BOD-106 / QA smoke field)."""
        return self.included

    @property
    def missing_required_sources(self) -> tuple[str, ...]:
        included = {item.source_uri for item in self.included}
        return tuple(uri for uri in self.required_sources if uri not in included)

    def to_dict(self) -> dict[str, Any]:
        sources = [item.to_dict() for item in self.included]
        return {
            "admitted": list(self.admitted),
            "exclusions": [item.to_dict() for item in self.exclusions],
            "chosen": self.chosen,
            "empty_intersection": self.empty_intersection,
            "active_providers": list(self.active_providers),
            "free_tier_providers": list(self.free_tier_providers),
            "pack_digest": self.pack_digest,
            "prompt_digest": self.prompt_digest,
            "pack_state": self.pack_state,
            "task_complete": self.task_complete,
            "required_sources": list(self.required_sources),
            "missing_required_sources": list(self.missing_required_sources),
            "included": sources,
            "included_sources": list(sources),
            "omissions": [item.to_dict() for item in self.omissions],
            "passport": [
                item.to_dict() if hasattr(item, "to_dict") else item for item in self.passport
            ],
            "confirm": [
                item.to_dict() if hasattr(item, "to_dict") else item for item in self.confirm
            ],
            "selected_because": self.selected_because,
            "task_class": self.task_class,
            "class_reasons": list(self.class_reasons),
            "requirements": list(self.requirements),
            "capability_matches": [dict(item) for item in self.capability_matches],
            "free_admitted": list(self.free_admitted),
            "paid_admitted": list(self.paid_admitted),
            "capability_coverage": self.capability_coverage,
            "execution_attempts": [dict(item) for item in self.execution_attempts],
            "verification": None if self.verification is None else dict(self.verification),
            "receipt_id": self.receipt_id,
            "task_profile_digest": self.task_profile_digest,
            "spend_policy": self.spend_policy,
            "candidate_pool": None if self.candidate_pool is None else dict(self.candidate_pool),
            "context_plans": [plan.to_dict() for plan in self.context_plans],
        }

    def as_eligibility_result(self, snapshot: OmniRouteAdmitSnapshot) -> EligibilityResult:
        by_id = {item.identity_id: item for item in snapshot.catalog}
        admitted_models: list[ModelInfo] = []
        for model_id in self.admitted:
            identity = by_id.get(model_id)
            provider = identity.provider if identity is not None else _provider_of(model_id)
            admitted_models.append(
                ModelInfo(
                    id=model_id,
                    provider=provider,
                    capability_tier=classify(model_id),
                    is_available=True,
                    availability_state="eligible",
                    source="free_tier_active_admit",
                )
            )
        records: list[EligibilityRecord] = [
            EligibilityRecord(
                model_id=model.id,
                provider=model.provider,
                admitted=True,
                verdict=EligibilityVerdict.ELIGIBLE,
                state="eligible",
                source="free_tier_active_admit",
                reason="free-tier ∩ active provider",
            )
            for model in admitted_models
        ]
        for drop in self.exclusions:
            records.append(
                EligibilityRecord(
                    model_id=drop.model_id,
                    provider=_provider_of(drop.model_id),
                    admitted=False,
                    verdict=_verdict_for_reason(drop.reason),
                    state="excluded",
                    source="free_tier_active_admit",
                    reason=drop.detail or drop.reason,
                )
            )
        return EligibilityResult(admitted=admitted_models, records=records)


def _verdict_for_reason(reason: str) -> EligibilityVerdict:
    mapping = {
        REASON_OPAQUE_AUTO: EligibilityVerdict.OPAQUE_AUTO,
        REASON_NOT_FREE_TIER: EligibilityVerdict.NOT_FREE_TIER,
        REASON_INACTIVE_UNCONNECTED: EligibilityVerdict.INACTIVE_UNCONNECTED,
        REASON_METADATA_GHOST: EligibilityVerdict.METADATA_GHOST,
        "no_passport": EligibilityVerdict.NO_PASSPORT,
        "passport_stale": EligibilityVerdict.PASSPORT_STALE,
        "confirm_failed": EligibilityVerdict.CONFIRM_FAILED,
        "confirm_budget_exhausted": EligibilityVerdict.CONFIRM_FAILED,
        "confirm_unavailable": EligibilityVerdict.CONFIRM_FAILED,
        REASON_CAPABILITY_MISMATCH: EligibilityVerdict.CAPABILITY_MISMATCH,
        REASON_REQUIRED_UNKNOWN: EligibilityVerdict.REQUIRED_UNKNOWN,
        REASON_WORTHY_EXCLUDES_FREE: EligibilityVerdict.WORTHY_EXCLUDES_FREE,
        REASON_UNMAPPED: EligibilityVerdict.UNMAPPED,
        REASON_STALE: EligibilityVerdict.STALE,
        "map_target_missing": EligibilityVerdict.UNMAPPED,
    }
    return mapping.get(reason, EligibilityVerdict.NOT_LIVE_ELIGIBLE)


def _provider_of(identity_id: str) -> str:
    if "/" in identity_id:
        return identity_id.split("/", 1)[0]
    return identity_id or "unknown"


def parse_provider_connections(payload: object) -> tuple[ProviderConnection, ...]:
    """Parse ``GET /api/providers`` (``connections[].isActive``)."""
    rows: list[Any] = []
    if isinstance(payload, Mapping):
        raw = payload.get("connections", payload.get("providers", payload.get("data")))
        if isinstance(raw, list):
            rows = raw
        elif isinstance(payload.get("provider"), str) or "isActive" in payload:
            rows = [payload]
    elif isinstance(payload, list):
        rows = payload
    out: list[ProviderConnection] = []
    seen: set[tuple[str, bool, str | None]] = set()
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        provider = row.get("provider") or row.get("id")
        if not isinstance(provider, str) or not provider.strip():
            continue
        name = row.get("name")
        test_status = row.get("testStatus") or row.get("test_status")
        conn = ProviderConnection(
            provider=provider.strip(),
            is_active=row.get("isActive") is True,
            test_status=str(test_status) if isinstance(test_status, str) else None,
            name=str(name) if isinstance(name, str) else None,
        )
        key = (conn.provider, conn.is_active, conn.test_status)
        if key in seen:
            continue
        seen.add(key)
        out.append(conn)
    return tuple(out)


def parse_free_tier_models(payload: object) -> tuple[FreeTierModel, ...]:
    """Parse ``GET /api/free-tier/summary`` ``perModel`` rows."""
    raw: object
    if isinstance(payload, Mapping):
        raw = payload.get("perModel")
        if raw is None:
            raw = payload.get("models", payload.get("data", payload.get("items")))
    else:
        raw = payload
    if not isinstance(raw, list):
        return ()
    out: list[FreeTierModel] = []
    for row in raw:
        if isinstance(row, str) and row.strip():
            out.append(FreeTierModel(model_id=row.strip(), provider=_provider_of(row.strip())))
            continue
        if not isinstance(row, Mapping):
            continue
        model_id = row.get("modelId") or row.get("model_id") or row.get("id") or row.get("model")
        provider = row.get("provider") or row.get("owned_by")
        if not isinstance(model_id, str) or not model_id.strip():
            continue
        model_id = model_id.strip()
        if not isinstance(provider, str) or not provider.strip():
            provider = _provider_of(model_id)
        free_type = row.get("freeType") or row.get("free_type")
        display = row.get("displayName") or row.get("name")
        out.append(
            FreeTierModel(
                model_id=model_id,
                provider=provider.strip(),
                free_type=str(free_type) if isinstance(free_type, str) else None,
                display_name=str(display) if isinstance(display, str) else None,
            )
        )
    return tuple(out)


def parse_catalog_identities(payload: object) -> tuple[CatalogIdentity, ...]:
    """Parse OpenAI-compatible ``/v1/models`` or a management catalog envelope."""
    rows: list[Any]
    if isinstance(payload, Mapping) and isinstance(payload.get("catalog"), Mapping):
        rows = []
        catalog = payload["catalog"]
        assert isinstance(catalog, Mapping)
        for provider, group in catalog.items():
            if not isinstance(group, Mapping) or not isinstance(group.get("models"), list):
                continue
            for raw_row in group["models"]:
                if isinstance(raw_row, Mapping):
                    item = dict(raw_row)
                    item.setdefault("provider", str(provider))
                    item.setdefault("owned_by", str(provider))
                    rows.append(item)
        payload = {"data": rows}
    if isinstance(payload, Mapping):
        raw = payload.get("data", payload.get("models", payload.get("items", [])))
    else:
        raw = payload
    if not isinstance(raw, list):
        return ()
    out: list[CatalogIdentity] = []
    seen: set[str] = set()
    for row in raw:
        if not isinstance(row, Mapping):
            continue
        identity_id = row.get("id") or row.get("name")
        if not isinstance(identity_id, str) or not identity_id.strip():
            continue
        identity_id = identity_id.strip()
        if identity_id in seen:
            continue
        seen.add(identity_id)
        owned = row.get("owned_by") or row.get("provider")
        provider = (
            owned.strip() if isinstance(owned, str) and owned.strip() else _provider_of(identity_id)
        )
        out.append(CatalogIdentity(identity_id=identity_id, provider=provider))
    return tuple(out)


def snapshot_from_payloads(
    *, catalog: object, free_tier: object, providers: object
) -> OmniRouteAdmitSnapshot:
    return OmniRouteAdmitSnapshot(
        catalog=parse_catalog_identities(catalog),
        free_tier=parse_free_tier_models(free_tier),
        connections=parse_provider_connections(providers),
    )


def normalize_omniroute_origin(base_url: str) -> str:
    url = base_url.strip()
    parsed = urlsplit(url)
    path = parsed.path.rstrip("/")
    if path.endswith("/v1"):
        path = path[: -len("/v1")]
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", "")).rstrip("/")


def _identity_rank(identity_id: str, provider: str) -> tuple[int, int, int, str]:
    first = identity_id.split("/", 1)[0]
    opaque = 1 if is_opaque_route_id(identity_id) else 0
    combo = 1 if first in _COMBO_PREFIXES else 0
    canonical = 0 if first == provider else (1 if first in _ALIAS_PREFIXES else 2)
    return (opaque, combo, canonical, identity_id)


def _matches_free_model(catalog_id: str, model_id: str, provider: str) -> bool:
    if catalog_id == model_id:
        return True
    if catalog_id == f"{provider}/{model_id}":
        return True
    return catalog_id.endswith("/" + model_id)


def _is_positively_free_identity(identity_id: str) -> bool:
    return free_status({"id": identity_id}) == "free"


def _looks_free_by_name(identity_id: str) -> bool:
    """Name heuristic only. Never authoritative when a free_admitted set exists."""
    lowered = identity_id.lower()
    leaf = lowered.rsplit("/", 1)[-1]
    return ":free" in lowered or leaf.endswith("-free") or leaf.endswith(":free")


def _choose_sort(
    identity_id: str, active_healthy: frozenset[str], free_admitted: Collection[str] | None = None
) -> tuple[int, int, int, str]:
    """Free-first ordering keyed on authoritative free-tier membership (BOD-112).

    ``free_admitted`` is the free∩active set observed from OmniRoute's free-tier
    summary. When it is provided, an identity is free iff it is a member — a free
    model without a ``:free``/``-free`` suffix must not be displaced by paid
    candidates, and a paid model must not jump the queue by name. The name
    heuristic is used only when no authoritative set is available.
    """
    lowered = identity_id.lower()
    if free_admitted is not None:
        free_mark = 0 if identity_id in free_admitted else 1
    else:
        free_mark = 0 if _looks_free_by_name(identity_id) else 1
    small = 0 if any(token in lowered for token in _SMALL_TOKENS) else 1
    provider = _provider_of(identity_id)
    healthy = 0 if provider in active_healthy else 1
    return (free_mark, healthy, small, identity_id)


def resolve_catalog_matches(
    model_id: str, provider: str, catalog: Sequence[CatalogIdentity]
) -> tuple[str, ...]:
    """Concrete catalog identities that correspond to one free-tier row."""
    matches = [
        item.identity_id
        for item in catalog
        if item.provider == provider and _matches_free_model(item.identity_id, model_id, provider)
    ]
    if not matches:
        matches = [
            item.identity_id
            for item in catalog
            if _matches_free_model(item.identity_id, model_id, provider)
        ]
    concrete = [item for item in matches if not is_opaque_route_id(item)]
    if not concrete:
        return tuple(matches)
    concrete.sort(key=lambda identity_id: _identity_rank(identity_id, provider))
    best_rank = _identity_rank(concrete[0], provider)[:3]
    return tuple(item for item in concrete if _identity_rank(item, provider)[:3] == best_rank)


def admit_free_tier_active(snapshot: OmniRouteAdmitSnapshot) -> FreeTierAdmitReceipt:
    """Admit only free-tier ∩ active-provider concrete catalog identities."""
    active = snapshot.active_providers
    catalog = snapshot.catalog
    exclusions: list[NamedDrop] = []
    admitted: list[str] = []
    admitted_set: set[str] = set()

    def _admit(identity_id: str) -> None:
        if identity_id in admitted_set:
            return
        admitted_set.add(identity_id)
        admitted.append(identity_id)

    for row in snapshot.free_tier:
        label = f"{row.provider}/{row.model_id}"
        if is_opaque_route_id(row.model_id) or is_opaque_route_id(label):
            exclusions.append(NamedDrop(label, REASON_OPAQUE_AUTO, "opaque auto/* alias"))
            continue
        if (row.free_type or "").lower() == "discontinued":
            exclusions.append(
                NamedDrop(label, REASON_NOT_FREE_TIER, "discontinued free-tier metadata")
            )
            continue
        if row.provider not in active:
            exclusions.append(
                NamedDrop(
                    label, REASON_INACTIVE_UNCONNECTED, "provider is not an active connection"
                )
            )
            continue
        matches = resolve_catalog_matches(row.model_id, row.provider, catalog)
        concrete = [item for item in matches if not is_opaque_route_id(item)]
        if not concrete:
            if matches:
                exclusions.append(
                    NamedDrop(label, REASON_OPAQUE_AUTO, "resolved only to opaque aliases")
                )
            else:
                exclusions.append(
                    NamedDrop(
                        label,
                        REASON_METADATA_GHOST,
                        "free-tier metadata has no concrete catalog identity",
                    )
                )
            continue
        for identity_id in concrete:
            _admit(identity_id)

    free_providers = snapshot.free_tier_providers
    for identity in catalog:
        if identity.identity_id in admitted_set:
            continue
        if is_opaque_route_id(identity.identity_id):
            if identity.provider in active and identity.provider in free_providers:
                exclusions.append(
                    NamedDrop(identity.identity_id, REASON_OPAQUE_AUTO, "opaque catalog alias")
                )
            continue
        if identity.provider not in active:
            continue
        if identity.provider not in free_providers:
            continue
        if _is_positively_free_identity(identity.identity_id):
            first = identity.identity_id.split("/", 1)[0]
            if first in _COMBO_PREFIXES:
                exclusions.append(
                    NamedDrop(
                        identity.identity_id, REASON_OPAQUE_AUTO, "combo-prefixed catalog identity"
                    )
                )
                continue
            _admit(identity.identity_id)

    admitted_sorted = tuple(sorted(admitted))
    healthy = frozenset(
        row.provider
        for row in snapshot.connections
        if row.is_active and (row.test_status or "active") == "active"
    )
    chosen = None
    if admitted_sorted:
        chosen = sorted(
            admitted_sorted, key=lambda item: _choose_sort(item, healthy, admitted_sorted)
        )[0]
    return FreeTierAdmitReceipt(
        admitted=admitted_sorted,
        exclusions=tuple(exclusions),
        chosen=chosen,
        empty_intersection=chosen is None,
        active_providers=tuple(sorted(active)),
        free_tier_providers=tuple(sorted(free_providers)),
        free_admitted=admitted_sorted,
    )


def load_omniroute_admit_snapshot(
    base_url: str,
    api_key: str | None = None,
    *,
    timeout: float = 20.0,
    transport: httpx.BaseTransport | None = None,
) -> OmniRouteAdmitSnapshot:
    """Fetch catalog, free-tier summary, and provider connections from OmniRoute."""
    origin = normalize_omniroute_origin(base_url)
    headers = {"accept": "application/json"}
    if api_key and api_key.strip():
        headers["authorization"] = f"Bearer {api_key.strip()}"
    paths = {
        "catalog": "/v1/models",
        "free_tier": "/api/free-tier/summary",
        "providers": "/api/providers",
    }
    payloads: dict[str, object] = {}
    try:
        with httpx.Client(transport=transport, timeout=timeout, follow_redirects=False) as client:
            for name, path in paths.items():
                response = client.get(f"{origin}{path}", headers=headers)
                if response.status_code in {401, 403}:
                    raise LiveAdmitError("unauthorized", f"{path} returned {response.status_code}")
                if response.status_code == 404:
                    raise LiveAdmitError("unsupported", f"{path} is unavailable")
                if not 200 <= response.status_code < 300:
                    raise LiveAdmitError("http_error", f"{path} returned {response.status_code}")
                try:
                    payloads[name] = response.json()
                except ValueError as exc:
                    raise LiveAdmitError("malformed", f"{path} is not JSON") from exc
    except LiveAdmitError:
        raise
    except httpx.TimeoutException as exc:
        raise LiveAdmitError("timeout", "timed out reading OmniRoute admit surfaces") from exc
    except httpx.HTTPError as exc:
        raise LiveAdmitError("transport", type(exc).__name__) from exc
    return snapshot_from_payloads(
        catalog=payloads["catalog"],
        free_tier=payloads["free_tier"],
        providers=payloads["providers"],
    )


def execute_offload_chat(
    base_url: str,
    model_id: str,
    task: str,
    *,
    api_key: str | None = None,
    max_tokens: int = 256,
    timeout: float = 30.0,
    transport: httpx.BaseTransport | None = None,
) -> tuple[str, str]:
    """Send the routed (optionally packed) content through OmniRoute chat.

    ``task`` is the user message body — typically the cheap-path compiled
    prompt when a context pack was built. Returns
    ``(transport_outcome, content_or_error)``. Never returns secrets.
    """
    origin = normalize_omniroute_origin(base_url)
    headers = {"accept": "application/json", "content-type": "application/json"}
    if api_key and api_key.strip():
        headers["authorization"] = f"Bearer {api_key.strip()}"
    url = f"{origin}/v1/chat/completions"
    payload = {
        "model": model_id,
        "messages": [{"role": "user", "content": task}],
        "max_tokens": max_tokens,
    }
    try:
        with httpx.Client(transport=transport, timeout=timeout, follow_redirects=False) as client:
            response = client.post(url, headers=headers, json=payload)
            if not 200 <= response.status_code < 300:
                return "error", f"http {response.status_code}"
            body = response.json()
    except httpx.TimeoutException:
        return "error", "timeout"
    except (httpx.HTTPError, ValueError) as exc:
        return "error", type(exc).__name__
    if not isinstance(body, Mapping):
        return "error", "invalid provider response: expected JSON object"
    served_model = body.get("model")
    if served_model is not None and not isinstance(served_model, str):
        return "error", "invalid provider response: model must be a string"
    if fatal_identity_mismatch(model_id, served_model):
        return "error", f"identity mismatch: selected {model_id!r}, served {served_model!r}"
    choices = body.get("choices")
    if not isinstance(choices, list) or not choices:
        return "error", "invalid provider response: missing choices"
    message = (choices[0] or {}).get("message") if isinstance(choices[0], Mapping) else {}
    content = message.get("content") if isinstance(message, Mapping) else ""
    if not isinstance(content, str) or not content:
        return "error", "invalid provider response: missing completion content"
    return "sent", content


def omniroute_endpoint_from_env(
    providers: Mapping[str, Any] | None = None,
) -> tuple[str, str | None] | None:
    """Return ``(base_url, api_key)`` for OmniRoute when configured."""
    env_url = os.getenv("OMNIROUTE_BASE_URL")
    env_key = os.getenv("OMNIROUTE_API_KEY")
    if env_url and env_url.strip():
        return env_url.strip(), env_key
    if not providers:
        return None
    for name, cfg in providers.items():
        if name != "omniroute":
            continue
        base_url = getattr(cfg, "base_url", "") or ""
        if not str(base_url).strip():
            return None
        key = getattr(cfg, "api_key", None)
        env_name = getattr(cfg, "api_key_env", None)
        if not key and env_name:
            key = os.getenv(str(env_name))
        return str(base_url).strip(), key or env_key
    return None


def _is_combo_identity(identity_id: str) -> bool:
    return identity_id.split("/", 1)[0] in _COMBO_PREFIXES


def is_frontier_identity(identity_id: str, allowlist: tuple[str, ...] | None = None) -> bool:
    """Named frontier/high-cap identity. Never inferred from 'sounds expensive'."""
    if allowlist:
        return identity_id in allowlist
    return classify(identity_id) <= 1


def _catalog_paid_identities(
    snapshot: OmniRouteAdmitSnapshot, *, skip: set[str]
) -> tuple[tuple[str, ...], tuple[NamedDrop, ...]]:
    """Active-provider catalog identities that are not positively free."""
    admitted: list[str] = []
    seen: set[str] = set(skip)
    drops: list[NamedDrop] = []
    active = snapshot.active_providers
    for identity in snapshot.catalog:
        identity_id = identity.identity_id
        if identity_id in seen:
            continue
        if is_opaque_route_id(identity_id):
            continue
        if identity.provider not in active:
            continue
        if _is_positively_free_identity(identity_id):
            continue
        if _is_combo_identity(identity_id):
            drops.append(
                NamedDrop(identity_id, REASON_OPAQUE_AUTO, "combo-prefixed catalog identity")
            )
            continue
        seen.add(identity_id)
        admitted.append(identity_id)
    return tuple(admitted), tuple(drops)


def expand_admit_for_worthiness(
    receipt: FreeTierAdmitReceipt,
    snapshot: OmniRouteAdmitSnapshot,
    *,
    task_class: str,
    class_reasons: tuple[str, ...],
    frontier_allowlist: tuple[str, ...] | None = None,
    spend_policy: str | None = None,
    task_profile_digest: str | None = None,
) -> FreeTierAdmitReceipt:
    """Ordinary: keep free and add lesser-paid. Worthy: drop free-for-cost, keep frontier paid.

    ``spend_policy`` (BOD-S1) is a hard economic boundary applied *after* class
    semantics: ``free_only`` can never admit a paid identity regardless of task
    class or score (every paid candidate gets a named exclusion), and
    ``frontier_required`` keeps only frontier-class paid identities. Policy
    decides who may compete; nothing here silently escalates spend.
    """
    from verdict.task_profile import (  # lazy: breaks the verdict.__init__ cycle
        SPEND_FREE_ONLY,
        SPEND_FREE_PREFERRED,
        SPEND_FRONTIER_REQUIRED,
        normalize_spend_policy,
    )

    policy = normalize_spend_policy(spend_policy)
    free_set = frozenset(receipt.free_admitted or receipt.admitted)
    paid, extra_drops = _catalog_paid_identities(snapshot, skip=set(receipt.admitted))
    exclusions = list(receipt.exclusions)
    exclusions.extend(extra_drops)

    if task_class == "worthy":
        for identity_id in receipt.admitted:
            exclusions.append(
                NamedDrop(
                    identity_id,
                    REASON_WORTHY_EXCLUDES_FREE,
                    "worthy path never selects free-tier solely for cost",
                )
            )
        frontier = tuple(item for item in paid if is_frontier_identity(item, frontier_allowlist))
        for identity_id in paid:
            if identity_id not in frontier:
                exclusions.append(
                    NamedDrop(
                        identity_id,
                        REASON_NOT_FREE_TIER,
                        "ordinary/lesser-paid identity is not frontier-class for worthy work",
                    )
                )
        remaining = frontier
        chosen = remaining[0] if remaining else None
        free_admitted = receipt.free_admitted or receipt.admitted
        paid_admitted: tuple[str, ...] = frontier
    else:
        merged = tuple(dict.fromkeys((*receipt.admitted, *paid)))
        chosen = receipt.chosen if receipt.chosen in merged else (merged[0] if merged else None)
        remaining = merged
        free_admitted = receipt.free_admitted or receipt.admitted
        paid_admitted = paid

    # Hard policy filter: remove anyone the economic policy forbids, naming why.
    if policy == SPEND_FREE_ONLY:
        for identity_id in paid:
            if not any(
                drop.model_id == identity_id and drop.reason == REASON_SPEND_POLICY_EXCLUDES_PAID
                for drop in exclusions
            ):
                exclusions.append(
                    NamedDrop(
                        identity_id,
                        REASON_SPEND_POLICY_EXCLUDES_PAID,
                        "spend_policy=free_only forbids paid identities regardless of score",
                    )
                )
        kept = tuple(item for item in remaining if item in free_set)
        for identity_id in remaining:
            if identity_id not in free_set:
                exclusions.append(
                    NamedDrop(
                        identity_id,
                        REASON_SPEND_POLICY_EXCLUDES_PAID,
                        "spend_policy=free_only forbids paid identities regardless of score",
                    )
                )
        remaining = kept
        paid_admitted = ()
        chosen = chosen if chosen in free_set else (kept[0] if kept else None)
    elif policy == SPEND_FRONTIER_REQUIRED:
        allowed = frozenset(frontier_allowlist) if frontier_allowlist else None
        kept = tuple(
            item
            for item in remaining
            if item not in free_set
            and (item in allowed if allowed is not None else is_frontier_identity(item))
        )
        for identity_id in remaining:
            if identity_id not in kept:
                exclusions.append(
                    NamedDrop(
                        identity_id,
                        REASON_SPEND_POLICY_REQUIRES_FRONTIER,
                        "spend_policy=frontier_required admits frontier-class paid identities only",
                    )
                )
        remaining = kept
        paid_admitted = kept
        chosen = chosen if chosen in kept else (kept[0] if kept else None)
    elif policy == SPEND_FREE_PREFERRED:
        # Free candidates keep competing; chosen prefers a qualified free
        # identity whenever one survived (paid stays fallback-only).
        chosen = (
            chosen
            if chosen in free_set
            else (next((item for item in remaining if item in free_set), chosen))
        )

    return replace(
        receipt,
        admitted=remaining,
        exclusions=tuple(exclusions),
        chosen=chosen,
        empty_intersection=chosen is None,
        task_class=task_class,
        class_reasons=class_reasons,
        free_admitted=free_admitted,
        paid_admitted=paid_admitted,
        task_profile_digest=task_profile_digest,
        spend_policy=policy,
    )


__all__ = [
    "DEFAULT_CHEAP_PATH_TOKEN_BUDGET",
    "FAIL_CLOSED_REASON",
    "NO_ELIGIBLE_TARGET",
    "REASON_CAPABILITY_MISMATCH",
    "REASON_INACTIVE_UNCONNECTED",
    "REASON_METADATA_GHOST",
    "REASON_NOT_FREE_TIER",
    "REASON_OPAQUE_AUTO",
    "REASON_REQUIRED_UNKNOWN",
    "REASON_SPEND_POLICY_EXCLUDES_PAID",
    "REASON_SPEND_POLICY_REQUIRES_FRONTIER",
    "REASON_STALE",
    "REASON_TASK_INSTRUCTIONS_OMITTED",
    "REASON_UNMAPPED",
    "REASON_WORTHY_EXCLUDES_FREE",
    "TASK_SOURCE_URI",
    "CatalogIdentity",
    "CheapPathContextPack",
    "FreeTierAdmitReceipt",
    "FreeTierModel",
    "IncludedProvenance",
    "LiveAdmitError",
    "NamedDrop",
    "NamedOmission",
    "OmniRouteAdmitSnapshot",
    "PackState",
    "ProviderConnection",
    "admit_free_tier_active",
    "build_cheap_path_context_pack",
    "execute_offload_chat",
    "expand_admit_for_worthiness",
    "is_frontier_identity",
    "load_omniroute_admit_snapshot",
    "normalize_omniroute_origin",
    "omniroute_endpoint_from_env",
    "parse_catalog_identities",
    "parse_free_tier_models",
    "parse_provider_connections",
    "snapshot_from_payloads",
]
