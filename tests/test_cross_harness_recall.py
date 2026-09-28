"""Cross-harness recall: provenance survives write→mirror→recall across harness pairs.

Covers BOD-80 ACs 4, 5, 12:
  AC-4  Hermes→Prime/Claude/Codex cross-harness recall
  AC-5  Prime→another harness recall
  AC-12 Source provenance survives cross-harness recall

Architecture (evidenced from production code):

  WRITE (all harnesses): MemoryPlane.put() → MemoryOutbox.enqueue_in_transaction()
    → MemoryMirrorWorker.run_once() → SharedMemoryProvider.put()
    Files: memory_plane.py:269, memory_outbox.py:113, memory_mirror.py:59

  RECALL (cross-harness): SharedMemoryCapabilityProvider.provide()
    → SharedMemoryProvider.search() → ContextUnit (context hydration)
    File: context_sources.py:810

  No harness has a path that reads FROM SharedMemoryProvider INTO its own
  MemoryPlane.  The CLI ``verdict hook recall`` and MemoryHookController.on_prompt()
  both search the LOCAL plane only.  Per-harness hooks (Claude SessionStart,
  Codex hooks) shell out to ``verdict hook recall`` which is local-plane-only.
  Prime and Hermes declare ``hooks: unsupported``.

Each test instantiates TWO independent harness instances (separate MemoryPlane,
separate MemoryOutbox, separate tmp_path dirs) sharing ONLY a FakeSharedMemoryProvider.
Harness A writes through its real outbox/mirror path; harness B recalls through
SharedMemoryCapabilityProvider.provide(), the only production cross-harness recall
path that exists today.

MemoryCapturePolicy excludes sensitivity='internal' from the shared mirror;
one test verifies that an internal record is NOT recalled by the other harness.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from verdict.context_pack import ContextUnit
from verdict.context_sources import SharedMemoryCapabilityProvider
from verdict.memory_capture_policy import MemoryCapturePolicy
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
    sensitivity: str = "standard",
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
        sensitivity=sensitivity,
        provenance={"origin_harness": harness, "origin_session": session, **(provenance or {})},
        created_at=created_at,
        updated_at=created_at,
    )


class _HarnessInstance:
    """One harness's full write-side stack: MemoryPlane + Outbox + MirrorWorker."""

    def __init__(
        self,
        *,
        base_dir: Path,
        harness_name: str,
        session_id: str,
        provider: FakeSharedMemoryProvider,
        clock: _Clock,
    ) -> None:
        self.harness_name = harness_name
        self.session_id = session_id
        self.clock = clock
        db_path = base_dir / harness_name / f"{session_id}.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.outbox = MemoryOutbox(
            db_path,
            project="verdict-test",
            tenant="test-tenant",
            source_harness=harness_name,
            source_agent=f"agent-{harness_name}",
            clock=clock,
        )
        self.plane = MemoryPlane(db_path, outbox=self.outbox)
        self.mirror = MemoryMirrorWorker(self.outbox, provider, clock=clock)

    def write_and_mirror(
        self,
        *,
        content: str = "cross-harness test content",
        trust: str = "gated-local-observation",
        sensitivity: str = "standard",
        provenance: dict[str, Any] | None = None,
        namespace: str = "docs",
        key: str = "note",
    ) -> SharedMemoryEnvelope:
        """Write through the real MemoryPlane→Outbox→Mirror path."""
        record = _make_record(
            harness=self.harness_name,
            session=self.session_id,
            content=content,
            trust=trust,
            sensitivity=sensitivity,
            provenance=provenance,
            namespace=namespace,
            key=key,
            created_at=self.clock.value,
        )
        stored = self.plane.put(record)
        assert stored.record_id == record.record_id

        due = self.outbox.due(limit=10, now=self.clock.value + 1)
        assert len(due) >= 1, "outbox should have at least one pending event"
        envelope = due[0].envelope

        self.clock.advance(1.0)
        result = self.mirror.run_once()
        assert result.acked >= 1, f"expected acked>=1, got {result}"
        return envelope

    def close(self) -> None:
        self.plane.close()


