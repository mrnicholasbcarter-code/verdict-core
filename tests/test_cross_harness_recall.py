"""Cross-harness recall: provenance survives write→mirror→recall across harness pairs.

Covers BOD-80 ACs 4, 5, 12:
  AC-4  Hermes→Prime/Claude/Codex cross-harness recall
  AC-5  Prime→another harness recall
  AC-12 Source provenance survives cross-harness recall

Each test writes a SharedMemoryEnvelope through one harness's real outbox/mirror
path, recalls through the shared provider, and asserts every provenance field
(source_harness, source_session, timestamps, trust, authority) survives unchanged.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from verdict.memory_mirror import MemoryMirrorWorker
from verdict.memory_outbox import MemoryOutbox
from verdict.memory_plane import MemoryPlane, MemoryRecord
from verdict.shared_memory import FakeSharedMemoryProvider, SharedMemoryEnvelope, SharedMemoryQuery

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _Clock:
    """Deterministic clock for reproducible timestamps."""

    def __init__(self, value: float = 1_700_000_000.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def _make_record(
    *,
    harness: str,
    session: str,
    content: str = "cross-harness test content",
    namespace: str = "docs",
    key: str = "note",
    scope: str = "default",
    trust: str = "gated-local-observation",
    confidence: float = 0.9,
    provenance: dict[str, Any] | None = None,
    created_at: float = 1_700_000_000.0,
) -> MemoryRecord:
    """Build a MemoryRecord tagged with harness/session origin."""
    return MemoryRecord(
        record_id=f"rec_{harness}_{session}",
        namespace=namespace,
        key=key,
        content=content,
        source=harness,
        trust=trust,
        scope=scope,
        confidence=confidence,
        provenance={"origin_harness": harness, "origin_session": session, **(provenance or {})},
        created_at=created_at,
        updated_at=created_at,
    )


def _write_and_mirror(
    *,
    tmp_path: Path,
    source_harness: str,
    source_session: str,
    provider: FakeSharedMemoryProvider,
    content: str = "cross-harness test content",
    trust: str = "gated-local-observation",
    provenance: dict[str, Any] | None = None,
    created_at: float = 1_700_000_000.0,
) -> SharedMemoryEnvelope:
    """Write a record through the real outbox/mirror path and return the mirrored envelope."""
    clock = _Clock(created_at)
    db_path = tmp_path / f"{source_harness}_{source_session}.db"

    outbox = MemoryOutbox(
        db_path,
        project="verdict-test",
        tenant="test-tenant",
        source_harness=source_harness,
        source_agent=f"agent-{source_harness}",
        clock=clock,
    )

    plane = MemoryPlane(db_path, outbox=outbox)

    record = _make_record(
        harness=source_harness,
        session=source_session,
        content=content,
        trust=trust,
        provenance=provenance,
        created_at=created_at,
    )

    # Write through the real MemoryPlane → outbox enqueue path
    stored = plane.put(record)
    assert stored.record_id == record.record_id

    # Verify the outbox has a pending event
    due = outbox.due(limit=10, now=clock.value + 1)
    assert len(due) >= 1, "outbox should have at least one pending event"

    # Mirror through the real MemoryMirrorWorker path
    worker = MemoryMirrorWorker(outbox, provider, clock=clock)
    clock.advance(1.0)
    result = worker.run_once()
    assert result.acked >= 1, f"expected acked>=1, got {result}"

    plane.close()
    return due[0].envelope


def _recall_from_provider(
    provider: FakeSharedMemoryProvider,
    query: str = "cross-harness",
    project: str = "verdict-test",
    tenant: str = "test-tenant",
) -> SharedMemoryEnvelope:
    """Search the shared provider and return the first hit's envelope."""
    result = provider.search(SharedMemoryQuery(query=query, project=project, tenant=tenant))
    assert result.hits, "expected at least one hit from shared provider"
    return result.hits[0].envelope


# ---------------------------------------------------------------------------
# Cross-harness pair tests
# ---------------------------------------------------------------------------

