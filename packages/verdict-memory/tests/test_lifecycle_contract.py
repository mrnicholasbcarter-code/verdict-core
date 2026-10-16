"""Contract tests for lifecycle-event/v1 + FakeProvider + outbox dedupe."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from verdict_memory import (
    CONTRACT_API_VERSION,
    LIFECYCLE_EVENT_SCHEMA,
    CaptureDecision,
    FakeMemoryProvider,
    LifecycleEvent,
    ProviderResultStatus,
    RecallQuery,
    canonicalize,
    event_digest,
    loads_lifecycle_event,
    redact_secrets,
)
from verdict_memory.contracts import EventIdentity
from verdict_memory.outbox import MemoryOutbox
from verdict_memory.policy import MemoryCapturePolicy

VECTORS = Path(__file__).resolve().parents[1] / "vectors"


def _event(**overrides):
    base = dict(
        kind="manual_capture",
        content="shared note about routing budgets",
        identity=EventIdentity(
            source_harness="verdict",
            project="verdict",
            session_id="sess-1",
            parent_session_id=None,
            attempt_id="attempt-1",
            tenant="default",
            workspace="default",
            source_agent="worker-a",
        ),
        created_at=1_700_000_000.0,
        observed_at=1_700_000_001.0,
        scope="default",
        metadata={"extension": {"k": 1}},
    )
    base.update(overrides)
    return LifecycleEvent(**base)


def test_api_version_experimental():
    assert CONTRACT_API_VERSION == "0.1.0a1"
    assert LIFECYCLE_EVENT_SCHEMA == "lifecycle-event/v1"


def test_event_roundtrip_and_digest_excludes_observed_at():
    first = _event(metadata={"b": 2, "a": 1})
    second = LifecycleEvent.from_dict(first.to_dict())
    assert second.to_dict() == first.to_dict()
    assert first.schema_version == LIFECYCLE_EVENT_SCHEMA
    # Equivalent reordered metadata + different observed_at => same digest
    twin = _event(metadata={"a": 1, "b": 2}, observed_at=9_999.0)
    assert twin.digest() == first.digest()
    assert twin.idempotency_key == first.digest()


def test_canonical_golden_vector():
    vector_path = VECTORS / "lifecycle_event_v1_minimal.json"
    assert vector_path.is_file(), "golden vector missing"
    raw = json.loads(vector_path.read_text())
    event = LifecycleEvent.from_dict(raw["event"])
    assert event.digest() == raw["digest"]
    assert canonicalize(event.to_dict()) == raw["canonical_json"]
    # Round-trip via loads
    loaded = loads_lifecycle_event(raw["canonical_json"])
    assert loaded.digest() == raw["digest"]


def test_fake_provider_scope_filter_and_healthy_empty():
    provider = FakeMemoryProvider()
    kept = provider.put(_event(content="alpha budget note", identity=EventIdentity(
        source_harness="verdict", project="verdict", session_id="s1",
    ), scope="alpha"))
    provider.put(_event(content="beta budget note", identity=EventIdentity(
        source_harness="verdict", project="verdict", session_id="s2",
    ), scope="beta"))
    provider.put(_event(content="other project", identity=EventIdentity(
        source_harness="verdict", project="other", session_id="s3",
    ), scope="alpha"))

    alpha = provider.search(RecallQuery("budget", project="verdict", scope="alpha"))
    assert alpha.status is ProviderResultStatus.AVAILABLE
    assert len(alpha.hits) == 1
    assert alpha.hits[0].ref.external_id == kept.external_id
    assert alpha.hits[0].untrusted is True

    empty = provider.search(RecallQuery("missing-term", project="verdict", scope="alpha"))
    assert empty.status is ProviderResultStatus.AVAILABLE
    assert empty.hits == ()


def test_recall_never_grants_authority():
    provider = FakeMemoryProvider()
    provider.put(_event(content="advisory only", trust="spoofed", metadata={"authority": "root"}))
    hit = provider.search(RecallQuery("advisory", project="verdict")).hits[0]
    assert hit.untrusted is True
    assert hit.event.trust == "spoofed"  # preserved as data
    # Consumer must treat as untrusted — package marks the hit.


def test_redact_secrets_before_persist_semantics():
    text = "Authorization: Bearer canary-secret-TOKEN-9f3c2b1a"
    assert "canary-secret" not in (redact_secrets(text) or "")


def test_capture_policy_blocks_secrets_and_noise():
    policy = MemoryCapturePolicy()
    secret = policy.evaluate(_event(content="api_key=sk-abcdefghijklmnopqrstuv"))
    assert secret == CaptureDecision(False, "secret_detected", secret.event_digest)
    noise = policy.evaluate(_event(kind="after_tool", memory_kind="tool", content="tool stdout"))
    assert noise.eligible is False
    assert noise.reason == "low_value_tool_noise"
    ok = policy.evaluate(_event())
    assert ok.eligible is True


def test_outbox_dedupe_same_logical_source_once(tmp_path: Path):
    box = MemoryOutbox(tmp_path / "outbox.sqlite")
    e1 = _event(observed_at=1.0)
    e2 = _event(observed_at=2.0)  # same logical identity
    assert e1.digest() == e2.digest()
    first = box.enqueue(e1)
    second = box.enqueue(e2)
    assert first is not None
    assert second is not None
    assert first.idempotency_key == second.idempotency_key
    assert box.count("pending") == 1
    due = box.due()
    assert len(due) == 1
    box.mark_acked(due[0], external_id="ext-1")
    assert box.count("pending") == 0
    assert box.count("acked") == 1
    # Replay after ack does not create a second pending row
    third = box.enqueue(e1)
    assert third is not None
    assert third.state == "acked"
    assert box.count("pending") == 0


def test_outbox_rejects_ineligible(tmp_path: Path):
    box = MemoryOutbox(tmp_path / "outbox.sqlite")
    assert box.enqueue(_event(content="password=hunter2")) is None
    assert box.count() == 0
