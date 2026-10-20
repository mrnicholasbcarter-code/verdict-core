"""Admit active-provider concrete identities for offloadable work.

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

This module is a candidate *feed* only. ``chosen`` is an advisory
inventory order, not a capability ranking. Serve callers apply candidate-pool
ranking, capability gates and fresh proof before selecting a route.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import quote, urlsplit, urlunsplit

import httpx

if TYPE_CHECKING:
    from verdict.admission import RuntimeEvidence

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
# Stable public eligibility diagnostics. Legacy reason strings remain
# accepted on old receipts, but new production decisions use these names.
REASON_OPAQUE_ROUTE_DISALLOWED = "opaque_route_disallowed"
REASON_PROVIDER_NOT_CONNECTED = "provider_not_connected"
REASON_PROVIDER_INACTIVE = "provider_inactive"
REASON_PROVIDER_UNHEALTHY = "provider_unhealthy"
REASON_CREDENTIAL_MISSING = "credential_missing"
REASON_PRICE_UNKNOWN = "price_unknown"
REASON_PAID_MODEL_NOT_ALLOWED = "paid_model_not_allowed"
REASON_SHAPE_UNKNOWN = "shape_unknown"
TASK_SOURCE_URI = "urn:verdict:task"
_COMBO_PREFIXES = frozenset({"claude", "combo"})
_ALIAS_PREFIXES = frozenset({"oc", "kr", "cf", "or", "nv"})
_SMALL_TOKENS = ("nano", "flash", "haiku", "mini", "small", "lite", "instant")

NO_ELIGIBLE_TARGET = "no_eligible_target"
FAIL_CLOSED_REASON = "fail_closed — empty active ∩ fresh-passport ∩ confirmed intersection"


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


def _provider_connection_usable(connection: ProviderConnection) -> bool:
    """True only for explicit active and healthy provider evidence."""
    if not connection.is_active:
        return False
    status = (connection.test_status or "").strip().lower()
    return status in {"active", "healthy", "ok", "ready", "connected"}


def _provider_connections_by_name(
    connections: tuple[ProviderConnection, ...],
) -> dict[str, ProviderConnection]:
    """Collapse duplicate rows, preferring a row with explicit healthy evidence."""
    selected: dict[str, ProviderConnection] = {}
    for connection in connections:
        current = selected.get(connection.provider)
        if current is None or (
            _provider_connection_usable(connection) and not _provider_connection_usable(current)
        ):
            selected[connection.provider] = connection
    return selected


def _provider_rejection(connection: ProviderConnection | None) -> tuple[str, str] | None:
    """Return a stable fail-closed reason, or None for explicitly healthy."""
    if connection is None:
        return REASON_PROVIDER_NOT_CONNECTED, "provider has no OmniRoute connection"
    if not connection.is_active:
        return REASON_PROVIDER_INACTIVE, "provider connection is inactive"
    status = (connection.test_status or "").strip().lower()
    if status in {"unauthorized", "auth_failed", "authentication_failed", "credential_missing"}:
        return REASON_CREDENTIAL_MISSING, f"provider connection status={status}"
    if not status:
        return REASON_PROVIDER_UNHEALTHY, "provider health status is unknown"
    if not _provider_connection_usable(connection):
        return REASON_PROVIDER_UNHEALTHY, f"provider connection status={status}"
    return None


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
    # Economic status is evidence, not a model-name heuristic. ``price_known``
    # is true only when OmniRoute supplied parseable pricing on the inventory
    # row. Free membership still comes exclusively from /api/free-tier/summary.
    price_known: bool = False
    input_cost: float | None = None
    output_cost: float | None = None
    # Catalog context limit when the inventory row carries one
    # (context_window / context_length / context); None means the catalog
    # did not say. Caps the census needle steps for the route.
    max_context_tokens: int | None = None


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
    """Normalized live (or fixture) surfaces for active admit."""

    catalog: tuple[CatalogIdentity, ...]
    free_tier: tuple[FreeTierModel, ...]
    connections: tuple[ProviderConnection, ...]
    passive_health: RuntimeEvidence | None = None
    quota_blocks: tuple[NamedDrop, ...] = ()
    # Admit surfaces served from the last-good snapshot after a live read failed.
    stale_served: tuple[str, ...] = ()
    # BOD-345/S04 raw inventory accounting. ``fetched_at`` is the source
    # timestamp of the live read; it never renews the observation time of
    # stale-served surfaces. ``inventory_complete`` is False only when the
    # catalog shape could not be accounted for: unknown, never observed zero.
    fetched_at: datetime | None = None
    inventory_complete: bool = True
    inventory_diagnostics: tuple[NamedDrop, ...] = ()
    # Gateway telemetry/scope fields observed but not interpreted by this
    # adapter: explicitly recorded as unknown, never treated as health.
    unsupported_fields: tuple[str, ...] = ()

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
    """Compiled task pack used on the active offload path."""

    pack_digest: str
    compiled_prompt: str
    omissions: tuple[NamedOmission, ...]
    pack_id: str
    plan_digest: str
    units: tuple[ContextUnit, ...] = ()
    included: tuple[IncludedProvenance, ...] = ()
    pack_state: PackState = "empty"
    # pack completeness contract: the task instructions must be packed, and
    # every task-required source must be included, before ``hydrated`` is possible.
    task_complete: bool = True
    required_sources: tuple[str, ...] = ()
    # Coverage contract: acceptance criteria / proof criteria / verification
    # commands that must appear in the compiled pack for ``hydrated``.
    required_facts: tuple[str, ...] = ()
    satisfied_facts: tuple[str, ...] = ()
    # BudgetReceipt + Context Trust feed for execution-path authority offers.
    budget_receipt: BudgetReceipt | None = None
    context_trust_admitted: bool = True
    capability_coverage: dict[str, Any] | None = None

    @property
    def included_sources(self) -> tuple[IncludedProvenance, ...]:
        """Receipt-facing alias of ``included`` (pack_state classification / QA smoke field)."""
        return self.included

    @property
    def missing_required_sources(self) -> tuple[str, ...]:
        included = {item.source_uri for item in self.included}
        return tuple(uri for uri in self.required_sources if uri not in included)

    @property
    def unsatisfied_facts(self) -> tuple[str, ...]:
        """Coverage-contract facts not carried by the compiled pack."""
        satisfied = frozenset(self.satisfied_facts)
        return tuple(f for f in self.required_facts if f not in satisfied)

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
            "required_facts": list(self.required_facts),
            "satisfied_facts": list(self.satisfied_facts),
            "unsatisfied_facts": list(self.unsatisfied_facts),
            "included": sources,
            "included_sources": list(sources),
            "omissions": [item.to_dict() for item in self.omissions],
            "budget_receipt": None
            if self.budget_receipt is None
            else self.budget_receipt.to_dict(),
            "context_trust_admitted": self.context_trust_admitted,
            "capability_coverage": self.capability_coverage,
        }


def _normalize_fact(text: str) -> str:
    """Collapse whitespace and lowercase for fact-satisfaction comparison."""
    return " ".join(text.split()).lower()


def _derive_required_facts(
    acceptance_criteria: Sequence[str],
    proof_criteria: Sequence[str],
    verification_commands: Sequence[Sequence[str]] = (),
) -> tuple[str, ...]:
    """Build the coverage-contract fact list from task criteria.

    Each acceptance criterion, proof criterion, and verification command
    becomes a mandatory fact that must appear in the compiled pack for the
    pack to reach ``hydrated``.
    """
    facts: list[str] = []
    for ac in acceptance_criteria:
        if ac and ac.strip():
            facts.append(ac.strip())
    for pc in proof_criteria:
        if pc and pc.strip():
            facts.append(pc.strip())
    for cmd in verification_commands:
        joined = " ".join(cmd).strip()
        if joined:
            facts.append(f"verification_command:{joined}")
    return tuple(facts)


def _check_fact_satisfaction(
    required_facts: Sequence[str], units: Sequence[object]
) -> tuple[str, ...]:
    """Return which required facts are carried by included units.

    A fact is satisfied when its whitespace-collapsed, case-insensitive text
    appears as a substring in any included unit's content.
    """
    if not required_facts:
        return ()
    # Pre-normalize all unit contents once.
    contents: list[str] = []
    for unit in units:
        raw = getattr(unit, "content", None)
        if raw and isinstance(raw, str):
            contents.append(_normalize_fact(raw))
    satisfied: list[str] = []
    for fact in required_facts:
        needle = _normalize_fact(fact)
        if any(needle in c for c in contents):
            satisfied.append(fact)
    return tuple(satisfied)


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
    verification_commands: Sequence[Sequence[str]] = (),
    errors: Sequence[str] = (),
    memory_path: Path | str | None = None,
    use_context_fabric: bool = False,
) -> CheapPathContextPack:
    """Compile a provenance-rich context pack for cheap-path offload.

    Gather real workspace units (repo docs / architecture / ADRs / project docs,
    plus MCP only when a source is configured), admit every external unit through
    Context Trust, allocate via BudgetReceipt, then compile
    only the allocated set. High-value roots (ADR, architecture, README) remain
    preferred under budget. Missing sources become named omissions — never
    invented content. An empty gather still compiles the task and does not block
    execute.

    ``pack_state`` classifies the result for receipts. Savings stay
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
    # Coverage-contract: derive required facts and pack criteria as mandatory
    # instruction units so they are carried to the model and satisfiable.
    required_facts = _derive_required_facts(
        acceptance_criteria, proof_criteria, verification_commands
    )
    criteria_slots: list[ContextPackSlot] = []
    if acceptance_criteria:
        criteria_slots.append(
            ContextPackSlot(
                slot_type="instructions",
                key="acceptance_criteria",
                content="\n".join(f"- {ac}" for ac in acceptance_criteria if ac.strip()),
                source="cheap_path",
                created_at=0.0,
                source_uri="urn:verdict:source:acceptance_criteria",
            )
        )
    if proof_criteria:
        criteria_slots.append(
            ContextPackSlot(
                slot_type="instructions",
                key="proof_criteria",
                content="\n".join(f"- {pc}" for pc in proof_criteria if pc.strip()),
                source="cheap_path",
                created_at=0.0,
                source_uri="urn:verdict:source:proof_criteria",
            )
        )
    if verification_commands:
        vc_lines = [
            f"- verification_command:{' '.join(cmd)}"
            for cmd in verification_commands
            if cmd and " ".join(cmd).strip()
        ]
        if vc_lines:
            criteria_slots.append(
                ContextPackSlot(
                    slot_type="instructions",
                    key="verification_commands",
                    content="\n".join(vc_lines),
                    source="cheap_path",
                    created_at=0.0,
                    source_uri="urn:verdict:source:verification_commands",
                )
            )
    slots = (task_slot, *criteria_slots, *(extra_slots or ()))
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
            # Mandatory task cannot fit usable budget — pack completeness contract failed pack.
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
    satisfied_facts = _check_fact_satisfaction(required_facts, pack.units)
    pack_state = classify_pack_state(
        included=included,
        gathered=gathered.units,
        omissions=omissions,
        required=gathered.required_uris,
        task_complete=task_complete,
        required_facts=required_facts,
        satisfied_facts=satisfied_facts,
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
        required_facts=required_facts,
        satisfied_facts=satisfied_facts,
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
    """Run security boundary admit on every external unit before model-bound compile."""
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
        """Receipt-facing alias of ``included`` (pack_state classification / QA smoke field)."""
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
        # R5 (phase g): O3 declared tiers, only in VERDICT_PIPELINE_RANKING_MODE=write.
        declared = load_declared_tiers()
        for model_id in self.admitted:
            identity = by_id.get(model_id)
            provider = identity.provider if identity is not None else _provider_of(model_id)
            admitted_models.append(
                ModelInfo(
                    id=model_id,
                    provider=provider,
                    capability_tier=classify(model_id, overrides=declared),
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
                reason="active provider",
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


RANKING_MODE_ENV = "VERDICT_PIPELINE_RANKING_MODE"
DECLARED_TIERS_FILE = "declared-tiers.json"
O3_TIER1_MIN_ELO = 1450
O3_TIER2_MIN_ELO = 1380


def declared_tier_from_elo(elo: float) -> int | None:
    """O3 bands (controller 10:05Z): tier 1 at Elo >= 1450, tier 2 at 1380-1449, else None."""
    if elo >= O3_TIER1_MIN_ELO:
        return 1
    if elo >= O3_TIER2_MIN_ELO:
        return 2
    return None


def load_declared_tiers(path: Path | None = None) -> dict[str, int]:
    """Exact full route id -> O3 declared tier (1 or 2), for ``classify`` overrides.

    Empty unless ``VERDICT_PIPELINE_RANKING_MODE=write``. The file (phase d's
    ranking output) maps route ids to text Elo, so a tier is always derived from
    the bands and tier 0 is never declared. Ids whose pattern tier is 0 are left
    out so tier 0 stays pattern-only and the frontier spend guard is unchanged.
    """
    if os.getenv(RANKING_MODE_ENV, "").strip().lower() != "write":
        return {}
    if path is None:
        from verdict.catalog_cache import default_catalog_cache_path

        path = default_catalog_cache_path().parent / DECLARED_TIERS_FILE
    import json

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    declared: dict[str, int] = {}
    for route, elo in data.items():
        if isinstance(elo, bool) or not isinstance(elo, int | float):
            continue
        tier = declared_tier_from_elo(float(elo))
        if tier is not None and classify(str(route)) != 0:
            declared[str(route)] = tier
    return declared


def _verdict_for_reason(reason: str) -> EligibilityVerdict:
    mapping = {
        REASON_OPAQUE_AUTO: EligibilityVerdict.OPAQUE_AUTO,
        REASON_OPAQUE_ROUTE_DISALLOWED: EligibilityVerdict.OPAQUE_AUTO,
        REASON_NOT_FREE_TIER: EligibilityVerdict.NOT_FREE_TIER,
        REASON_INACTIVE_UNCONNECTED: EligibilityVerdict.INACTIVE_UNCONNECTED,
        REASON_PROVIDER_NOT_CONNECTED: EligibilityVerdict.INACTIVE_UNCONNECTED,
        REASON_PROVIDER_INACTIVE: EligibilityVerdict.INACTIVE_UNCONNECTED,
        REASON_PROVIDER_UNHEALTHY: EligibilityVerdict.NOT_LIVE_ELIGIBLE,
        REASON_CREDENTIAL_MISSING: EligibilityVerdict.NOT_LIVE_ELIGIBLE,
        REASON_PRICE_UNKNOWN: EligibilityVerdict.REQUIRED_UNKNOWN,
        REASON_PAID_MODEL_NOT_ALLOWED: EligibilityVerdict.NOT_FREE_TIER,
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


def why_not_model(receipt: FreeTierAdmitReceipt, model_id: str) -> tuple[dict[str, str], ...]:
    """Return deterministic bounded rejection evidence for one candidate."""
    if model_id in receipt.admitted:
        return ()
    rows = [drop.to_dict() for drop in receipt.exclusions if drop.model_id == model_id]
    if rows:
        return tuple(rows)
    return (
        {
            "model": model_id,
            "reason": "not_discovered",
            "detail": "candidate was not present in the evaluated inventory",
        },
    )


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


def _number(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _catalog_prices(row: Mapping[str, Any]) -> tuple[bool, float | None, float | None]:
    """Parse only explicit OmniRoute inventory pricing; absent means unknown."""
    pricing = row.get("pricing")
    source = pricing if isinstance(pricing, Mapping) else row

    def first_present(*names: str) -> object:
        for name in names:
            if name in source and source[name] is not None:
                return source[name]
        return None

    input_price = _number(first_present("input", "input_cost", "input_cost_per_million", "prompt"))
    output_price = _number(
        first_present("output", "output_cost", "output_cost_per_million", "completion")
    )
    return input_price is not None and output_price is not None, input_price, output_price


def _catalog_context_limit(row: Mapping[str, Any]) -> int | None:
    """Positive context limit from the inventory row; absent means unknown."""
    for key in ("context_window", "context_length", "context"):
        value = row.get(key)
        if isinstance(value, int | float) and not isinstance(value, bool) and value > 0:
            return int(value)
    return None


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
        price_known, input_cost, output_cost = _catalog_prices(row)
        out.append(
            CatalogIdentity(
                identity_id=identity_id,
                provider=provider,
                price_known=price_known,
                input_cost=input_cost,
                output_cost=output_cost,
                max_context_tokens=_catalog_context_limit(row),
            )
        )
    return tuple(out)


def _quota_blocks(payload: object) -> tuple[NamedDrop, ...]:
    """Fence a provider only when every reported account is exhausted.

    Provider routes cannot pin a connection; OmniRoute selects an available account.
    Availability requires positive evidence (explicit flag or reported capacity), not the absence of a block.
    `available: True` alone (no quota data) is sufficient positive evidence.
    """
    if not isinstance(payload, Mapping):
        return ()
    rows = payload.get("accountQuotas", [])
    if not isinstance(rows, list):
        return ()
    blocks: list[NamedDrop] = []
    available_providers: set[str] = set()
    now = datetime.now(timezone.utc)
    for row in rows:
        if not isinstance(row, Mapping) or not isinstance(row.get("provider"), str):
            continue
        provider = row["provider"]
        before = len(blocks)
        has_capacity = False
        account = row.get("connectionId", "unknown")
        if row.get("available") is False and row.get("reason") != "not_supported":
            blocks.append(
                NamedDrop(
                    provider,
                    "quota_exhausted",
                    f"account={account}: {row.get('reason', 'unavailable')}",
                )
            )
        quotas = row.get("quotas", {})
        if not isinstance(quotas, Mapping):
            quotas = {}
        for window, quota in quotas.items():
            if not isinstance(quota, Mapping):
                continue
            used = _number(quota.get("usedPercentage"))
            if used is None or used < 100:
                if used is not None:
                    has_capacity = True
                continue
            reset = quota.get("resetAt")
            if isinstance(reset, str):
                try:
                    reset_time = datetime.fromisoformat(reset.replace("Z", "+00:00"))
                    if reset_time.tzinfo is not None and reset_time <= now:
                        has_capacity = True
                        continue
                except ValueError:
                    pass
            blocks.append(
                NamedDrop(
                    provider,
                    "quota_exhausted",
                    f"account={account} window={window} used={used}% resetAt={reset}",
                )
            )
        if len(blocks) == before and (row.get("available") is True or has_capacity):
            available_providers.add(provider)
    return tuple(block for block in blocks if block.model_id not in available_providers)


def _rows_of(payload: object, keys: tuple[str, ...]) -> list[object] | None:
    """The row list of a surface payload, or None when the shape is unknown."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, Mapping):
        for key in keys:
            value = payload.get(key)
            if isinstance(value, list):
                return value
    return None


def _surface_shape_diagnostics(
    catalog: object, free_tier: object, providers: object, account_status: object
) -> tuple[NamedDrop, ...]:
    """Surfaces whose deployed shape we cannot account for stay unknown.

    An unaccountable surface is never reported as an observed zero: it is
    recorded as an explicit shape diagnostic so downstream consumers fail
    closed instead of trusting an empty intersection.
    """
    drops: list[NamedDrop] = []
    if _surface_rows(catalog) is None:
        drops.append(
            NamedDrop(
                "catalog", REASON_SHAPE_UNKNOWN, "catalog payload is not a supported row list"
            )
        )
    if _rows_of(free_tier, ("perModel", "models", "data", "items")) is None:
        drops.append(
            NamedDrop(
                "free_tier", REASON_SHAPE_UNKNOWN, "free-tier payload is not a supported row list"
            )
        )
    providers_rows = _rows_of(providers, ("connections", "providers", "data"))
    single_row = isinstance(providers, Mapping) and (
        isinstance(providers.get("provider"), str) or "isActive" in providers
    )
    if providers_rows is None and not single_row:
        drops.append(
            NamedDrop(
                "providers", REASON_SHAPE_UNKNOWN, "provider payload is not a supported row list"
            )
        )
    if _rows_of(account_status, ("accountQuotas",)) is None:
        drops.append(
            NamedDrop(
                "account_status",
                REASON_SHAPE_UNKNOWN,
                "account status payload is not a supported row list",
            )
        )
    return tuple(drops)


_UNSUPPORTED_TELEMETRY_KEYS = frozenset(
    {
        "analytics",
        "telemetry",
        "mcp",
        "successrate",
        "success_rate",
        "ratelimit",
        "rate_limit",
        "ratelimits",
        "rate_limits",
    }
)


def _unsupported_fields(payloads: Mapping[str, object]) -> tuple[str, ...]:
    """Gateway telemetry fields observed but not interpreted: explicit unknowns.

    A nested analytics successRate=1/CLOSED block or an empty MCP array is
    recorded here instead of silently leaking into health decisions.
    """
    found: set[str] = set()

    def scan(node: object, depth: int) -> None:
        if depth > 2:
            return
        if isinstance(node, list | tuple):
            for item in node:
                scan(item, depth)
            return
        if not isinstance(node, Mapping):
            return
        for key, value in node.items():
            name = str(key)
            if name.lower() in _UNSUPPORTED_TELEMETRY_KEYS:
                found.add(name)
            scan(value, depth + 1)

    for payload in payloads.values():
        scan(payload, 0)
    return tuple(sorted(found))


def snapshot_from_payloads(
    *, catalog: object, free_tier: object, providers: object, account_status: object = None
) -> OmniRouteAdmitSnapshot:
    return OmniRouteAdmitSnapshot(
        catalog=parse_catalog_identities(catalog),
        free_tier=parse_free_tier_models(free_tier),
        connections=parse_provider_connections(providers),
        quota_blocks=_quota_blocks(account_status),
        inventory_complete=_surface_rows(catalog) is not None,
        inventory_diagnostics=_surface_shape_diagnostics(
            catalog, free_tier, providers, account_status
        ),
        unsupported_fields=_unsupported_fields(
            {
                "catalog": catalog,
                "free_tier": free_tier,
                "providers": providers,
                "account_status": account_status,
            }
        ),
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
    """Free-first ordering keyed on authoritative free-tier membership.

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
    # Provider ownership is an authoritative part of the catalog identity.
    # A suffix collision in another provider's inventory must never inherit this
    # row's free-tier or connection evidence.
    matches = [
        item.identity_id
        for item in catalog
        if item.provider == provider and _matches_free_model(item.identity_id, model_id, provider)
    ]
    concrete = [item for item in matches if not is_opaque_route_id(item)]
    if not concrete:
        return tuple(matches)
    concrete.sort(key=lambda identity_id: _identity_rank(identity_id, provider))
    best_rank = _identity_rank(concrete[0], provider)[:3]
    return tuple(item for item in concrete if _identity_rank(item, provider)[:3] == best_rank)


def admit_free_tier_active(snapshot: OmniRouteAdmitSnapshot) -> FreeTierAdmitReceipt:
    """Admit concrete catalog identities on active, healthy, quota-available providers.

    Pricing and free-tier membership describe inventory but never restrict it.
    Callers must still apply task capability and fresh proof/confirmation gates.
    """
    exclusions: list[NamedDrop] = []
    admitted: list[str] = []
    free_ids: set[str] = set()
    connections = _provider_connections_by_name(snapshot.connections)

    def provider_reason(provider: str) -> tuple[str, str] | None:
        block = next((row for row in snapshot.quota_blocks if row.model_id == provider), None)
        if block is not None:
            return block.reason, block.detail or "account quota exhausted"
        return _provider_rejection(connections.get(provider))

    for row in snapshot.free_tier:
        label = f"{row.provider}/{row.model_id}"
        if is_opaque_route_id(row.model_id) or is_opaque_route_id(label):
            exclusions.append(
                NamedDrop(label, REASON_OPAQUE_ROUTE_DISALLOWED, "opaque route is not auditable")
            )
            continue
        blocked = provider_reason(row.provider)
        if blocked is not None:
            exclusions.append(NamedDrop(label, blocked[0], blocked[1]))
            continue
        matches = resolve_catalog_matches(row.model_id, row.provider, snapshot.catalog)
        concrete = [item for item in matches if not is_opaque_route_id(item)]
        if not concrete:
            reason = REASON_OPAQUE_ROUTE_DISALLOWED if matches else REASON_METADATA_GHOST
            exclusions.append(
                NamedDrop(label, reason, "free-tier row has no concrete inventory identity")
            )
        elif (row.free_type or "").lower() != "discontinued":
            free_ids.update(concrete)

    for identity in snapshot.catalog:
        blocked = provider_reason(identity.provider)
        if blocked is not None:
            exclusions.append(NamedDrop(identity.identity_id, blocked[0], blocked[1]))
        elif is_opaque_route_id(identity.identity_id) or _is_combo_identity(identity.identity_id):
            exclusions.append(
                NamedDrop(
                    identity.identity_id,
                    REASON_OPAQUE_ROUTE_DISALLOWED,
                    "opaque/combo route is not a concrete auditable identity",
                )
            )
        else:
            admitted.append(identity.identity_id)

    admitted_sorted = tuple(sorted(set(admitted)))
    healthy = tuple(
        sorted(provider for provider in connections if provider_reason(provider) is None)
    )
    return FreeTierAdmitReceipt(
        admitted=admitted_sorted,
        exclusions=tuple(exclusions),
        chosen=admitted_sorted[0] if admitted_sorted else None,
        empty_intersection=not admitted_sorted,
        active_providers=healthy,
        free_tier_providers=tuple(sorted(snapshot.free_tier_providers)),
        free_admitted=tuple(item for item in admitted_sorted if item in free_ids),
        paid_admitted=tuple(item for item in admitted_sorted if item not in free_ids),
    )


ADMIT_SURFACE_TIMEOUT_SECONDS = 20.0
ADMIT_CATALOG_TIMEOUT_SECONDS = 20.0
ADMIT_READ_ATTEMPTS = 2
ADMIT_LAST_GOOD_MAX_AGE_SECONDS = 15 * 60
ADMIT_LAST_GOOD_FILE = "admit-last-good.json"
CENSUS_CATALOG_CACHE_FILE = "census-catalog-cache.json"
PROVIDER_MODELS_PATH = "/api/v1/providers/{provider}/models"
# BOD-345/S04 discovery bounds (V1 contract section 22.3): 20s per HTTP read,
# a 30s aggregate discovery deadline, a 16 MiB decoded budget, 50,000 raw
# rows and at most 100 pages. Exceeding any bound yields an
# ``incomplete_source`` error and never publishes a truncated inventory as
# complete; original-deadline last-good retention rules still apply.
ADMIT_AGGREGATE_DEADLINE_SECONDS = 30.0
ADMIT_DECODED_BYTE_BUDGET = 16 * 1024 * 1024
ADMIT_RAW_ROW_LIMIT = 50_000
ADMIT_PAGE_LIMIT = 100
_RETRYABLE_ADMIT_ERRORS = frozenset({"timeout", "transport", "http_error"})
# Bound violations are deterministic and never retried, but like other read
# failures they may still be served from a valid original-deadline last-good
# snapshot instead of publishing truncated data as complete.
_MASKABLE_ADMIT_ERRORS = _RETRYABLE_ADMIT_ERRORS | frozenset({"incomplete_source"})


def default_admit_last_good_path() -> Path:
    """Last-good admit payloads (all but the catalog) live beside the catalog cache."""
    from verdict.catalog_cache import default_catalog_cache_path

    return default_catalog_cache_path().parent / ADMIT_LAST_GOOD_FILE


def _read_last_good(path: Path, origin: str, now: datetime, max_age: float) -> dict[str, object]:
    import json

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    entry = data.get(origin) if isinstance(data, dict) else None
    if not isinstance(entry, dict):
        return {}
    fresh: dict[str, object] = {}
    for name, row in entry.items():
        if not isinstance(row, dict) or not isinstance(row.get("fetched_at"), str):
            continue
        try:
            age = (now - datetime.fromisoformat(row["fetched_at"])).total_seconds()
        except (TypeError, ValueError):
            continue
        if 0 <= age < max_age and "payload" in row:
            fresh[name] = row["payload"]
    return fresh


def _write_last_good(
    path: Path, origin: str, payloads: Mapping[str, object], now: datetime
) -> None:
    """Merge fresh surfaces into the last-good file with an atomic replace."""
    import json
    import tempfile

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    entry = data.get(origin)
    if not isinstance(entry, dict):
        entry = {}
    for name, payload in payloads.items():
        entry[name] = {"fetched_at": now.isoformat(), "payload": payload}
    data[origin] = entry
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def default_census_catalog_cache_path() -> Path:
    """Gated per-provider catalogs are cached apart from the full catalog cache."""
    from verdict.catalog_cache import default_catalog_cache_path

    return default_catalog_cache_path().parent / CENSUS_CATALOG_CACHE_FILE


def _gated_providers(free_tier: object, providers: object, account_status: object) -> list[str]:
    """Providers admit can admit from: free-tier summary rows with an active connection."""
    probe = snapshot_from_payloads(
        catalog={"data": []},
        free_tier=free_tier,
        providers=providers,
        account_status=account_status,
    )
    return sorted(probe.free_tier_providers & probe.active_providers)


def _merge_provider_catalogs(by_provider: Mapping[str, object]) -> dict[str, object]:
    """Merge per-provider model lists into one /v1/models-shaped catalog.

    Per-provider ids are upstream ids (verified on OmniRoute 3.8.51: nvidia
    returns ``nvidia/nemotron-...`` and ``meta/llama-...``), while the unified
    catalog id is always ``<provider>/<upstream id>``, so every id gets the
    provider prefix, even one that already starts with the provider name.
    The body is ``{"object": "list", "data": [...]}``; a non-list body
    contributes none.
    """
    rows: list[object] = []
    for provider, payload in sorted(by_provider.items()):
        data = payload.get("data") if isinstance(payload, Mapping) else payload
        if not isinstance(data, list):
            continue
        for row in data:
            if isinstance(row, Mapping) and isinstance(row.get("id"), str):
                row = {**row, "id": f"{provider}/{row['id']}"}
            rows.append(row)
    return {"object": "list", "data": rows}


class _DecodedBudget:
    """Thread-safe cumulative decoded-byte budget for one discovery run."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self._used = 0
        self._lock = threading.Lock()

    def charge(self, nbytes: int) -> bool:
        with self._lock:
            if self._used + nbytes > self.limit:
                return False
            self._used += nbytes
            return True


def _read_admit_surface(
    client: httpx.Client,
    url: str,
    path: str,
    headers: Mapping[str, str],
    timeout: float,
    budget: _DecodedBudget | None = None,
) -> object:
    try:
        response = client.get(url, headers=dict(headers), timeout=timeout)
    except httpx.TimeoutException as exc:
        raise LiveAdmitError("timeout", f"timed out reading {path}") from exc
    except httpx.HTTPError as exc:
        raise LiveAdmitError("transport", f"{path}: {type(exc).__name__}") from exc
    if response.status_code in {401, 403}:
        raise LiveAdmitError("unauthorized", f"{path} returned {response.status_code}")
    if response.status_code == 404:
        raise LiveAdmitError("unsupported", f"{path} is unavailable")
    if not 200 <= response.status_code < 300:
        raise LiveAdmitError("http_error", f"{path} returned {response.status_code}")
    nbytes = len(response.content)
    if budget is not None and not budget.charge(nbytes):
        raise LiveAdmitError(
            "incomplete_source",
            f"{path} exceeded the decoded byte budget ({nbytes} bytes, limit {budget.limit})",
        )
    try:
        return response.json()
    except ValueError as exc:
        raise LiveAdmitError("malformed", f"{path} is not JSON") from exc


def _surface_rows(payload: object) -> list[object] | None:
    """The row list of a catalog surface payload, or None when unaccountable."""
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, Mapping):
        return None
    for key in ("data", "models", "items"):
        value = payload.get(key)
        if isinstance(value, list):
            return value
    catalog = payload.get("catalog")
    if isinstance(catalog, Mapping):
        rows: list[object] = []
        for group in catalog.values():
            if isinstance(group, Mapping) and isinstance(group.get("models"), list):
                rows.extend(group["models"])
        return rows
    return None


def _next_page_cursor(payload: object) -> str | None:
    """The adapter-supported ``after`` id cursor of a catalog page, if any."""
    if not isinstance(payload, Mapping):
        return None
    for key in ("next_cursor", "nextCursor", "cursor"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


_PAGINATION_TOKEN_FIELDS = frozenset({"nextPageToken", "next_page_token", "nextPage", "next_page"})
_PAGINATION_MORE_FIELDS = frozenset({"hasMore", "has_more"})


def _unsupported_pagination_fields(payload: object) -> tuple[str, ...]:
    """Pagination hints this adapter cannot follow: incomplete by definition."""
    if not isinstance(payload, Mapping):
        return ()
    found = [key for key in sorted(_PAGINATION_TOKEN_FIELDS) if key in payload]
    found += [key for key in sorted(_PAGINATION_MORE_FIELDS) if payload.get(key)]
    return tuple(found)


def _read_catalog_pages(
    client: httpx.Client,
    origin: str,
    path: str,
    headers: Mapping[str, str],
    *,
    per_request_timeout: float,
    tries: int,
    pause: Callable[[float], None],
    deadline: float,
    budget: _DecodedBudget,
    row_limit: int,
    page_limit: int,
) -> object | LiveAdmitError:
    """Read a catalog surface page by page under explicit discovery bounds.

    Only the adapter-supported ``after`` id cursor is followed. Repeated
    cursors, inconsistent pages, unsupported pagination hints and any exceeded
    bound (aggregate deadline, decoded bytes, raw rows, pages) fail closed
    with ``incomplete_source``: a truncated inventory is never returned as a
    complete one.
    """
    import random
    import time

    rows: list[object] = []
    seen_ids: set[str] = set()
    seen_cursors: set[str] = set()
    cursor: str | None = None
    for _page in range(max(1, page_limit)):
        url = f"{origin}{path}"
        if cursor is not None:
            url = f"{url}?after={quote(cursor, safe='')}"
        payload: object | None = None
        for attempt in range(tries):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return LiveAdmitError("timeout", f"aggregate discovery deadline exceeded at {path}")
            try:
                payload = _read_admit_surface(
                    client, url, path, headers, min(per_request_timeout, remaining), budget=budget
                )
                break
            except LiveAdmitError as exc:
                if exc.code not in _RETRYABLE_ADMIT_ERRORS or attempt + 1 >= tries:
                    return exc
                pause(random.uniform(0.25, 1.0) * (attempt + 1))
        assert payload is not None
        unsupported = _unsupported_pagination_fields(payload)
        if unsupported:
            return LiveAdmitError(
                "incomplete_source",
                f"{path} uses unsupported pagination field(s): {', '.join(unsupported)}",
            )
        page_rows = _surface_rows(payload)
        if page_rows is None:
            # Unrecognized shape: not countable, not pageable. Returned as-is;
            # the snapshot records the surface as unknown, never as zero.
            return payload
        followed = cursor is not None
        new_rows = 0
        for row in page_rows:
            row_id = row.get("id") if isinstance(row, Mapping) else None
            if isinstance(row_id, str):
                if row_id in seen_ids:
                    continue
                seen_ids.add(row_id)
            new_rows += 1
        rows.extend(page_rows)
        if followed and page_rows and new_rows == 0:
            return LiveAdmitError(
                "incomplete_source", f"{path} page after cursor {cursor!r} repeated every row"
            )
        if len(rows) > row_limit:
            return LiveAdmitError(
                "incomplete_source", f"{path} exceeded the raw row limit {row_limit}"
            )
        nxt = _next_page_cursor(payload)
        if nxt is None:
            return payload if not followed else {"object": "list", "data": rows}
        if nxt in seen_cursors:
            return LiveAdmitError("incomplete_source", f"{path} repeated pagination cursor {nxt!r}")
        seen_cursors.add(nxt)
        cursor = nxt
    return LiveAdmitError("incomplete_source", f"{path} exceeded the page limit {page_limit}")


def load_omniroute_admit_snapshot(
    base_url: str,
    api_key: str | None = None,
    *,
    timeout: float = ADMIT_SURFACE_TIMEOUT_SECONDS,
    catalog_timeout: float = ADMIT_CATALOG_TIMEOUT_SECONDS,
    attempts: int = ADMIT_READ_ATTEMPTS,
    last_good_path: Path | None = None,
    last_good_max_age_seconds: float = ADMIT_LAST_GOOD_MAX_AGE_SECONDS,
    sleep: Callable[[float], None] | None = None,
    transport: httpx.BaseTransport | None = None,
    catalog_scope: str = "full",
    catalog_cache_path: Path | None = None,
    aggregate_deadline_seconds: float = ADMIT_AGGREGATE_DEADLINE_SECONDS,
    decoded_byte_budget: int = ADMIT_DECODED_BYTE_BUDGET,
    raw_row_limit: int = ADMIT_RAW_ROW_LIMIT,
    page_limit: int = ADMIT_PAGE_LIMIT,
) -> OmniRouteAdmitSnapshot:
    """Fetch catalog, free-tier summary, and provider connections from OmniRoute.

    Surfaces are read in parallel under the S04 discovery bounds: a 20-second
    per-HTTP timeout, a ``aggregate_deadline_seconds`` wall-clock discovery
    deadline, a ``decoded_byte_budget`` cumulative decoded-size budget, a
    ``raw_row_limit`` raw-row cap and a ``page_limit`` pagination cap. The
    catalog walk follows only the adapter-supported ``after`` id cursor and
    rejects repeated cursors, inconsistent pages and unsupported pagination
    hints. Exceeding any bound raises ``incomplete_source`` and never
    publishes a truncated inventory as complete. Timeouts, transport errors
    and 5xx are retried once with jitter. A surface that still fails is served
    from the last-good snapshot when it is younger than
    ``last_good_max_age_seconds`` and is listed in ``stale_served``; the
    original fetched-at deadline is never renewed. Without a usable last-good
    copy the read fails closed. Auth and unsupported-surface errors are never
    masked. This path never reads auto/* or combo routes.

    ``catalog_scope="gated"`` (the census) skips the ~27 MB ``/v1/models`` pull
    and reads ``/api/v1/providers/{p}/models`` only for providers admit can
    admit from (free-tier rows with an active connection), cached separately.
    ``"full"`` stays the default because other admit consumers explain
    exclusions across the whole inventory (``admit_free_tier_active`` walks
    every catalog identity), which a gated subset cannot do.
    """
    if catalog_scope not in {"full", "gated"}:
        raise ValueError("catalog_scope must be 'full' or 'gated'")
    gated = catalog_scope == "gated"
    import logging
    import random
    import time
    from concurrent.futures import ThreadPoolExecutor

    from verdict.catalog_cache import get_cached_catalog, put_cached_catalog

    log = logging.getLogger(__name__)
    origin = normalize_omniroute_origin(base_url)
    headers = {"accept": "application/json"}
    if api_key and api_key.strip():
        headers["authorization"] = f"Bearer {api_key.strip()}"
    pause = sleep or time.sleep
    tries = max(1, attempts)
    store = last_good_path or default_admit_last_good_path()

    now_utc = datetime.now(timezone.utc)
    catalog_path = (catalog_cache_path or default_census_catalog_cache_path()) if gated else None
    cached_catalog = get_cached_catalog(origin, api_key, now=now_utc, cache_path=catalog_path)
    paths: dict[str, str] = {}
    if cached_catalog is None and not gated:
        paths["catalog"] = "/v1/models"
    paths["free_tier"] = "/api/free-tier/summary"
    paths["providers"] = "/api/providers"
    paths["account_status"] = "/api/v1/me/status"

    payloads: dict[str, object] = {}
    if cached_catalog is not None:
        payloads["catalog"] = cached_catalog
    failures: dict[str, LiveAdmitError] = {}
    deadline = time.monotonic() + aggregate_deadline_seconds
    budget = _DecodedBudget(max(1, decoded_byte_budget))

    def fetch(item: tuple[str, str]) -> tuple[str, object | LiveAdmitError]:
        name, path = item
        limit = catalog_timeout if name == "catalog" or name.startswith("model:") else timeout
        if name == "catalog" or name.startswith("model:"):
            return name, _read_catalog_pages(
                client,
                origin,
                path,
                headers,
                per_request_timeout=limit,
                tries=tries,
                pause=pause,
                deadline=deadline,
                budget=budget,
                row_limit=raw_row_limit,
                page_limit=page_limit,
            )
        for attempt in range(tries):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return name, LiveAdmitError(
                    "timeout", f"aggregate discovery deadline exceeded at {path}"
                )
            try:
                return name, _read_admit_surface(
                    client, f"{origin}{path}", path, headers, min(limit, remaining), budget=budget
                )
            except LiveAdmitError as exc:
                if exc.code not in _RETRYABLE_ADMIT_ERRORS or attempt + 1 >= tries:
                    return name, exc
                pause(random.uniform(0.25, 1.0) * (attempt + 1))
        raise AssertionError("unreachable")

    with (
        httpx.Client(transport=transport, timeout=timeout, follow_redirects=False) as client,
        ThreadPoolExecutor(max_workers=len(paths)) as pool,
    ):
        for name, result in pool.map(fetch, paths.items()):
            if isinstance(result, LiveAdmitError):
                failures[name] = result
            else:
                payloads[name] = result
        if gated and cached_catalog is None:
            # Phase 2: per-provider catalog reads for the gated providers only.
            # A failed providers/free-tier read leaves the gate unknown, so the
            # catalog falls back to its last-good copy with them.
            if "free_tier" in payloads and "providers" in payloads:
                wanted = _gated_providers(
                    payloads["free_tier"], payloads["providers"], payloads.get("account_status")
                )
                model_paths = {
                    f"model:{p}": PROVIDER_MODELS_PATH.format(provider=quote(p, safe=""))
                    for p in wanted
                }
                by_provider: dict[str, object] = {}
                catalog_error: LiveAdmitError | None = None
                if model_paths:
                    with ThreadPoolExecutor(max_workers=min(8, len(model_paths))) as models_pool:
                        for name, result in models_pool.map(fetch, model_paths.items()):
                            if isinstance(result, LiveAdmitError):
                                catalog_error = catalog_error or result
                            else:
                                by_provider[name.removeprefix("model:")] = result
                if catalog_error is None:
                    merged = _merge_provider_catalogs(by_provider)
                    merged_rows = merged.get("data")
                    if isinstance(merged_rows, list) and len(merged_rows) > raw_row_limit:
                        failures["catalog"] = LiveAdmitError(
                            "incomplete_source",
                            f"gated catalog exceeded the raw row limit {raw_row_limit}",
                        )
                    else:
                        payloads["catalog"] = merged
                        paths["catalog"] = PROVIDER_MODELS_PATH
                else:
                    failures["catalog"] = catalog_error
            else:
                failures["catalog"] = LiveAdmitError(
                    "transport", "gated catalog skipped: provider gate unavailable"
                )

    for error in failures.values():
        if error.code not in _MASKABLE_ADMIT_ERRORS:
            raise error
    fetched = {name: payloads[name] for name in paths if name in payloads}
    if "catalog" in fetched:
        put_cached_catalog(
            origin, api_key, fetched["catalog"], now=now_utc, cache_path=catalog_path
        )
    small = {name: payload for name, payload in fetched.items() if name != "catalog"}
    if small:
        try:
            _write_last_good(store, origin, small, now_utc)
        except OSError as exc:
            log.warning("admit last-good snapshot not written: %s", type(exc).__name__)

    stale: list[str] = []
    if failures:
        last_good = _read_last_good(store, origin, now_utc, last_good_max_age_seconds)
        if "catalog" in failures:
            # The catalog cache is the catalog's last-good copy (bounded stale).
            stale_catalog = get_cached_catalog(
                origin,
                api_key,
                now=now_utc,
                cache_path=catalog_path,
                max_age_seconds=last_good_max_age_seconds,
            )
            if stale_catalog is not None:
                last_good["catalog"] = stale_catalog
        missing = sorted(name for name in failures if name not in last_good)
        if missing:
            first = failures[missing[0]]
            raise LiveAdmitError(
                first.code,
                f"OmniRoute admit surfaces failed ({', '.join(missing)}) and no last-good "
                f"snapshot younger than {int(last_good_max_age_seconds)}s exists: {first}",
            )
        for name in sorted(failures):
            payloads[name] = last_good[name]
            stale.append(name)
        log.warning("admit snapshot served stale surfaces from last-good: %s", ", ".join(stale))

    from verdict.omniroute_health import load_omniroute_health

    return replace(
        snapshot_from_payloads(
            catalog=payloads["catalog"],
            free_tier=payloads["free_tier"],
            providers=payloads["providers"],
            account_status=payloads["account_status"],
        ),
        passive_health=load_omniroute_health(
            origin,
            api_key=api_key,
            now=datetime.now(timezone.utc),
            timeout=timeout,
            transport=transport,
        ),
        stale_served=tuple(stale),
        fetched_at=now_utc,
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
    if not isinstance(content, str) or not content.strip():
        return "error", "invalid provider response: missing completion content"
    return "sent", content


def omniroute_endpoint_from_env(
    providers: Mapping[str, Any] | None = None,
) -> tuple[str, str | None] | None:
    """Return ``(base_url, api_key)`` for OmniRoute when configured.

    An explicit ``OMNIROUTE_BASE_URL`` or provider config wins. When neither is
    set, use the same local OmniRoute origin as the serve proxy. This does not
    probe the network; a dead default still fails open in the snapshot loader.
    """
    env_url = os.getenv("OMNIROUTE_BASE_URL")
    from verdict.credentials_store import resolve_api_key

    env_key = resolve_api_key()
    if env_url and env_url.strip():
        return env_url.strip(), env_key
    if providers:
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
    return "http://127.0.0.1:20128", env_key


def _is_combo_identity(identity_id: str) -> bool:
    return identity_id.split("/", 1)[0] in _COMBO_PREFIXES


def is_frontier_identity(identity_id: str, allowlist: tuple[str, ...] | None = None) -> bool:
    """Named frontier/high-cap identity. Never inferred from 'sounds expensive'."""
    if allowlist:
        return identity_id in allowlist
    return classify(identity_id) <= 1


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
    """Annotate task classification without economic filtering or reordering.

    The legacy signature is retained for callers; price and resource class do
    not decide which qualified identities may compete.
    """
    return replace(
        receipt,
        task_class=task_class,
        class_reasons=class_reasons,
        task_profile_digest=task_profile_digest,
        spend_policy=spend_policy,
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