_HARNESS_PAIRS = [
    ("hermes", "codex"),
    ("prime", "claude"),
    ("claude", "prime"),
    ("codex", "hermes"),
]


@pytest.fixture(params=_HARNESS_PAIRS, ids=[f"{a}->{b}" for a, b in _HARNESS_PAIRS])
def harness_pair(request: pytest.FixtureRequest) -> tuple[str, str]:
    return request.param


class TestCrossHarnessRecall:
    """Deterministic cross-harness recall via the real outbox→mirror→provider path."""

    def test_content_survives_cross_harness_recall(
        self, tmp_path: Path, harness_pair: tuple[str, str]
    ) -> None:
        """Content written by harness A is recalled intact by harness B."""
        source, _target = harness_pair
        provider = FakeSharedMemoryProvider()
        content = f"decision: use {source} for routing"

        _write_and_mirror(
            tmp_path=tmp_path,
            source_harness=source,
            source_session=f"sess-{source}-001",
            provider=provider,
            content=content,
        )

        recalled = _recall_from_provider(provider, query="decision routing")
        assert recalled.content == content

    def test_source_harness_survives(self, tmp_path: Path, harness_pair: tuple[str, str]) -> None:
        """source_harness field set by the writing harness survives recall."""
        source, _target = harness_pair
        provider = FakeSharedMemoryProvider()

        _write_and_mirror(
            tmp_path=tmp_path,
            source_harness=source,
            source_session=f"sess-{source}-002",
            provider=provider,
        )

        recalled = _recall_from_provider(provider)
        assert recalled.source_harness == source

    def test_source_agent_survives(self, tmp_path: Path, harness_pair: tuple[str, str]) -> None:
        """source_agent field survives cross-harness recall."""
        source, _target = harness_pair
        provider = FakeSharedMemoryProvider()

        _write_and_mirror(
            tmp_path=tmp_path,
            source_harness=source,
            source_session=f"sess-{source}-003",
            provider=provider,
        )

        recalled = _recall_from_provider(provider)
        assert recalled.source_agent == f"agent-{source}"

    def test_provenance_fields_survive(self, tmp_path: Path, harness_pair: tuple[str, str]) -> None:
        """All provenance fields including custom keys survive cross-harness recall."""
        source, _target = harness_pair
        provider = FakeSharedMemoryProvider()
        session_id = f"sess-{source}-004"
        custom_provenance = {"custom_key": f"value-from-{source}", "depth": 3}

        _write_and_mirror(
            tmp_path=tmp_path,
            source_harness=source,
            source_session=session_id,
            provider=provider,
            provenance=custom_provenance,
        )

        recalled = _recall_from_provider(provider)

        # Core provenance fields injected by MemoryOutbox.envelope_for()
        assert "local_record_id" in recalled.provenance
        assert recalled.provenance["local_record_id"] == f"rec_{source}_{session_id}"
        assert recalled.provenance["local_source"] == source

        # Custom provenance from the original record
        assert recalled.provenance["origin_harness"] == source
        assert recalled.provenance["origin_session"] == session_id
        assert recalled.provenance["custom_key"] == f"value-from-{source}"
        assert recalled.provenance["depth"] == 3

    def test_trust_survives(self, tmp_path: Path, harness_pair: tuple[str, str]) -> None:
        """Trust level set at write time survives cross-harness recall."""
        source, _target = harness_pair
        provider = FakeSharedMemoryProvider()
        trust = "verified-observation"

        _write_and_mirror(
            tmp_path=tmp_path,
            source_harness=source,
            source_session=f"sess-{source}-005",
            provider=provider,
            trust=trust,
        )

        recalled = _recall_from_provider(provider)
        assert recalled.trust == trust

    def test_timestamps_survive(self, tmp_path: Path, harness_pair: tuple[str, str]) -> None:
        """created_at and observed_at timestamps survive cross-harness recall."""
        source, _target = harness_pair
        provider = FakeSharedMemoryProvider()
        created = 1_700_100_000.0

        _write_and_mirror(
            tmp_path=tmp_path,
            source_harness=source,
            source_session=f"sess-{source}-006",
            provider=provider,
            created_at=created,
        )

        recalled = _recall_from_provider(provider)
        assert recalled.created_at == created
        assert recalled.observed_at == created

    def test_content_hash_survives(self, tmp_path: Path, harness_pair: tuple[str, str]) -> None:
        """Content hash is consistent after cross-harness recall."""
        source, _target = harness_pair
        provider = FakeSharedMemoryProvider()
        content = f"unique content from {source}"

        envelope = _write_and_mirror(
            tmp_path=tmp_path,
            source_harness=source,
            source_session=f"sess-{source}-007",
            provider=provider,
            content=content,
        )

        recalled = _recall_from_provider(provider, query="unique content")
        assert recalled.content_hash == envelope.content_hash
        assert recalled.content_hash != ""

    def test_authority_and_sensitivity_survive(
        self, tmp_path: Path, harness_pair: tuple[str, str]
    ) -> None:
        """Authority and sensitivity fields survive cross-harness recall."""
        source, _target = harness_pair
        provider = FakeSharedMemoryProvider()

        _write_and_mirror(
            tmp_path=tmp_path,
            source_harness=source,
            source_session=f"sess-{source}-008",
            provider=provider,
        )

        recalled = _recall_from_provider(provider)
        assert recalled.authority == "shared-memory-advisory"
        assert recalled.authority_verified is False
        assert recalled.sensitivity == "standard"


