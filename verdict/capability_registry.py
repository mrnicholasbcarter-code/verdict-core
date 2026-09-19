"""Semantic capability registry + provider resolver contract (BOD-87).

Callers request brand-free semantic capabilities. The registry selects the
highest-authority healthy provider among native Verdict and optional enrichment
adapters (Serena/LSP, Context7, Codebase Memory, MemoryPlane). External
providers enrich; they are never hard dependencies.

Missing enrichments → native fallback + explicit degraded coverage.
Conflicting or stale evidence is surfaced on the resolve decision — never
silently accepted.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Final, Literal

from verdict.semantic_capabilities import SEMANTIC_CAPABILITIES, is_semantic_capability

REGISTRY_SCHEMA_VERSION: Final[str] = "capability-registry/v1"

ProviderHealth = Literal["healthy", "degraded", "unhealthy", "unavailable"]
CostLatencyClass = Literal["local", "cheap", "moderate", "expensive"]
ConflictKind = Literal["stale", "conflicting"]

_SELECTABLE_HEALTH: Final[frozenset[str]] = frozenset({"healthy"})
_DEFAULT_STALE_AFTER_SECONDS: Final[float] = 3_600.0

# Native baseline authority — always present; enrichments rank higher when healthy.
NATIVE_AUTHORITY_RANK: Final[int] = 10
SERENA_LSP_AUTHORITY_RANK: Final[int] = 80
CODEBASE_MEMORY_AUTHORITY_RANK: Final[int] = 70
CONTEXT7_AUTHORITY_RANK: Final[int] = 60
MEMORY_PLANE_AUTHORITY_RANK: Final[int] = 40
TREE_SITTER_AST_AUTHORITY_RANK: Final[int] = 15


@dataclass(frozen=True)
class ProviderDescriptor:
    """Registered provider: brand + coverage + selection metadata.

    ``provider_id`` / ``brand`` carry product identity. Capability membership
    uses brand-free semantic ids only.
    """

    provider_id: str
    brand: str
    capabilities: frozenset[str]
    authority_rank: int
    health: ProviderHealth = "healthy"
    freshness_seconds: float | None = None
    cost_latency_class: CostLatencyClass = "local"
    provenance: str = "declared"
    fallback_provider_ids: tuple[str, ...] = ()
    observed_at: str | None = None
    evidence_digest: str | None = None
    hard_dependency: bool = False
    notes: str = ""

    def supports(self, capability_id: str) -> bool:
        return capability_id in self.capabilities

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "brand": self.brand,
            "capabilities": sorted(self.capabilities),
            "authority_rank": self.authority_rank,
            "health": self.health,
            "freshness_seconds": self.freshness_seconds,
            "cost_latency_class": self.cost_latency_class,
            "provenance": self.provenance,
            "fallback_provider_ids": list(self.fallback_provider_ids),
            "observed_at": self.observed_at,
            "evidence_digest": self.evidence_digest,
            "hard_dependency": self.hard_dependency,
            "notes": self.notes,
        }


@dataclass(frozen=True)
class SkipReason:
    provider_id: str
    reason: str

    def to_dict(self) -> dict[str, str]:
        return {"provider_id": self.provider_id, "reason": self.reason}


@dataclass(frozen=True)
class EvidenceConflict:
    """Surfaced when healthy candidates disagree or one is stale."""

    capability_id: str
    provider_ids: tuple[str, ...]
    kind: ConflictKind
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "capability_id": self.capability_id,
            "provider_ids": list(self.provider_ids),
            "kind": self.kind,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class ResolveDecision:
    """Auditable outcome of capability → provider resolution."""

    capability_id: str
    selected: ProviderDescriptor | None
    skipped: tuple[SkipReason, ...] = ()
    fallback_chain: tuple[str, ...] = ()
    conflicts: tuple[EvidenceConflict, ...] = ()
    degraded: bool = False
    reason: str = ""
    schema_version: str = REGISTRY_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "capability_id": self.capability_id,
            "selected_provider_id": (
                self.selected.provider_id if self.selected is not None else None
            ),
            "skipped": [item.to_dict() for item in self.skipped],
            "fallback_chain": list(self.fallback_chain),
            "conflicts": [item.to_dict() for item in self.conflicts],
            "degraded": self.degraded,
            "reason": self.reason,
        }


@dataclass
class SemanticCapabilityRegistry:
    """Versioned registry: register providers, resolve semantic capabilities."""

    stale_after_seconds: float = _DEFAULT_STALE_AFTER_SECONDS
    _providers: dict[str, ProviderDescriptor] = field(default_factory=dict, init=False)
    schema_version: str = REGISTRY_SCHEMA_VERSION

    def register(self, descriptor: ProviderDescriptor) -> None:
        if descriptor.hard_dependency:
            raise ValueError(
                f"external providers must not be hard dependencies: {descriptor.provider_id}"
            )
        if not descriptor.provider_id.strip():
            raise ValueError("provider_id is required")
        for cap in descriptor.capabilities:
            if cap not in SEMANTIC_CAPABILITIES and "." not in cap:
                raise ValueError(f"invalid capability membership: {cap!r}")
        self._providers[descriptor.provider_id] = descriptor

    def unregister(self, provider_id: str) -> None:
        self._providers.pop(provider_id, None)

    def providers(self) -> tuple[ProviderDescriptor, ...]:
        return tuple(self._providers.values())

    def providers_for(self, capability_id: str) -> tuple[ProviderDescriptor, ...]:
        return tuple(p for p in self._providers.values() if p.supports(capability_id))

    def resolve(self, capability_id: str) -> ResolveDecision:
        if not is_semantic_capability(capability_id):
            return ResolveDecision(
                capability_id=capability_id,
                selected=None,
                degraded=True,
                reason="unknown_capability",
            )

        candidates = list(self.providers_for(capability_id))
        if not candidates:
            return ResolveDecision(
                capability_id=capability_id,
                selected=None,
                degraded=True,
                reason="no_provider_registered",
            )

        skipped: list[SkipReason] = []
        healthy: list[ProviderDescriptor] = []
        for provider in candidates:
            if provider.health not in _SELECTABLE_HEALTH:
                skipped.append(
                    SkipReason(provider_id=provider.provider_id, reason=f"health:{provider.health}")
                )
                continue
            healthy.append(provider)

        if not healthy:
            # Prefer native among non-selectable if somehow marked differently —
            # normally native is healthy; if all unhealthy, explicit gap.
            native = next((p for p in candidates if p.provider_id.startswith("native.")), None)
            if native is not None and native.health == "healthy":
                healthy = [native]
            else:
                return ResolveDecision(
                    capability_id=capability_id,
                    selected=None,
                    skipped=tuple(skipped),
                    degraded=True,
                    reason="no_healthy_provider",
                    fallback_chain=tuple(p.provider_id for p in candidates),
                )

        healthy.sort(
            key=lambda p: (
                p.authority_rank,
                _freshness_sort_key(p),
                -_cost_rank(p.cost_latency_class),
            ),
            reverse=True,
        )
        selected = healthy[0]
        conflicts = _detect_conflicts(
            capability_id, healthy, selected, stale_after_seconds=self.stale_after_seconds
        )

        fallback_chain = _fallback_chain(selected, self._providers, capability_id)
        non_native_candidates = [p for p in candidates if not p.provider_id.startswith("native.")]
        if selected.provider_id.startswith("native.") and non_native_candidates:
            degraded = True
            reason = "native_fallback"
        elif conflicts:
            reason = "highest_authority_healthy_with_conflicts"
            degraded = any(c.kind == "stale" for c in conflicts)
        else:
            degraded = False
            reason = "highest_authority_healthy"

        return ResolveDecision(
            capability_id=capability_id,
            selected=selected,
            skipped=tuple(skipped),
            fallback_chain=fallback_chain,
            conflicts=tuple(conflicts),
            degraded=degraded,
            reason=reason,
        )

    def health_report(self) -> Mapping[str, Any]:
        """Controller-friendly snapshot (BOD-65 may consume later)."""
        by_health: dict[str, list[str]] = {
            "healthy": [],
            "degraded": [],
            "unhealthy": [],
            "unavailable": [],
        }
        for provider in self._providers.values():
            by_health.setdefault(provider.health, []).append(provider.provider_id)
        return {
            "schema_version": self.schema_version,
            "provider_count": len(self._providers),
            "by_health": {k: sorted(v) for k, v in by_health.items()},
            "capabilities_covered": sorted(
                {cap for p in self._providers.values() for cap in p.capabilities}
            ),
        }


def _cost_rank(cls: CostLatencyClass) -> int:
    order: dict[CostLatencyClass, int] = {"local": 4, "cheap": 3, "moderate": 2, "expensive": 1}
    return order.get(cls, 0)


def _freshness_sort_key(provider: ProviderDescriptor) -> float:
    """Higher is better: unknown freshness sorts below known-fresh."""
    if provider.freshness_seconds is None:
        return -1.0
    # Invert: fresher (lower seconds) → higher sort key
    return max(0.0, 1_000_000.0 - provider.freshness_seconds)


def _parse_observed_at(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _detect_conflicts(
    capability_id: str,
    healthy: Sequence[ProviderDescriptor],
    selected: ProviderDescriptor,
    *,
    stale_after_seconds: float,
) -> list[EvidenceConflict]:
    conflicts: list[EvidenceConflict] = []
    now = datetime.now(timezone.utc)

    if selected.freshness_seconds is not None and selected.freshness_seconds > stale_after_seconds:
        conflicts.append(
            EvidenceConflict(
                capability_id=capability_id,
                provider_ids=(selected.provider_id,),
                kind="stale",
                detail=(
                    f"selected provider freshness_seconds="
                    f"{selected.freshness_seconds} exceeds stale_after={stale_after_seconds}"
                ),
            )
        )
    else:
        observed = _parse_observed_at(selected.observed_at)
        if observed is not None:
            age = (now - observed.astimezone(timezone.utc)).total_seconds()
            if age > stale_after_seconds:
                conflicts.append(
                    EvidenceConflict(
                        capability_id=capability_id,
                        provider_ids=(selected.provider_id,),
                        kind="stale",
                        detail=f"selected provider observed_at age={age:.0f}s exceeds stale_after={stale_after_seconds}",
                    )
                )

    digests = {p.provider_id: p.evidence_digest for p in healthy if p.evidence_digest is not None}
    unique_digests = {d for d in digests.values() if d}
    if len(unique_digests) > 1:
        conflicts.append(
            EvidenceConflict(
                capability_id=capability_id,
                provider_ids=tuple(sorted(digests)),
                kind="conflicting",
                detail=(
                    "healthy providers disagree on evidence_digest: "
                    + ", ".join(f"{pid}={digest}" for pid, digest in sorted(digests.items()))
                ),
            )
        )
    return conflicts


def _fallback_chain(
    selected: ProviderDescriptor, providers: Mapping[str, ProviderDescriptor], capability_id: str
) -> tuple[str, ...]:
    chain: list[str] = [selected.provider_id]
    for fid in selected.fallback_provider_ids:
        if fid in providers and providers[fid].supports(capability_id) and fid not in chain:
            chain.append(fid)
    # Always offer native if present and not already selected
    for pid, provider in providers.items():
        if pid.startswith("native.") and provider.supports(capability_id) and pid not in chain:
            chain.append(pid)
    return tuple(chain)


# ---------------------------------------------------------------------------
# Descriptor factories (adapter stubs — no product imports)
# ---------------------------------------------------------------------------


def _native_capabilities() -> frozenset[str]:
    # Native Core covers Wave-1 semantic set + docs.library / docs.lookup locally.
    return frozenset(
        {
            "code.symbols",
            "code.definitions",
            "code.references",
            "code.graph",
            "tests.impact",
            "git.diff",
            "repo.state",
            "docs.project",
            "docs.library",
            "docs.lookup",
            "memory.search",
            "task.requirements",
            "task.proof",
        }
    )


def make_native_verdict_descriptor(
    *,
    health: ProviderHealth = "healthy",
    authority_rank: int = NATIVE_AUTHORITY_RANK,
    capabilities: frozenset[str] | None = None,
    evidence_digest: str | None = None,
    observed_at: str | None = None,
    freshness_seconds: float | None = 0.0,
) -> ProviderDescriptor:
    return ProviderDescriptor(
        provider_id="native.verdict",
        brand="verdict",
        capabilities=capabilities if capabilities is not None else _native_capabilities(),
        authority_rank=authority_rank,
        health=health,
        freshness_seconds=freshness_seconds,
        cost_latency_class="local",
        provenance="native-baseline",
        fallback_provider_ids=(),
        observed_at=observed_at,
        evidence_digest=evidence_digest,
        hard_dependency=False,
        notes="Always-on Tree-sitter/AST/exact-text/git native Core (BOD-123).",
    )


def make_tree_sitter_ast_descriptor(
    *, health: ProviderHealth = "healthy", authority_rank: int = TREE_SITTER_AST_AUTHORITY_RANK
) -> ProviderDescriptor:
    """Explicit AST/tree-sitter native fallback lane (same brand family)."""
    return ProviderDescriptor(
        provider_id="native.verdict.ast",
        brand="verdict",
        capabilities=frozenset(
            {"code.symbols", "code.definitions", "code.references", "code.imports", "code.graph"}
        ),
        authority_rank=authority_rank,
        health=health,
        freshness_seconds=0.0,
        cost_latency_class="local",
        provenance="native-ast",
        fallback_provider_ids=("native.verdict",),
        hard_dependency=False,
        notes="Deterministic AST/exact-text slice; enrichment adapters may supersede.",
    )


def make_serena_lsp_stub(
    *,
    health: ProviderHealth = "unavailable",
    authority_rank: int = SERENA_LSP_AUTHORITY_RANK,
    capabilities: frozenset[str] | None = None,
    evidence_digest: str | None = None,
    observed_at: str | None = None,
    freshness_seconds: float | None = None,
) -> ProviderDescriptor:
    """Serena / LSP enrichment adapter stub — registerable without importing Serena."""
    return ProviderDescriptor(
        provider_id="adapter.serena_lsp",
        brand="serena",
        capabilities=capabilities
        if capabilities is not None
        else frozenset(
            {
                "code.symbols",
                "code.definitions",
                "code.references",
                "code.callers",
                "code.callees",
                "code.imports",
                "code.graph",
            }
        ),
        authority_rank=authority_rank,
        health=health,
        freshness_seconds=freshness_seconds,
        cost_latency_class="cheap",
        provenance="adapter-stub",
        fallback_provider_ids=("native.verdict", "native.verdict.ast"),
        observed_at=observed_at,
        evidence_digest=evidence_digest,
        hard_dependency=False,
        notes="Optional LSP/Serena enrichment; default unavailable until health probe passes.",
    )


def make_context7_stub(
    *,
    health: ProviderHealth = "unavailable",
    authority_rank: int = CONTEXT7_AUTHORITY_RANK,
    capabilities: frozenset[str] | None = None,
    evidence_digest: str | None = None,
    observed_at: str | None = None,
    freshness_seconds: float | None = None,
) -> ProviderDescriptor:
    """Context7 / docs-mcp enrichment stub — no Context7 import."""
    return ProviderDescriptor(
        provider_id="adapter.context7",
        brand="context7",
        capabilities=capabilities
        if capabilities is not None
        else frozenset({"docs.library", "docs.lookup"}),
        authority_rank=authority_rank,
        health=health,
        freshness_seconds=freshness_seconds,
        cost_latency_class="moderate",
        provenance="adapter-stub",
        fallback_provider_ids=("native.verdict",),
        observed_at=observed_at,
        evidence_digest=evidence_digest,
        hard_dependency=False,
        notes="Optional docs enrichment; native project docs remain the fallback.",
    )


def make_codebase_memory_stub(
    *,
    health: ProviderHealth = "unavailable",
    authority_rank: int = CODEBASE_MEMORY_AUTHORITY_RANK,
    capabilities: frozenset[str] | None = None,
    evidence_digest: str | None = None,
    observed_at: str | None = None,
    freshness_seconds: float | None = None,
) -> ProviderDescriptor:
    """Codebase Memory MCP enrichment stub — no product import."""
    return ProviderDescriptor(
        provider_id="adapter.codebase_memory",
        brand="codebase-memory",
        capabilities=capabilities
        if capabilities is not None
        else frozenset(
            {
                "code.symbols",
                "code.graph",
                "code.changed-neighborhood",
                "memory.search",
                "tests.impact",
            }
        ),
        authority_rank=authority_rank,
        health=health,
        freshness_seconds=freshness_seconds,
        cost_latency_class="cheap",
        provenance="adapter-stub",
        fallback_provider_ids=("native.verdict",),
        observed_at=observed_at,
        evidence_digest=evidence_digest,
        hard_dependency=False,
        notes="Optional codebase-memory MCP; native Core covers symbols/memory when absent.",
    )


def make_memory_plane_stub(
    *,
    health: ProviderHealth = "healthy",
    authority_rank: int = MEMORY_PLANE_AUTHORITY_RANK,
    capabilities: frozenset[str] | None = None,
) -> ProviderDescriptor:
    """Verdict MemoryPlane provider lane (in-process; not an external MCP)."""
    return ProviderDescriptor(
        provider_id="native.verdict.memory_plane",
        brand="verdict",
        capabilities=capabilities
        if capabilities is not None
        else frozenset({"memory.search", "task.requirements", "task.proof"}),
        authority_rank=authority_rank,
        health=health,
        freshness_seconds=0.0,
        cost_latency_class="local",
        provenance="memory-plane",
        fallback_provider_ids=("native.verdict",),
        hard_dependency=False,
        notes="In-process MemoryPlane; higher authority than generic native for memory.search.",
    )


def build_default_registry(
    *,
    stale_after_seconds: float = _DEFAULT_STALE_AFTER_SECONDS,
    enrichment_health: ProviderHealth = "unavailable",
) -> SemanticCapabilityRegistry:
    """Default registry: native always healthy; enrichment stubs default unavailable."""
    registry = SemanticCapabilityRegistry(stale_after_seconds=stale_after_seconds)
    registry.register(make_native_verdict_descriptor())
    registry.register(make_tree_sitter_ast_descriptor())
    registry.register(make_memory_plane_stub())
    registry.register(make_serena_lsp_stub(health=enrichment_health))
    registry.register(make_context7_stub(health=enrichment_health))
    registry.register(make_codebase_memory_stub(health=enrichment_health))
    return registry


def resolve_capability(
    capability_id: str, *, registry: SemanticCapabilityRegistry | None = None
) -> ResolveDecision:
    """Module-level convenience for callers / BOD-120 planner later."""
    active = registry if registry is not None else build_default_registry()
    return active.resolve(capability_id)


def describe_registry(registry: SemanticCapabilityRegistry | None = None) -> dict[str, Any]:
    active = registry if registry is not None else build_default_registry()
    return {
        "schema_version": active.schema_version,
        "vocabulary_size": len(SEMANTIC_CAPABILITIES),
        "providers": [p.to_dict() for p in active.providers()],
        "health": dict(active.health_report()),
    }


__all__ = [
    "CODEBASE_MEMORY_AUTHORITY_RANK",
    "CONTEXT7_AUTHORITY_RANK",
    "MEMORY_PLANE_AUTHORITY_RANK",
    "NATIVE_AUTHORITY_RANK",
    "REGISTRY_SCHEMA_VERSION",
    "SERENA_LSP_AUTHORITY_RANK",
    "TREE_SITTER_AST_AUTHORITY_RANK",
    "ConflictKind",
    "CostLatencyClass",
    "EvidenceConflict",
    "ProviderDescriptor",
    "ProviderHealth",
    "ResolveDecision",
    "SemanticCapabilityRegistry",
    "SkipReason",
    "build_default_registry",
    "describe_registry",
    "make_codebase_memory_stub",
    "make_context7_stub",
    "make_memory_plane_stub",
    "make_native_verdict_descriptor",
    "make_serena_lsp_stub",
    "make_tree_sitter_ast_descriptor",
    "resolve_capability",
]