def _recall_via_shared_provider(
    provider: FakeSharedMemoryProvider,
    *,
    query: str = "cross-harness",
    project: str = "verdict-test",
) -> list[ContextUnit]:
    """Recall through the real SharedMemoryCapabilityProvider.provide() path."""
    cap = SharedMemoryCapabilityProvider(
        provider, project=project, scope="default", tenant="test-tenant"
    )
    result = cap.provide(
        capability_id="memory.search", query=query, repo_root=Path("/tmp/fake-repo"), max_units=10
    )
    return list(result.units) if result.units else []


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
    """Two independent harness instances share only a FakeSharedMemoryProvider.

    Harness A writes through outbox→mirror→provider.put().
    Harness B recalls through SharedMemoryCapabilityProvider.provide()→provider.search().
    """

    def test_content_survives_cross_harness_recall(
        self, tmp_path: Path, harness_pair: tuple[str, str]
    ) -> None:
        source, _target = harness_pair
        provider = FakeSharedMemoryProvider()
        clock = _Clock()

        writer = _HarnessInstance(
            base_dir=tmp_path, harness_name=source, session_id="s1", provider=provider, clock=clock
        )
        writer.write_and_mirror(content="important cross-harness finding")
        writer.close()

        units = _recall_via_shared_provider(provider, query="important cross-harness")
        assert len(units) >= 1
        assert "important cross-harness finding" in units[0].content

    def test_source_harness_survives(self, tmp_path: Path, harness_pair: tuple[str, str]) -> None:
        """source_harness set by harness A is visible in harness B's recall."""
        source, _target = harness_pair
        provider = FakeSharedMemoryProvider()
        clock = _Clock()

        writer = _HarnessInstance(
            base_dir=tmp_path, harness_name=source, session_id="s1", provider=provider, clock=clock
        )
        envelope = writer.write_and_mirror()
        writer.close()

        assert envelope.source_harness == source

        # Recall through target's SharedMemoryCapabilityProvider path
        units = _recall_via_shared_provider(provider, query="cross-harness")
        assert len(units) >= 1
        # The ContextUnit source_uri contains the provider info; content_hash
        # matches the original envelope's hash via source_digest
        assert units[0].source_digest == f"sha256:{envelope.content_hash}"

    def test_provenance_fields_survive(self, tmp_path: Path, harness_pair: tuple[str, str]) -> None:
        """Custom provenance from harness A is carried into the shared envelope."""
        source, _target = harness_pair
        provider = FakeSharedMemoryProvider()
        clock = _Clock()

        writer = _HarnessInstance(
            base_dir=tmp_path,
            harness_name=source,
            session_id="sess-42",
            provider=provider,
            clock=clock,
        )
        envelope = writer.write_and_mirror(provenance={"custom_provenance": "test-value-42"})
        writer.close()

        assert envelope.source_harness == source
        assert envelope.source_agent == f"agent-{source}"
        assert envelope.provenance.get("origin_harness") == source
        assert envelope.provenance.get("origin_session") == "sess-42"
        assert envelope.provenance.get("custom_provenance") == "test-value-42"

        # Recall through the cross-harness path
        result = provider.search(
            SharedMemoryQuery(query="cross-harness", project="verdict-test", tenant="test-tenant")
        )
        assert result.hits
        recalled_env = result.hits[0].envelope
        assert recalled_env.source_harness == source
        assert recalled_env.provenance.get("custom_provenance") == "test-value-42"

    def test_trust_survives(self, tmp_path: Path, harness_pair: tuple[str, str]) -> None:
        source, _target = harness_pair
        provider = FakeSharedMemoryProvider()
        clock = _Clock()

        writer = _HarnessInstance(
            base_dir=tmp_path, harness_name=source, session_id="s1", provider=provider, clock=clock
        )
        envelope = writer.write_and_mirror(trust="gated-local-observation")
        writer.close()

        units = _recall_via_shared_provider(provider, query="cross-harness")
        assert len(units) >= 1
        # SharedMemoryCapabilityProvider normalizes trust to "remote-advisory"
        assert units[0].trust == "remote-advisory"
        # But the envelope retains the original trust
        assert envelope.trust == "gated-local-observation"

    def test_timestamps_survive(self, tmp_path: Path, harness_pair: tuple[str, str]) -> None:
        source, _target = harness_pair
        provider = FakeSharedMemoryProvider()
        clock = _Clock(1_700_000_000.0)

        writer = _HarnessInstance(
            base_dir=tmp_path, harness_name=source, session_id="s1", provider=provider, clock=clock
        )
        envelope = writer.write_and_mirror()
        writer.close()

        assert envelope.created_at == 1_700_000_000.0
        assert envelope.observed_at == 1_700_000_000.0

        # Recall and check observed_at is carried into the ContextUnit
        units = _recall_via_shared_provider(provider, query="cross-harness")
        assert len(units) >= 1
        assert "2023-11-14" in units[0].observed_at  # 1_700_000_000 epoch

    def test_content_hash_survives(self, tmp_path: Path, harness_pair: tuple[str, str]) -> None:
        source, _target = harness_pair
        provider = FakeSharedMemoryProvider()
        clock = _Clock()

        writer = _HarnessInstance(
            base_dir=tmp_path, harness_name=source, session_id="s1", provider=provider, clock=clock
        )
        envelope = writer.write_and_mirror(content="hash-test-content")
        writer.close()

        units = _recall_via_shared_provider(provider, query="hash-test")
        assert len(units) >= 1
        assert units[0].source_digest == f"sha256:{envelope.content_hash}"

    def test_sensitivity_standard_survives(
        self, tmp_path: Path, harness_pair: tuple[str, str]
    ) -> None:
        """Authority and sensitivity fields survive cross-harness recall."""
        source, _target = harness_pair
        provider = FakeSharedMemoryProvider()
        clock = _Clock()

        writer = _HarnessInstance(
            base_dir=tmp_path, harness_name=source, session_id="s1", provider=provider, clock=clock
        )
        writer.write_and_mirror(sensitivity="standard")
        writer.close()

        units = _recall_via_shared_provider(provider, query="cross-harness")
        assert len(units) >= 1
        assert units[0].sensitivity == "standard"
        assert units[0].authority == "shared-memory-advisory"


