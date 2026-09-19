"""Context intelligence: deterministic slices, retrieval, and model-aware packs.

Core owns policy. Retrieval adapters supply units only. Compiling a pack never
dumps a repository or a chat transcript.

BOD-123 Wave-1 adds capability-oriented ContextQueryPlan / native resolver
contracts. External MCP/Serena providers are adapters later — native Verdict
baseline is always available.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from time import time as unix_time
from typing import Any, Literal

from verdict.context_pack import (
    ContextPack,
    ContextPackCompiler,
    ContextPlan,
    ContextReceipt,
    ContextUnit,
    estimate_tokens,
)
from verdict.context_sources import PROVIDER_ID, CapabilityProvider, build_native_providers
from verdict.memory_plane import MemoryPlane, MemorySearchResult

SCHEMA_VERSION = "context-intelligence/v1"
FABRIC_SCHEMA_VERSION = "context-intelligence-fabric/v1"
SliceCategory = Literal["docs", "code", "memory"]
DEFAULT_MAX_UNITS = 8
DEFAULT_MAX_FILE_BYTES = 8_192
DEFAULT_MAX_EXPANSION_DEPTH = 2

# Brand-free semantic capabilities. Native baseline covers Wave-1 start set;
# code.graph / tests.impact are optional expansions when budget allows.
WAVE1_NATIVE_CAPABILITIES: frozenset[str] = frozenset(
    {
        "code.symbols",
        "code.definitions",
        "code.references",
        "code.graph",
        "tests.impact",
        "git.diff",
        "repo.state",
        "docs.project",
        "memory.search",
        "task.requirements",
        "task.proof",
    }
)
WAVE1_BASELINE_ORDER: tuple[str, ...] = (
    "task.requirements",
    "task.proof",
    "code.symbols",
    "code.definitions",
    "code.references",
    "docs.project",
    "memory.search",
    "git.diff",
    "repo.state",
)
_SKIP_DIR_PARTS = {".git", ".venv", "venv", "__pycache__", "node_modules", ".tox"}
_SECRET = re.compile(
    r"(?i)(?:api[_-]?key\s*[:=]|password\s*[:=]|bearer\s+[a-z0-9._~+/=-]{16,}|sk-[a-z0-9]{20,})"
)
_STOP = frozenset(
    {
        "this",
        "that",
        "with",
        "from",
        "your",
        "project",
        "stored",
        "store",
        "return",
        "json",
        "only",
        "reply",
        "using",
        "exact",
        "planted",
        "unique",
        "docs",
        "code",
        "memory",
        "the",
        "and",
        "for",
        "into",
    }
)


class ContextIntelligenceError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class RetrievalSlice:
    slice_id: str
    category: SliceCategory
    query: str
    root: str | None = None
    max_units: int = DEFAULT_MAX_UNITS


@dataclass(frozen=True)
class Omission:
    category: str
    reason: str
    ref: str | None = None


@dataclass(frozen=True)
class WorkingState:
    goal: str
    slices: tuple[str, ...]
    pack_digest: str | None = None
    required_fact_kept: bool = False
    omissions: tuple[Omission, ...] = ()

    def to_slots(self) -> dict[str, str]:
        return {
            "goal": self.goal,
            "slices": ",".join(self.slices),
            "pack_digest": self.pack_digest or "",
            "required_fact_kept": "true" if self.required_fact_kept else "false",
            "omissions": ",".join(f"{item.category}:{item.reason}" for item in self.omissions),
        }


@dataclass(frozen=True)
class RetrievalResult:
    units: tuple[ContextUnit, ...]
    omissions: tuple[Omission, ...]
    working_state: WorkingState
    file_count: int = 0


@dataclass(frozen=True)
class CoverageGap:
    """Named coverage hole — missing provider or empty native result."""

    capability_id: str
    reason: str
    provider_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "capability_id": self.capability_id,
            "reason": self.reason,
            "provider_id": self.provider_id,
        }


@dataclass(frozen=True)
class CapabilityCoverage:
    """Pack-receipt fields: requested / available / used / omitted."""

    requested: tuple[str, ...]
    available: tuple[str, ...]
    used: tuple[str, ...]
    omitted: tuple[CoverageGap, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "requested": list(self.requested),
            "available": list(self.available),
            "used": list(self.used),
            "omitted": [gap.to_dict() for gap in self.omitted],
        }


@dataclass(frozen=True)
class CapabilityRequest:
    """Planner asks for a semantic capability — never a brand-name tool."""

    capability_id: str
    query: str
    max_units: int = DEFAULT_MAX_UNITS
    max_depth: int = 1
    hints: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "hints", dict(self.hints))


@dataclass(frozen=True)
class ContextQueryPlan:
    """Bounded retrieval plan derived from task / AC / proof / errors / budget."""

    plan_id: str
    task: str
    requests: tuple[CapabilityRequest, ...]
    token_budget: int
    max_units: int = DEFAULT_MAX_UNITS
    max_expansion_depth: int = DEFAULT_MAX_EXPANSION_DEPTH
    acceptance_criteria: tuple[str, ...] = ()
    proof_criteria: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    target_symbols: tuple[str, ...] = ()
    schema_version: str = FABRIC_SCHEMA_VERSION


@dataclass(frozen=True)
class FabricRetrieval:
    """Capability-oriented retrieval result with explicit coverage."""

    units: tuple[ContextUnit, ...]
    coverage: CapabilityCoverage
    plan: ContextQueryPlan
    stop_reason: str
    file_count: int = 0
    omissions: tuple[Omission, ...] = ()

    def attach_coverage(self, receipt: ContextReceipt) -> ContextReceipt:
        """Return a receipt copy carrying capability coverage (additive)."""
        return replace(receipt, capability_coverage=self.coverage.to_dict())


class NativeCapabilityResolver:
    """Resolve semantic capabilities to the always-on native Verdict baseline.

    External adapters register later via ``verdict.capability_registry``
    (BOD-87); absence of Serena/MCP must not remove native coverage for Wave-1
    capabilities. Multi-provider authority ranking lives on
    ``SemanticCapabilityRegistry`` — this class remains the fabric execution
    bridge to in-process ``CapabilityProvider`` callables.
    """

    def __init__(self, providers: Sequence[CapabilityProvider] | None = None) -> None:
        self._providers: tuple[CapabilityProvider, ...] = tuple(
            providers if providers is not None else build_native_providers()
        )
        index: dict[str, CapabilityProvider] = {}
        for provider in self._providers:
            for capability_id in provider.capabilities:
                index.setdefault(capability_id, provider)
        self._by_capability = index

    @property
    def provider_id(self) -> str:
        return PROVIDER_ID

    def available_capabilities(self) -> frozenset[str]:
        return frozenset(self._by_capability)

    def resolve(self, capability_id: str) -> CapabilityProvider | None:
        return self._by_capability.get(capability_id)

    def ranked_provider_id(self, capability_id: str) -> str | None:
        """Consult BOD-87 registry for highest-authority healthy provider id.

        Fabric execution still uses ``resolve()`` → native callables; this
        surfaces which enrichment brand would win when adapters are healthy.
        """
        from verdict.capability_registry import build_default_registry

        decision = build_default_registry().resolve(capability_id)
        if decision.selected is None:
            return None
        return decision.selected.provider_id


def plan_context_query(
    task: str,
    *,
    acceptance_criteria: Sequence[str] = (),
    proof_criteria: Sequence[str] = (),
    errors: Sequence[str] = (),
    token_budget: int = 4096,
    target_symbols: Sequence[str] = (),
    max_units: int = DEFAULT_MAX_UNITS,
    max_expansion_depth: int = DEFAULT_MAX_EXPANSION_DEPTH,
    extra_capabilities: Sequence[str] = (),
    include_optional_graph: bool = False,
) -> ContextQueryPlan:
    """Build a capability-oriented plan. Never requests Serena/MCP by name."""
    if token_budget < 1:
        raise ContextIntelligenceError("invalid_budget", "token_budget must be positive")
    if max_units < 1 or max_units > 32:
        raise ContextIntelligenceError("invalid_budget", "max_units must be in 1..32")
    query = task.strip() or "context retrieval"
    symbols = tuple(item for item in target_symbols if str(item).strip())
    if not symbols:
        # Seed from task / errors when caller did not name symbols.
        blob = " ".join([query, *errors])
        symbols = tuple(
            name
            for name in re.findall(r"\b([A-Za-z_][A-Za-z0-9_]{3,})\b", blob)
            if name.lower()
            not in {
                "rename",
                "update",
                "fix",
                "preserve",
                "honor",
                "pytest",
                "green",
                "error",
                "nameerror",
            }
        )[:4]

    ordered = list(WAVE1_BASELINE_ORDER)
    if include_optional_graph:
        ordered.extend(["code.graph", "tests.impact"])
    extra = tuple(str(item).strip() for item in extra_capabilities if str(item).strip())
    per_cap = max(1, min(2, max(1, max_units // max(len(ordered) + len(extra), 1))))
    hints_base: dict[str, Any] = {
        "task": query,
        "acceptance_criteria": tuple(acceptance_criteria),
        "proof_criteria": tuple(proof_criteria),
        "errors": tuple(errors),
        "symbols": symbols,
        "target_symbols": symbols,
    }
    requests: list[CapabilityRequest] = []
    for capability_id in ordered:
        requests.append(
            CapabilityRequest(
                capability_id=capability_id,
                query=query,
                max_units=per_cap,
                max_depth=max_expansion_depth,
                hints=hints_base,
            )
        )
    for cap in extra:
        requests.append(
            CapabilityRequest(
                capability_id=cap, query=query, max_units=per_cap, max_depth=1, hints=hints_base
            )
        )
    plan_id = f"cq:{_digest_text(query + str(token_budget) + ','.join(ordered))[:16]}"
    return ContextQueryPlan(
        plan_id=plan_id,
        task=query,
        requests=tuple(requests),
        token_budget=token_budget,
        max_units=max_units,
        max_expansion_depth=max_expansion_depth,
        acceptance_criteria=tuple(acceptance_criteria),
        proof_criteria=tuple(proof_criteria),
        errors=tuple(errors),
        target_symbols=symbols,
    )


def execute_context_query(
    plan: ContextQueryPlan,
    *,
    repo_root: Path,
    plane: MemoryPlane | None = None,
    resolver: NativeCapabilityResolver | None = None,
) -> FabricRetrieval:
    """Execute a capability plan with bounded expansion; never dump the repo."""
    root = repo_root.resolve()
    if plan.max_units > 32:
        raise ContextIntelligenceError("repo_dump_refused", "context query plan exceeds unit bound")
    active = resolver or NativeCapabilityResolver()
    available = tuple(sorted(active.available_capabilities()))
    requested = tuple(req.capability_id for req in plan.requests)
    units: list[ContextUnit] = []
    used: list[str] = []
    omitted: list[CoverageGap] = []
    omissions: list[Omission] = []
    seen_keys: set[str] = set()
    token_used = 0
    stop_reason = "satisfied"
    depth_hits = 0

    for req in plan.requests:
        provider = active.resolve(req.capability_id)
        if provider is None:
            omitted.append(CoverageGap(req.capability_id, "provider_unavailable", provider_id=None))
            omissions.append(Omission(req.capability_id, "provider_unavailable", None))
            continue

        if len(units) >= plan.max_units:
            stop_reason = "max_units"
            omitted.append(CoverageGap(req.capability_id, "max_units", provider.provider_id))
            continue
        if token_used >= plan.token_budget:
            stop_reason = "budget"
            omitted.append(CoverageGap(req.capability_id, "budget", provider.provider_id))
            continue
        if depth_hits > plan.max_expansion_depth * len(WAVE1_BASELINE_ORDER):
            stop_reason = "depth_limit"
            omitted.append(CoverageGap(req.capability_id, "depth_limit", provider.provider_id))
            continue

        remaining = plan.max_units - len(units)
        result = provider.provide(
            capability_id=req.capability_id,
            query=req.query,
            repo_root=root,
            max_units=min(req.max_units, remaining),
            hints=req.hints,
            plane=plane,
        )
        depth_hits += 1
        if result.gap_reason and not result.units:
            reason = result.gap_reason
            omitted.append(CoverageGap(req.capability_id, reason, provider.provider_id))
            omissions.append(Omission(req.capability_id, reason, None))
            continue

        added = False
        for unit in result.units:
            if unit.key in seen_keys:
                continue
            cost = estimate_tokens(unit.content)
            if token_used + cost > plan.token_budget and units:
                stop_reason = "budget"
                break
            seen_keys.add(unit.key)
            units.append(unit)
            token_used += cost
            added = True
            if len(units) >= plan.max_units:
                stop_reason = "max_units"
                break
        if added:
            used.append(req.capability_id)
        elif result.gap_reason:
            omitted.append(CoverageGap(req.capability_id, result.gap_reason, provider.provider_id))

    coverage = CapabilityCoverage(
        requested=requested,
        available=available,
        used=tuple(dict.fromkeys(used)),
        omitted=tuple(omitted),
    )
    return FabricRetrieval(
        units=tuple(units),
        coverage=coverage,
        plan=plan,
        stop_reason=stop_reason,
        file_count=_count_files(root),
        omissions=tuple(omissions),
    )


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _digest_text(text: str) -> str:
    return f"sha256:{hashlib.sha256(text.encode()).hexdigest()}"


def _query_terms(query: str) -> tuple[str, ...]:
    terms = [part.lower() for part in re.findall(r"[A-Za-z0-9_-]{4,}", query)]
    return tuple(term for term in terms if term not in _STOP)


def _looks_secret(text: str) -> bool:
    return _SECRET.search(text) is not None


def _skip_path(path: Path) -> bool:
    return any(part in _SKIP_DIR_PARTS for part in path.parts)


def plan_slices(task: str, *, proof_root: Path) -> tuple[RetrievalSlice, ...]:
    root = proof_root.resolve()
    query = task.strip() or "lift token"
    return (
        RetrievalSlice("docs-adr", "docs", query, root=str(root / "docs" / "adr")),
        RetrievalSlice("code-markers", "code", query, root=str(root)),
        RetrievalSlice("memory-search", "memory", query),
    )


def retrieve_units(
    slices: Sequence[RetrievalSlice],
    *,
    proof_root: Path,
    plane: MemoryPlane | None = None,
    required_fact: str | None = None,
    task: str = "",
) -> RetrievalResult:
    root = proof_root.resolve()
    units: list[ContextUnit] = []
    omissions: list[Omission] = []
    file_count = _count_files(root)
    for item in slices:
        if item.max_units > 32 or _is_dump_slice(item, root):
            raise ContextIntelligenceError(
                "repo_dump_refused", f"slice {item.slice_id} would dump the repository"
            )
        if item.category == "docs":
            found = _retrieve_docs(item, root, required_fact=required_fact)
        elif item.category == "code":
            found = _retrieve_code(item, root, required_fact=required_fact)
        elif item.category == "memory":
            found = _retrieve_memory(item, plane, required_fact=required_fact)
        else:
            found = []
        if not found:
            reason = "not_found"
            missing_docs = item.category == "docs" and not (root / "docs" / "adr").is_dir()
            if (item.category == "memory" and plane is None) or missing_docs:
                reason = "no_default_location"
            omissions.append(Omission(item.category, reason, item.root))
        units.extend(found)
    if required_fact and not any(required_fact in unit.content for unit in units):
        omissions.append(Omission("required_fact", "not_found", None))
    state = WorkingState(
        goal=task or "retrieve",
        slices=tuple(item.slice_id for item in slices),
        omissions=tuple(omissions),
    )
    return RetrievalResult(tuple(units), tuple(omissions), state, file_count=file_count)


def compile_pack(
    units: Sequence[ContextUnit],
    *,
    token_budget: int,
    required_fact: str,
    candidate_id: str,
    compaction: bool = False,
) -> tuple[ContextPack, WorkingState]:
    if not required_fact.strip():
        raise ContextIntelligenceError("required_fact_missing", "required fact is empty")
    now = unix_time()
    prepared = []
    for unit in units:
        if required_fact in unit.content:
            prepared.append(replace(unit, confidence=1.0, created_at=now + 1_000_000))
        else:
            prepared.append(replace(unit, confidence=min(unit.confidence, 0.2), created_at=now))
    if compaction:
        prepared = _compact_optional(
            prepared, required_fact=required_fact, token_budget=token_budget
        )
    if not any(required_fact in unit.content for unit in prepared):
        raise ContextIntelligenceError(
            "required_fact_missing", "required fact is not in retrieved units"
        )
    required_cost = sum(
        estimate_tokens(unit.content) for unit in prepared if required_fact in unit.content
    )
    if required_cost >= token_budget:
        raise ContextIntelligenceError(
            "required_fact_omitted", "token budget cannot hold the required fact"
        )
    compiler = ContextPackCompiler(default_token_budget=token_budget)
    plan = ContextPlan(
        plan_id=f"lift:{candidate_id}",
        candidate_id=candidate_id,
        token_budget=token_budget,
        output_token_reserve=0,
        tool_token_reserve=0,
    )
    pack = compiler.compile_units(tuple(prepared), plan)
    if required_fact not in pack.compiled_prompt:
        raise ContextIntelligenceError(
            "required_fact_omitted", "compiled pack omitted the required fact"
        )
    state = WorkingState(
        goal="compile",
        slices=(),
        pack_digest=pack.digest,
        required_fact_kept=True,
        omissions=tuple(
            Omission("pack", decision.reason, decision.unit_id)
            for decision in pack.decisions
            if decision.action == "exclude"
        ),
    )
    return pack, state


def _is_dump_slice(item: RetrievalSlice, proof_root: Path) -> bool:
    if not item.root:
        return False
    resolved = Path(item.root).resolve()
    if resolved in {Path("/"), Path.home().resolve()}:
        return True
    return item.category in {"docs", "code"} and resolved == proof_root and not item.query.strip()


def _count_files(root: Path) -> int:
    if not root.is_dir():
        return 0
    return sum(1 for path in root.rglob("*") if path.is_file() and not _skip_path(path))


def _matches(text: str, query: str, required_fact: str | None) -> bool:
    if required_fact and required_fact in text:
        return True
    lowered = text.lower()
    terms = _query_terms(query)
    return any(term in lowered for term in terms)


def _unit(
    *, slot_type: str, key: str, content: str, source_uri: str, confidence: float = 1.0
) -> ContextUnit | None:
    if _looks_secret(content):
        return None
    return ContextUnit(
        unit_id=_digest_text(source_uri + key)[:24],
        slot_type=slot_type,  # type: ignore[arg-type]
        key=key,
        content=content,
        source_uri=source_uri,
        source_digest=_digest_text(content),
        observed_at=_now_iso(),
        trust="local-observation",
        confidence=confidence,
    )


def _retrieve_docs(
    item: RetrievalSlice, proof_root: Path, *, required_fact: str | None
) -> list[ContextUnit]:
    adr = Path(item.root).resolve() if item.root else proof_root / "docs" / "adr"
    if not adr.is_dir():
        adr = proof_root / "docs" / "adr"
    if not adr.is_dir():
        return []
    found: list[ContextUnit] = []
    for path in sorted(adr.rglob("*.md")):
        if _skip_path(path) or not path.is_file():
            continue
        payload = path.read_text(encoding="utf-8", errors="replace")[:DEFAULT_MAX_FILE_BYTES]
        if not _matches(payload, item.query, required_fact):
            continue
        unit = _unit(
            slot_type="evidence",
            key=f"docs:{path.name}",
            content=payload,
            source_uri=str(path.relative_to(proof_root) if proof_root in path.parents else path),
        )
        if unit is not None:
            found.append(unit)
        if len(found) >= item.max_units:
            break
    return found


def _retrieve_code(
    item: RetrievalSlice, proof_root: Path, *, required_fact: str | None
) -> list[ContextUnit]:
    root = Path(item.root).resolve() if item.root else proof_root
    if not root.is_dir():
        return []
    found: list[ContextUnit] = []
    for path in sorted(root.rglob("*.py")):
        if _skip_path(path) or not path.is_file():
            continue
        payload = path.read_text(encoding="utf-8", errors="replace")[:DEFAULT_MAX_FILE_BYTES]
        if not _matches(payload, item.query, required_fact):
            continue
        rel = str(
            path.relative_to(proof_root)
            if proof_root in path.parents or path == proof_root
            else path
        )
        unit = _unit(slot_type="evidence", key=f"code:{path.name}", content=payload, source_uri=rel)
        if unit is not None:
            found.append(unit)
        if len(found) >= item.max_units:
            break
    return found


def _retrieve_memory(
    item: RetrievalSlice, plane: MemoryPlane | None, *, required_fact: str | None
) -> list[ContextUnit]:
    if plane is None:
        return []
    ranked: list[MemorySearchResult] = plane.search_ranked(item.query, limit=item.max_units)
    found: list[ContextUnit] = []
    for result in ranked:
        content = result.record.content
        if not _matches(content, item.query, required_fact):
            continue
        if result.stale:
            continue
        if result.record.namespace == "memory_gate_events" or result.record.key.startswith(
            "gate_event"
        ):
            continue
        unit = _unit(
            slot_type="memory",
            key=f"memory:{result.record.key}",
            content=content,
            source_uri=f"memory:{result.record.record_id}",
            confidence=max(0.1, min(1.0, 1.0 / (result.rank or 1))),
        )
        if unit is not None:
            found.append(unit)
    if required_fact and not any(required_fact in unit.content for unit in found):
        for record in plane.records():
            if required_fact in record.content:
                unit = _unit(
                    slot_type="memory",
                    key=f"memory:{record.key}",
                    content=record.content,
                    source_uri=f"memory:{record.record_id}",
                )
                if unit is not None:
                    found.append(unit)
                break
    return found[: item.max_units]


def _compact_optional(
    units: Sequence[ContextUnit], *, required_fact: str, token_budget: int
) -> list[ContextUnit]:
    kept: list[ContextUnit] = []
    used = 0
    for unit in units:
        required = required_fact in unit.content or unit.slot_type in {"instructions", "policy"}
        content = unit.content
        if not required and estimate_tokens(content) > 80:
            content = content[:160] + "\n[summarized]"
            unit = ContextUnit(
                unit_id=unit.unit_id,
                slot_type=unit.slot_type,
                key=unit.key,
                content=content,
                source_uri=unit.source_uri,
                source_digest=_digest_text(content),
                observed_at=unit.observed_at,
                trust=unit.trust,
                confidence=unit.confidence,
            )
        cost = estimate_tokens(unit.content)
        if not required and used + cost > token_budget:
            continue
        kept.append(unit)
        used += cost
    return kept


__all__ = [
    "FABRIC_SCHEMA_VERSION",
    "SCHEMA_VERSION",
    "WAVE1_BASELINE_ORDER",
    "WAVE1_NATIVE_CAPABILITIES",
    "CapabilityCoverage",
    "CapabilityRequest",
    "ContextIntelligenceError",
    "ContextQueryPlan",
    "CoverageGap",
    "FabricRetrieval",
    "NativeCapabilityResolver",
    "Omission",
    "RetrievalResult",
    "RetrievalSlice",
    "WorkingState",
    "compile_pack",
    "execute_context_query",
    "plan_context_query",
    "plan_slices",
    "retrieve_units",
]