class TestCrossHarnessRoundTrip:
    """Full round-trip: write via harness A outbox, mirror, recall, verify envelope identity."""

    def test_envelope_dict_round_trip(self, tmp_path: Path) -> None:
        """Envelope survives to_dict → from_dict serialization across harnesses."""
        provider = FakeSharedMemoryProvider()

        envelope = _write_and_mirror(
            tmp_path=tmp_path,
            source_harness="hermes",
            source_session="round-trip-001",
            provider=provider,
            content="round-trip serialization test",
            provenance={"trace_id": "abc123"},
        )

        # Simulate cross-harness serialization boundary
        serialized = envelope.to_dict()
        deserialized = SharedMemoryEnvelope.from_dict(serialized)

        assert deserialized.content == envelope.content
        assert deserialized.source_harness == "hermes"
        assert deserialized.provenance["trace_id"] == "abc123"
        assert deserialized.content_hash == envelope.content_hash
        assert deserialized.digest() == envelope.digest()

    def test_multiple_harnesses_coexist_in_provider(self, tmp_path: Path) -> None:
        """Records from multiple source harnesses coexist and are distinguishable."""
        provider = FakeSharedMemoryProvider()

        for harness in ("hermes", "prime", "claude", "codex"):
            _write_and_mirror(
                tmp_path=tmp_path,
                source_harness=harness,
                source_session=f"coexist-{harness}",
                provider=provider,
                content=f"record from {harness} harness",
            )

        result = provider.search(
            SharedMemoryQuery(
                query="record harness", project="verdict-test", tenant="test-tenant", limit=10
            )
        )

        assert len(result.hits) == 4
        harnesses_found = {hit.envelope.source_harness for hit in result.hits}
        assert harnesses_found == {"hermes", "prime", "claude", "codex"}

    def test_idempotency_key_stable_across_recall(self, tmp_path: Path) -> None:
        """The idempotency_key is deterministic and stable after recall."""
        provider = FakeSharedMemoryProvider()

        envelope = _write_and_mirror(
            tmp_path=tmp_path,
            source_harness="prime",
            source_session="idempotency-001",
            provider=provider,
            content="idempotency check content",
        )

        recalled = _recall_from_provider(provider, query="idempotency check")
        assert recalled.idempotency_key == envelope.idempotency_key
        assert recalled.idempotency_key != ""

    def test_schema_and_protocol_versions_survive(self, tmp_path: Path) -> None:
        """Schema and protocol version fields survive cross-harness recall."""
        provider = FakeSharedMemoryProvider()

        envelope = _write_and_mirror(
            tmp_path=tmp_path,
            source_harness="codex",
            source_session="version-001",
            provider=provider,
        )

        recalled = _recall_from_provider(provider)
        assert recalled.schema_version == envelope.schema_version
        assert recalled.protocol_version == envelope.protocol_version