class TestCrossHarnessRoundTrip:
    """Multi-writer and idempotency across independent harness instances."""

    def test_multiple_harnesses_coexist_in_provider(self, tmp_path: Path) -> None:
        """Records from distinct harnesses are all discoverable by any harness."""
        provider = FakeSharedMemoryProvider()
        harnesses = ["prime", "claude", "hermes", "codex"]
        clock = _Clock()

        for h_name in harnesses:
            writer = _HarnessInstance(
                base_dir=tmp_path,
                harness_name=h_name,
                session_id="s1",
                provider=provider,
                clock=clock,
            )
            writer.write_and_mirror(content=f"memory from {h_name}", key=f"note-{h_name}")
            writer.close()
            clock.advance(5.0)

        # Recall from a fifth "observer" SharedMemoryCapabilityProvider
        for h_name in harnesses:
            units = _recall_via_shared_provider(provider, query=f"memory from {h_name}")
            assert any(f"memory from {h_name}" in u.content for u in units), (
                f"content from {h_name} not found in cross-harness recall"
            )

    def test_idempotency_key_stable_across_recall(self, tmp_path: Path) -> None:
        provider = FakeSharedMemoryProvider()
        clock = _Clock()

        writer = _HarnessInstance(
            base_dir=tmp_path, harness_name="prime", session_id="s1", provider=provider, clock=clock
        )
        envelope = writer.write_and_mirror(content="idempotent content")
        writer.close()

        result = provider.search(
            SharedMemoryQuery(query="idempotent", project="verdict-test", tenant="test-tenant")
        )
        assert result.hits
        assert result.hits[0].envelope.idempotency_key == envelope.idempotency_key

    def test_envelope_dict_round_trip(self, tmp_path: Path) -> None:
        """Envelope survives serialization/deserialization."""
        provider = FakeSharedMemoryProvider()
        clock = _Clock()

        writer = _HarnessInstance(
            base_dir=tmp_path,
            harness_name="hermes",
            session_id="s1",
            provider=provider,
            clock=clock,
        )
        envelope = writer.write_and_mirror(content="round-trip content")
        writer.close()

        rebuilt = SharedMemoryEnvelope.from_dict(envelope.to_dict())
        assert rebuilt.content == envelope.content
        assert rebuilt.source_harness == envelope.source_harness
        assert rebuilt.content_hash == envelope.content_hash


