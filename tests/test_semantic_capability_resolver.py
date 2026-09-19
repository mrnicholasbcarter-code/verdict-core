"""BOD-87: semantic capability vocabulary + provider resolver contract proofs."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from verdict.capability_registry import (
    REGISTRY_SCHEMA_VERSION,
    EvidenceConflict,
    ProviderDescriptor,
    SemanticCapabilityRegistry,
    build_default_registry,
    make_codebase_memory_stub,
    make_context7_stub,
    make_memory_plane_stub,
    make_native_verdict_descriptor,
    make_serena_lsp_stub,
)
from verdict.semantic_capabilities import (
    SEMANTIC_CAPABILITIES,
    SEMANTIC_CAPABILITY_SCHEMA_VERSION,
    assert_brand_free_capability_id,
    is_semantic_capability,
)

_VALID_HEALTH = frozenset({"healthy", "degraded", "unhealthy", "unavailable"})


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def test_schema_versions_are_frozen() -> None:
    assert SEMANTIC_CAPABILITY_SCHEMA_VERSION.startswith("semantic-capabilities/")
    assert REGISTRY_SCHEMA_VERSION.startswith("capability-registry/")


def test_vocabulary_covers_wave2_context_capabilities() -> None:
    required = {
        "code.symbols",
        "code.references",
        "code.graph",
        "docs.project",
        "docs.library",
        "memory.search",
        "git.diff",
        "tests.impact",
        "task.requirements",
        "task.proof",
    }
    assert required <= SEMANTIC_CAPABILITIES
    for cap in SEMANTIC_CAPABILITIES:
        assert is_semantic_capability(cap)
        assert_brand_free_capability_id(cap)


def test_provider_brand_is_not_embedded_in_semantic_capability_identity() -> None:
    """Proof 4: capability ids are brand-free; brands live on providers only."""
    brands = (
        "serena",
        "context7",
        "codebase-memory",
        "mcp",
        "lsp",
        "omniroute",
        "github",
        "linear",
    )
    for cap in SEMANTIC_CAPABILITIES:
        lowered = cap.lower()
        for brand in brands:
            assert brand not in lowered, f"capability {cap!r} embeds brand {brand!r}"
        assert not lowered.startswith("native.")
        assert "." in cap  # domain.capability shape

    native = make_native_verdict_descriptor()
    serena = make_serena_lsp_stub(health="healthy")
    assert native.brand == "verdict"
    assert "serena" in serena.brand.lower() or "lsp" in serena.provider_id.lower()
    # Brand lives on descriptor — not on capability membership
    for cap in serena.capabilities:
        assert "serena" not in cap.lower()
        assert "lsp" not in cap.lower()


def test_highest_authority_healthy_provider_wins() -> None:
    """Proof 1: among healthy providers, highest authority_rank is selected."""
    registry = SemanticCapabilityRegistry()
    native = make_native_verdict_descriptor(authority_rank=10)
    serena = make_serena_lsp_stub(
        health="healthy",
        authority_rank=80,
        capabilities=frozenset({"code.symbols", "code.references"}),
    )
    registry.register(native)
    registry.register(serena)

    decision = registry.resolve("code.symbols")
    assert decision.selected is not None
    assert decision.selected.provider_id == serena.provider_id
    assert decision.degraded is False
    assert decision.reason == "highest_authority_healthy"


def test_unhealthy_unavailable_provider_is_skipped() -> None:
    """Proof 2: unhealthy/unavailable providers are skipped with a named reason."""
    registry = SemanticCapabilityRegistry()
    native = make_native_verdict_descriptor(authority_rank=10)
    sick = make_serena_lsp_stub(
        health="unhealthy", authority_rank=90, capabilities=frozenset({"code.symbols"})
    )
    down = make_codebase_memory_stub(
        health="unavailable",
        authority_rank=70,
        capabilities=frozenset({"code.symbols", "memory.search"}),
    )
    registry.register(native)
    registry.register(sick)
    registry.register(down)

    decision = registry.resolve("code.symbols")
    assert decision.selected is not None
    assert decision.selected.provider_id == native.provider_id
    skip_ids = {item.provider_id: item.reason for item in decision.skipped}
    assert sick.provider_id in skip_ids
    assert "unhealthy" in skip_ids[sick.provider_id]
    assert down.provider_id in skip_ids
    assert "unavailable" in skip_ids[down.provider_id]


def test_native_fallback_when_enrichments_disappear() -> None:
    """Proof 3: missing enrichments → native fallback + explicit degraded coverage."""
    registry = SemanticCapabilityRegistry()
    native = make_native_verdict_descriptor(authority_rank=10)
    # Enrichment registered but unavailable — simulates MCP gone on fresh VPS
    enrichment = make_context7_stub(
        health="unavailable",
        authority_rank=60,
        capabilities=frozenset({"docs.library", "docs.lookup"}),
    )
    registry.register(native)
    registry.register(enrichment)

    decision = registry.resolve("docs.library")
    assert decision.selected is not None
    assert decision.selected.provider_id == native.provider_id
    assert decision.degraded is True
    assert "fallback" in decision.reason or "native" in decision.reason
    assert enrichment.provider_id in {s.provider_id for s in decision.skipped}
    assert native.provider_id in decision.fallback_chain or decision.selected.authority_rank == 10


def test_conflicting_stale_evidence_is_surfaced() -> None:
    """Proof 5: conflicting/stale evidence is surfaced, not silently accepted."""
    now = datetime.now(timezone.utc)
    registry = SemanticCapabilityRegistry(stale_after_seconds=300.0)
    native = make_native_verdict_descriptor(
        authority_rank=10,
        evidence_digest="sha256:native-aaa",
        observed_at=_iso(now),
        freshness_seconds=5.0,
    )
    stale_rich = make_serena_lsp_stub(
        health="healthy",
        authority_rank=80,
        capabilities=frozenset({"code.symbols"}),
        evidence_digest="sha256:serena-bbb",
        observed_at=_iso(now - timedelta(hours=2)),
        freshness_seconds=7200.0,
    )
    registry.register(native)
    registry.register(stale_rich)

    decision = registry.resolve("code.symbols")
    assert decision.selected is not None
    # Highest healthy authority still wins, but conflict/stale is explicit
    assert decision.selected.provider_id == stale_rich.provider_id
    assert decision.conflicts, "stale/conflicting evidence must be surfaced"
    kinds = {c.kind for c in decision.conflicts}
    assert kinds & {"stale", "conflicting"}
    for conflict in decision.conflicts:
        assert isinstance(conflict, EvidenceConflict)
        assert conflict.capability_id == "code.symbols"
        assert stale_rich.provider_id in conflict.provider_ids


def test_default_registry_includes_adapter_stubs_without_hard_deps() -> None:
    registry = build_default_registry()
    ids = {d.provider_id for d in registry.providers()}
    assert any(pid.startswith("native.") for pid in ids)
    assert any("serena" in pid or "lsp" in pid for pid in ids)
    assert any("context7" in pid for pid in ids)
    assert any("codebase" in pid or "memory" in pid for pid in ids)
    assert any(
        "memory.plane" in pid or "memory_plane" in pid or "verdict.memory" in pid for pid in ids
    )

    # Stubs are registerable descriptors — not product imports
    for factory in (
        make_serena_lsp_stub,
        make_context7_stub,
        make_codebase_memory_stub,
        make_memory_plane_stub,
    ):
        desc = factory(health="unavailable")
        assert isinstance(desc, ProviderDescriptor)
        assert desc.health in _VALID_HEALTH


def test_degraded_health_is_skipped_when_healthy_peer_exists() -> None:
    registry = SemanticCapabilityRegistry()
    native = make_native_verdict_descriptor(authority_rank=10, health="healthy")
    degraded = make_serena_lsp_stub(
        health="degraded", authority_rank=90, capabilities=frozenset({"code.references"})
    )
    registry.register(native)
    registry.register(degraded)
    decision = registry.resolve("code.references")
    assert decision.selected is not None
    assert decision.selected.provider_id == native.provider_id
    assert any(s.provider_id == degraded.provider_id for s in decision.skipped)


def test_unknown_capability_returns_explicit_gap() -> None:
    registry = build_default_registry()
    decision = registry.resolve("not.a.real.capability")
    assert decision.selected is None
    assert decision.degraded is True
    assert "unknown" in decision.reason or "unsupported" in decision.reason
