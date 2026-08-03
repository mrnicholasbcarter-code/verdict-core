from __future__ import annotations

import pytest

from verdict.context_pack import ContextPackCompiler, ContextPackSlot
from verdict.memory_adapters import (
    AdapterDescriptor,
    AdapterRegistry,
    AdapterResolution,
    build_default_adapter_registry,
)
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