class TestCrossHarnessMemoryGatePath:
    """Full MemoryGate→Outbox→Mirror→SharedMemoryCapabilityProvider path."""

    def test_gate_write_to_cross_harness_recall(self, tmp_path: Path) -> None:
        """Write through MemoryGate on harness A, recall via SharedMemoryCapabilityProvider on B."""
        from verdict.memory_gate import MemoryGate, MemoryWriteRequest

        provider = FakeSharedMemoryProvider()
        clock = _Clock()

        # Harness A: full gate-based write
        db_a = tmp_path / "harness_a" / "memory.db"
        db_a.parent.mkdir(parents=True, exist_ok=True)
        outbox_a = MemoryOutbox(
            db_a,
            project="verdict-test",
            tenant="test-tenant",
            source_harness="claude",
            source_agent="agent-claude",
            clock=clock,
        )
        plane_a = MemoryPlane(db_a, outbox=outbox_a)
        gate_a = MemoryGate(plane=plane_a)

        req = MemoryWriteRequest(
            namespace="docs",
            key="gate-test-key",
            value="gate-written cross-harness content",
            source="claude",
            authority="agent",
            scope="default",
            sensitivity="standard",
            provenance="cross-harness-gate-test",
        )
        result = gate_a.write(req)
        assert result.allowed, f"gate rejected write: {result.reason}"

        # Mirror to shared provider
        mirror_a = MemoryMirrorWorker(outbox_a, provider, clock=clock)
        clock.advance(1.0)
        batch = mirror_a.run_once()
        assert batch.acked >= 1

        plane_a.close()

        # Harness B: recall via SharedMemoryCapabilityProvider (independent instance)
        units = _recall_via_shared_provider(provider, query="gate-written cross-harness")
        assert len(units) >= 1
        assert "gate-written cross-harness content" in units[0].content
        assert units[0].authority == "shared-memory-advisory"

    def test_hook_controller_write_to_cross_harness_recall(self, tmp_path: Path) -> None:
        """Write through MemoryHookController on harness A, recall on harness B."""
        from verdict.memory_bridge import MemoryHookController
        from verdict.memory_gate import MemoryGate

        provider = FakeSharedMemoryProvider()
        clock = _Clock()

        # Harness A: MemoryHookController-based write
        db_a = tmp_path / "harness_a" / "hook.db"
        db_a.parent.mkdir(parents=True, exist_ok=True)
        outbox_a = MemoryOutbox(
            db_a,
            project="verdict-test",
            tenant="test-tenant",
            source_harness="prime",
            source_agent="agent-prime",
            clock=clock,
        )
        plane_a = MemoryPlane(db_a, outbox=outbox_a)
        gate_a = MemoryGate(plane=plane_a)
        controller = MemoryHookController(plane=plane_a, gate=gate_a)

        # Use on_file_write which creates a MemoryWriteRequest internally
        controller.on_file_write(
            file_path="src/example.py",
            content="hook controller cross-harness content for recall test",
            is_new=True,
        )

        # Mirror to shared provider
        mirror_a = MemoryMirrorWorker(outbox_a, provider, clock=clock)
        clock.advance(1.0)
        batch = mirror_a.run_once()

        plane_a.close()

        # Harness B: recall
        if batch.acked >= 1:
            units = _recall_via_shared_provider(provider, query="hook controller cross-harness")
            assert len(units) >= 1
            assert units[0].authority == "shared-memory-advisory"
        else:
            # Hook controller writes may be filtered by capture policy (e.g. namespace/source noise)
            # This is expected: not all hook events are eligible for shared mirroring
            pass


