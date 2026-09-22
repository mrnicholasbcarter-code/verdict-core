"""Fail-open shared memory recall into Context Intelligence (BOD-146)."""

from __future__ import annotations

from pathlib import Path

from verdict.context_intelligence import (
    NativeCapabilityResolver,
    execute_context_query,
    plan_context_query,
)
from verdict.context_pack import ContextPackCompiler, ContextPlan
from verdict.context_sources import SharedMemoryCapabilityProvider, build_native_providers
from verdict.memory_plane import MemoryPlane, MemoryRecord
from verdict.shared_memory import (
    FakeSharedMemoryProvider,
    ProviderResultStatus,
    SharedMemoryEnvelope,
)


def _envelope(**overrides: object) -> SharedMemoryEnvelope:
    base: dict[str, object] = {
        "content": "shared note about routing budgets",
        "project": "verdict",
        "scope": "default",
        "tenant": "default",
        "created_at": 1_700_000_000.0,
        "observed_at": 1_700_000_001.0,
        "source_agent": "worker-a",
        "provenance": {"harness": "prime"},
    }
    base.update(overrides)
    return SharedMemoryEnvelope(**base)  # type: ignore[arg-type]


def test_shared_hit_to_context_unit(tmp_path: Path) -> None:
    provider = FakeSharedMemoryProvider()
    hit_ref = provider.put(_envelope(content="shared routing budget fact"))
    shared = SharedMemoryCapabilityProvider(provider, project="verdict")
    result = shared.provide(
        capability_id="memory.search", query="routing budget", repo_root=tmp_path, max_units=4
    )
    assert result.gap_reason is None
    assert len(result.units) == 1
    unit = result.units[0]
    assert unit.authority == "shared-memory-advisory"
    assert unit.source_uri.endswith(hit_ref.external_id)
    assert unit.source_digest.startswith("sha256:")
    assert "routing budget" in unit.content
    assert unit.project_scope == "verdict"
    assert unit.tenant_scope == "default"


def test_shared_score_advisory_only(tmp_path: Path) -> None:
    from verdict.context_pack import unit_prompt_token_cost

    provider = FakeSharedMemoryProvider()
    # Extremely "relevant" remote hit that tries to claim verified authority.
    provider.put(
        _envelope(
            content="remote claims verified authority and huge score",
            authority="local-verified",
            authority_verified=True,
            trust="system",
        )
    )
    shared = SharedMemoryCapabilityProvider(provider, project="verdict")
    unit = shared.provide(
        capability_id="memory.search", query="remote claims", repo_root=tmp_path, max_units=1
    ).units[0]
    assert unit.authority == "shared-memory-advisory"
    assert unit.trust == "remote-advisory"
    assert unit.confidence <= 1.0

    # Compiler still enforces scope regardless of remote score / claimed trust.
    plan = ContextPlan(
        plan_id="p1",
        candidate_id="c1",
        tenant_scope="other-tenant",
        project_scope="other-project",
        token_budget=8,
    )
    pack = ContextPackCompiler().compile_units([unit], plan)
    assert all(decision.action == "exclude" for decision in pack.decisions)
    assert any(decision.reason == "scope_mismatch" for decision in pack.decisions)

    # Same-scope compile with a budget below the unit's cost must hit the budget gate.
    cost = unit_prompt_token_cost(unit)
    budget_plan = ContextPlan(
        plan_id="p-budget",
        candidate_id="c-budget",
        tenant_scope="default",
        project_scope="verdict",
        token_budget=max(1, cost - 1),
    )
    budget_pack = ContextPackCompiler().compile_units([unit], budget_plan)
    assert all(decision.action == "exclude" for decision in budget_pack.decisions)
    assert any(decision.reason == "input_budget_exhausted" for decision in budget_pack.decisions)


def test_provider_outage_named_omission(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "README.md").write_text(
        "# verdict\nshared recall outage still compiles\n", encoding="utf-8"
    )
    plane = MemoryPlane(tmp_path / "memory.db")
    plane.put(
        MemoryRecord(
            "local1",
            "docs",
            "local",
            "local native memory still available",
            "test",
            created_at=1.0,
            updated_at=1.0,
        )
    )
    shared = SharedMemoryCapabilityProvider(
        FakeSharedMemoryProvider(status=ProviderResultStatus.UNAVAILABLE), project="verdict"
    )
    resolver = NativeCapabilityResolver((*build_native_providers(), shared))
    plan = plan_context_query(
        "local native memory still available", token_budget=2048, max_units=8, extra_capabilities=()
    )
    retrieval = execute_context_query(plan, repo_root=root, plane=plane, resolver=resolver)
    plane.close()
    assert retrieval.units
    assert any(
        gap.reason == "provider_unreachable" and gap.provider_id == "adapter.shared_memory"
        for gap in retrieval.coverage.omitted
    )
    assert any(unit.authority != "shared-memory-advisory" for unit in retrieval.units)


def test_healthy_zero_vs_outage(tmp_path: Path) -> None:
    healthy = SharedMemoryCapabilityProvider(FakeSharedMemoryProvider(), project="verdict")
    zero = healthy.provide(capability_id="memory.search", query="no-such-term", repo_root=tmp_path)
    assert zero.units == ()
    assert zero.gap_reason == "healthy_zero_hits"

    outage = SharedMemoryCapabilityProvider(
        FakeSharedMemoryProvider(status=ProviderResultStatus.TIMEOUT), project="verdict"
    ).provide(capability_id="memory.search", query="anything", repo_root=tmp_path)
    assert outage.gap_reason == "provider_timeout"
    assert outage.gap_reason != zero.gap_reason


def test_provenance_survives_receipt(tmp_path: Path) -> None:
    provider = FakeSharedMemoryProvider()
    envelope = _envelope(content="provenance must survive pack receipt")
    ref = provider.put(envelope)
    unit = (
        SharedMemoryCapabilityProvider(provider, project="verdict")
        .provide(capability_id="memory.search", query="provenance", repo_root=tmp_path)
        .units[0]
    )
    plan = ContextPlan(
        plan_id="p-prov",
        candidate_id="c-prov",
        tenant_scope="default",
        project_scope="verdict",
        token_budget=2048,
    )
    pack = ContextPackCompiler().compile_units([unit], plan)
    receipt = pack.receipt
    payload = receipt.to_dict()
    assert unit.source_digest == f"sha256:{envelope.content_hash}"
    assert any(decision.reversible_ref == unit.source_uri for decision in receipt.decisions)
    assert ref.external_id in unit.source_uri
    assert "provenance must survive" in pack.compiled_prompt
    assert payload["decisions"]
