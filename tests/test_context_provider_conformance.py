from __future__ import annotations

import pytest

from verdict.memory_adapters import AdapterDescriptor, build_default_adapter_registry
from verdict.memory_gate import AuthorityLevel, MemoryGate, MemoryWriteRequest
from verdict.memory_plane import MemoryPlane, MemoryRecord


def test_retrieval_is_advisory_and_preserves_score_and_rank(tmp_path) -> None:
    with MemoryPlane(tmp_path / "memory.db") as plane:
        plane.put(MemoryRecord("r1", "docs", "k1", "shared content", "local", scope="default"))
        results = plane.search_ranked("content", scope="default")
        assert len(results) == 1
        assert isinstance(results[0].score, float)
        assert results[0].rank == 1
        # Advisory ranking does not alter validity or trust
        assert results[0].record.trust == "local-observation"


def test_unverified_write_cannot_claim_verified_authority(tmp_path) -> None:
    with MemoryPlane(tmp_path / "memory.db") as plane:
        gate = MemoryGate(plane)
        res = gate.write(
            MemoryWriteRequest(
                namespace="patterns",
                key="k1",
                value="content",
                authority="caller",
                authority_level=AuthorityLevel.SYSTEM,
            )
        )
        assert res.allowed is False
        assert res.reason == "authority_level_mismatch"


def test_provider_outage_returns_explicit_unavailable_state(tmp_path) -> None:
    registry = build_default_adapter_registry()
    resolution = registry.resolve("missing_provider")
    assert resolution.available is False
    assert resolution.status == "unknown"

    with MemoryPlane(tmp_path / "memory.db") as plane:
        summary = registry.ingest_many(
            [{"adapter_id": "masterdocs-sqlite"}], plane=plane, root=tmp_path
        )
        assert summary.status == "unavailable"
        assert summary.reports[0].status == "unavailable"


def test_stale_record_is_labeled_when_ttl_exceeded(tmp_path) -> None:
    """FR-001/FR-002/SC-001: a record older than its provider's declared TTL
    is annotated stale; a fresh record from the same provider is not."""
    now = 1_000_000.0
    with MemoryPlane(tmp_path / "memory.db") as plane:
        plane.put(
            MemoryRecord("aged", "docs", "k1", "old content", "document", created_at=now - 120)
        )
        plane.put(
            MemoryRecord("fresh", "docs", "k2", "new content", "document", created_at=now - 5)
        )
        results = plane.search_ranked("content", ttl_lookup={"document": 60.0}, now=now)
        by_id = {r.record.record_id: r for r in results}
        assert by_id["aged"].stale is True
        assert by_id["fresh"].stale is False


def test_no_ttl_declared_never_marks_stale(tmp_path) -> None:
    """FR-003/SC-002: absence of a declared TTL fails open, never stale."""
    now = 1_000_000.0
    with MemoryPlane(tmp_path / "memory.db") as plane:
        plane.put(
            MemoryRecord("very-old", "docs", "k1", "ancient content", "document", created_at=0.0)
        )
        results = plane.search_ranked("content", ttl_lookup={}, now=now)
        assert results[0].stale is False
        # Also confirm the default (no ttl_lookup argument at all) fails open.
        results_default = plane.search_ranked("content", now=now)
        assert results_default[0].stale is False


def _real_adapter_ids() -> list[str]:
    """Every adapter type build_default_adapter_registry() actually ships,
    enumerated from the real registry rather than a hardcoded list, so a
    fifth adapter added later is automatically covered (FR-005)."""
    registry = build_default_adapter_registry()
    return [
        descriptor.adapter_id
        for descriptor in registry.list()
        if descriptor.effective_status != "unavailable"
    ]


def test_every_registered_adapter_type_is_covered() -> None:
    """Guards against the enumeration silently shrinking to zero.

    Five, not four: the registry also ships code-graph-manifest, which
    neither ADR-022 nor issue #287 named explicitly — discovered by this
    enumeration itself rather than assumed. This is exactly why FR-005
    requires iterating the real registry instead of a hardcoded id list.
    """
    ids = _real_adapter_ids()
    assert set(ids) == {
        "local-manifest",
        "document",
        "session-jsonl",
        "masterdocs-manifest",
        "code-graph-manifest",
    }


@pytest.mark.parametrize("adapter_id", _real_adapter_ids())
def test_every_registered_adapter_reports_explicit_unavailable_state(
    adapter_id: str, tmp_path
) -> None:
    """FR-005: the explicit-unavailable-state guarantee (ADR-022, property 3)
    already proven for one legacy id must hold for every real adapter type
    the default registry ships, not only masterdocs-manifest's sibling."""
    registry = build_default_adapter_registry()
    registry.declare_unavailable(
        AdapterDescriptor(adapter_id, description="simulated outage for conformance")
    )
    with MemoryPlane(tmp_path / "memory.db") as plane:
        summary = registry.ingest_many([{"adapter_id": adapter_id}], plane=plane, root=tmp_path)
    assert summary.status == "unavailable"
    assert summary.reports[0].status == "unavailable"
    assert summary.reports[0].adapter_id == adapter_id


def test_masterdocs_sqlite_legacy_id_is_refused() -> None:
    """FR-006: the documented masterdocs-sqlite refusal is test-enforced,
    not only asserted in a comment."""
    registry = build_default_adapter_registry()
    resolution = registry.resolve("masterdocs-sqlite")
    assert resolution.available is False
    assert resolution.status == "unavailable"
    assert resolution.reason == "private database boundary unsupported; use masterdocs-manifest"


def test_staleness_uses_recorded_timestamp_not_live_probe(tmp_path, monkeypatch) -> None:
    """FR-004: staleness is computed from the record's own created_at, never
    by calling out to the provider/adapter registry."""
    import verdict.memory_adapters as memory_adapters_module

    def _fail_if_called(*args: object, **kwargs: object) -> None:
        raise AssertionError("search_ranked must not call the adapter registry")

    monkeypatch.setattr(memory_adapters_module.AdapterRegistry, "resolve", _fail_if_called)
    now = 1_000_000.0
    with MemoryPlane(tmp_path / "memory.db") as plane:
        plane.put(MemoryRecord("r1", "docs", "k1", "content here", "document", created_at=now))
        results = plane.search_ranked("content", ttl_lookup={"document": 60.0}, now=now)
        assert results[0].stale is False