class TestCrossHarnessMemoryGatePath:
    """Write through the MemoryGate → MemoryPlane → outbox → mirror path.

    This exercises the full write path that harness hooks use via
    MemoryHookController.write_memory() → MemoryGate.write().
    """

    def test_gate_write_to_cross_harness_recall(self, tmp_path: Path) -> None:
        """A record written through MemoryGate is mirrored and recallable."""
        from verdict.memory_gate import MemoryGate, MemoryWriteRequest

        clock = _Clock()
        db_path = tmp_path / "gate_cross.db"
        provider = FakeSharedMemoryProvider()

        outbox = MemoryOutbox(
            db_path,
            project="verdict-test",
            tenant="test-tenant",
            source_harness="hermes",
            source_agent="agent-hermes",
            clock=clock,
        )
        plane = MemoryPlane(db_path, outbox=outbox)
        gate = MemoryGate(plane)

        req = MemoryWriteRequest(
            namespace="docs",
            key="gate-test",
            value="gate-written cross-harness content",
            source="hermes",
            authority="agent",
            trust="gated-local-observation",
            sensitivity="standard",
            provenance={"gate_origin": "hermes", "session": "gate-sess-001"},
        )

        result = gate.write(req)
        assert result.allowed, f"gate rejected write: {result.reason}"

        # Mirror — advance clock so due() returns pending events
        clock.advance(1.0)
        worker = MemoryMirrorWorker(outbox, provider, clock=clock)
        batch = worker.run_once()
        assert batch.acked >= 1

        # Recall
        recalled = _recall_from_provider(provider, query="gate-written cross-harness")
        assert recalled.source_harness == "hermes"
        assert recalled.content == "gate-written cross-harness content"
        assert "gate_origin" in recalled.provenance
        assert recalled.provenance["gate_origin"] == "hermes"

        gate.close()

    def test_hook_controller_write_to_cross_harness_recall(self, tmp_path: Path) -> None:
        """A record written through MemoryHookController is mirrored and recallable."""
        from verdict.memory_bridge import MemoryHookController
        from verdict.memory_gate import MemoryGate, MemoryWriteRequest

        clock = _Clock()
        db_path = tmp_path / "hook_cross.db"
        provider = FakeSharedMemoryProvider()

        outbox = MemoryOutbox(
            db_path,
            project="verdict-test",
            tenant="test-tenant",
            source_harness="prime",
            source_agent="agent-prime",
            clock=clock,
        )
        plane = MemoryPlane(db_path, outbox=outbox)
        gate = MemoryGate(plane)

        controller = MemoryHookController(plane=plane, gate=gate)

        # Write through the hook controller's write_memory path
        write_req = MemoryWriteRequest(
            namespace="sessions",
            key="hook-test",
            value="hook controller cross-harness content",
            source="prime",
            authority="agent",
            sensitivity="standard",
            provenance={"hook_origin": "prime", "session": "hook-sess-001"},
        )
        write_result = controller.write_memory(write_req)
        assert write_result["allowed"]

        # Mirror — advance clock so due() returns pending events
        clock.advance(1.0)
        worker = MemoryMirrorWorker(outbox, provider, clock=clock)
        batch = worker.run_once()
        assert batch.acked >= 1

        # Recall via provider (simulating another harness)
        recalled = _recall_from_provider(provider, query="hook controller cross-harness")
        assert recalled.source_harness == "prime"
        assert recalled.content == "hook controller cross-harness content"
        assert recalled.provenance["hook_origin"] == "prime"

        plane.close()