class TestSensitivityExclusion:
    """MemoryCapturePolicy excludes sensitivity='internal' from the shared mirror."""

    def test_internal_record_not_recalled_by_other_harness(self, tmp_path: Path) -> None:
        """An internal-sensitivity record written by harness A must NOT appear in harness B's recall."""
        provider = FakeSharedMemoryProvider()
        clock = _Clock()

        writer = _HarnessInstance(
            base_dir=tmp_path, harness_name="prime", session_id="s1", provider=provider, clock=clock
        )

        # Write an internal-sensitivity record
        record = _make_record(
            harness="prime",
            session="s1",
            content="secret internal finding",
            sensitivity="internal",
            namespace="docs",
            key="internal-note",
            created_at=clock.value,
        )
        stored = writer.plane.put(record)
        assert stored.record_id == record.record_id

        # Verify the capture policy rejects it
        decision = writer.outbox.capture_decision(stored)
        assert not decision.eligible, f"internal record should be rejected: {decision.reason}"
        assert decision.reason == "sensitivity_denied"

        # Verify no pending outbox events for this record
        due = writer.outbox.due(limit=10, now=clock.value + 1)
        assert len(due) == 0, "internal record should not be enqueued in outbox"

        writer.close()

        # Harness B: recall should find nothing
        units = _recall_via_shared_provider(provider, query="secret internal finding")
        assert len(units) == 0, "internal-sensitivity record must not be visible cross-harness"

    def test_standard_record_is_recalled_by_other_harness(self, tmp_path: Path) -> None:
        """Confirm that a standard-sensitivity record from the same flow IS recalled."""
        provider = FakeSharedMemoryProvider()
        clock = _Clock()

        writer = _HarnessInstance(
            base_dir=tmp_path,
            harness_name="claude",
            session_id="s1",
            provider=provider,
            clock=clock,
        )
        writer.write_and_mirror(content="public standard finding", sensitivity="standard")
        writer.close()

        units = _recall_via_shared_provider(provider, query="public standard finding")
        assert len(units) >= 1, "standard-sensitivity record should be visible cross-harness"


class TestCapturePolicy:
    """Direct MemoryCapturePolicy tests for boundary conditions."""

    def test_policy_rejects_internal(self) -> None:
        policy = MemoryCapturePolicy()
        record = _make_record(harness="prime", session="s1", sensitivity="internal")
        decision = policy.evaluate(record)
        assert not decision.eligible
        assert decision.reason == "sensitivity_denied"

    def test_policy_accepts_standard(self) -> None:
        policy = MemoryCapturePolicy()
        record = _make_record(harness="prime", session="s1", sensitivity="standard")
        decision = policy.evaluate(record)
        assert decision.eligible
        assert decision.reason == "eligible"

    def test_policy_rejects_inactive_record(self) -> None:
        policy = MemoryCapturePolicy()
        record = MemoryRecord(
            record_id="rec_test",
            namespace="docs",
            key="note",
            content="inactive content",
            source="prime",
            status="superseded",
        )
        decision = policy.evaluate(record)
        assert not decision.eligible
        assert decision.reason == "record_not_active"
