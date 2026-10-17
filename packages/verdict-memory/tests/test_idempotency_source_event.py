"""source_event_id dominates enqueue identity; conflict on payload mismatch."""

from __future__ import annotations

from pathlib import Path

import pytest

from verdict_memory import IdempotencyConflictError, LifecycleEvent
from verdict_memory.contracts import EventIdentity
from verdict_memory.outbox import MemoryOutbox


def _id(**overrides):
    base = {
        "source_harness": "verdict",
        "project": "bod80-memory-test",
        "session_id": "sess-1",
        "source_event_id": "src-evt-stable-1",
        "tenant": "default",
        "workspace": "default",
        "source_agent": "worker-a",
    }
    base.update(overrides)
    return EventIdentity(**base)


def _event(**overrides):
    base = {
        "kind": "after_edit",
        "content": "original source payload",
        "identity": _id(),
        "created_at": 1_700_000_000.0,
        "observed_at": 1_700_000_001.0,
    }
    base.update(overrides)
    return LifecycleEvent(**base)


def test_source_event_id_key_ignores_created_at_and_content():
    a = _event(created_at=1.0, content="alpha")
    b = _event(created_at=99.0, content="beta")  # different content + timestamp
    # Enqueue keys match (same source_event_id namespace)
    assert a.idempotency_key == b.idempotency_key
    # Content fingerprints differ
    assert a.digest() != b.digest()
    # Key is namespaced by project/workspace/harness
    other = _event(identity=_id(project="other-project"))
    assert other.idempotency_key != a.idempotency_key


def test_outbox_replay_preserves_original_created_at(tmp_path: Path):
    box = MemoryOutbox(tmp_path / "outbox.sqlite")
    first = _event(created_at=1_700_000_000.0, observed_at=1.0)
    row1 = box.enqueue(first)
    assert row1 is not None
    # Replay with rewritten clocks / observation time
    replay = _event(created_at=9_999.0, observed_at=9_999.0)
    row2 = box.enqueue(replay)
    assert row2 is not None
    assert row2.idempotency_key == row1.idempotency_key
    assert box.count("pending") == 1
    # Original source timestamp preserved in stored event
    assert row2.event.created_at == 1_700_000_000.0
    assert row2.created_at == row1.created_at


def test_outbox_conflict_on_same_source_id_different_payload(tmp_path: Path):
    box = MemoryOutbox(tmp_path / "outbox.sqlite")
    assert box.enqueue(_event(content="payload-a")) is not None
    with pytest.raises(IdempotencyConflictError) as ei:
        box.enqueue(_event(content="payload-b-different"))
    assert ei.value.source_event_id == "src-evt-stable-1"
    # Must not create a second row or silently replace
    assert box.count() == 1
    stored = box.get(_event().idempotency_key)
    assert stored is not None
    assert stored.event.content == "payload-a"


def test_outbox_replay_once_ack(tmp_path: Path):
    box = MemoryOutbox(tmp_path / "outbox.sqlite")
    e = _event()
    box.enqueue(e)
    due = box.due()
    assert len(due) == 1
    box.mark_acked(due[0], external_id="ext-1")
    assert box.count("pending") == 0
    assert box.count("acked") == 1
    # Replay after ack returns acked row; does not re-pend
    again = box.enqueue(_event(created_at=42.0, observed_at=99.0))
    assert again is not None
    assert again.state == "acked"
    assert box.count("pending") == 0


def test_without_source_event_id_falls_back_to_content_digest():
    e1 = _event(identity=_id(source_event_id=None), content="x", created_at=1.0)
    e2 = _event(identity=_id(source_event_id=None), content="x", created_at=2.0, observed_at=9.0)
    # Without source_event_id, created_at is part of content digest payload → different keys
    # (observation excluded, but created_at is source field when no source_event_id)
    assert e1.idempotency_key == e1.digest()
    assert e2.idempotency_key == e2.digest()
    assert e1.idempotency_key != e2.idempotency_key


def test_same_source_event_id_different_session_does_not_collide():
    a = _event(identity=_id(session_id="sess-a", source_event_id="same-id"))
    b = _event(identity=_id(session_id="sess-b", source_event_id="same-id"))
    assert a.idempotency_key != b.idempotency_key
