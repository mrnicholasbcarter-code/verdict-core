"""Contract tests for lifecycle-event/v1 + FakeProvider + outbox dedupe + compat policy."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from verdict_memory import (
    ALLOWED_EVENT_KINDS,
    CANONICAL_EVENT_KINDS,
    CONTRACT_API_VERSION,
    EVENT_KIND_ALIASES,
    LIFECYCLE_EVENT_SCHEMA,
    FakeMemoryProvider,
    LifecycleEvent,
    ProviderResultStatus,
    RecallQuery,
    canonicalize,
    redact_secrets,
    source_idempotency_key,
)
from verdict_memory.contracts import EventIdentity
from verdict_memory.outbox import MemoryOutbox
from verdict_memory.policy import MemoryCapturePolicy

VECTORS = Path(__file__).resolve().parents[1] / "vectors"


def _event(**overrides):
    base = {
        "kind": "manual_capture",
        "content": "shared note about routing budgets",
        "identity": EventIdentity(
            source_harness="verdict",
            project="verdict",
            session_id="sess-1",
            parent_session_id=None,
            attempt_id="attempt-1",
            source_event_id="src-evt-0001",
            tenant="default",
            workspace="default",
            source_agent="worker-a",
        ),
        "created_at": 1_700_000_000.0,
        "observed_at": 1_700_000_001.0,
        "scope": "default",
        "metadata": {"extension": {"k": 1}},
    }
    base.update(overrides)
    return LifecycleEvent(**base)


def test_api_version_experimental():
    assert CONTRACT_API_VERSION == "0.1.0a1"
    assert LIFECYCLE_EVENT_SCHEMA == "lifecycle-event/v1"
    for kind in (
        "session_start",
        "before_first_model_turn",
        "before_tool",
        "after_tool",
        "after_edit",
        "subagent_start",
        "subagent_end",
        "before_compact",
        "before_yield",
        "verification_result",
        "session_end",
        "resume",
    ):
        assert kind in CANONICAL_EVENT_KINDS
        assert kind in ALLOWED_EVENT_KINDS


def test_alias_normalization():
    assert EVENT_KIND_ALIASES["before_turn"] == "before_first_model_turn"
    assert EVENT_KIND_ALIASES["compaction"] == "before_compact"
    e = _event(kind="before_turn")
    assert e.kind == "before_first_model_turn"
    e2 = _event(kind="compaction")
    assert e2.kind == "before_compact"


def test_unsupported_kind_raises():
    with pytest.raises(ValueError, match="unsupported event kind"):
        _event(kind="not_a_real_kind")  # type: ignore[arg-type]


def test_unknown_optional_fields_ignored():
    raw = _event().to_dict()
    raw["future_optional_field"] = {"x": 1}
    raw["identity"] = dict(raw["identity"])
    raw["identity"]["future_identity_field"] = "nope"
    loaded = LifecycleEvent.from_dict(raw)
    assert loaded.digest() == _event().digest()
    assert "future_optional_field" not in loaded.to_dict()


def test_event_roundtrip_and_digest_excludes_observed_at():
    first = _event(metadata={"b": 2, "a": 1})
    second = LifecycleEvent.from_dict(first.to_dict())
    assert second.to_dict() == first.to_dict()
    twin = _event(metadata={"a": 1, "b": 2}, observed_at=9_999.0)
    assert twin.digest() == first.digest()
    assert twin.idempotency_key == first.idempotency_key
    assert twin.idempotency_key == source_idempotency_key(
        schema_version=first.schema_version,
        protocol_version=first.protocol_version,
        source_harness=first.identity.source_harness,
        project=first.identity.project,
        workspace=first.identity.workspace,
        tenant=first.identity.tenant,
        session_id=first.identity.session_id,
        source_event_id=str(first.identity.source_event_id),
    )


def test_stable_source_event_id_across_replay_timestamps():
    a = _event(
        created_at=1_700_000_000.0,
        observed_at=1.0,
        identity=EventIdentity(
            source_harness="verdict",
            project="verdict",
            session_id="s",
            source_event_id="stable-1",
        ),
    )
    b = _event(
        created_at=9_999.0,
        observed_at=99.0,
        identity=EventIdentity(
            source_harness="verdict",
            project="verdict",
            session_id="s",
            source_event_id="stable-1",
        ),
    )
    # Enqueue identity stable across created_at rewrite; content fingerprint differs
    assert a.idempotency_key == b.idempotency_key
    assert a.digest() != b.digest()


def test_canonical_golden_vectors():
    for name in (
        "lifecycle_event_v1_minimal.json",
        "lifecycle_event_v1_unicode_optional.json",
        "lifecycle_event_v1_numeric.json",
    ):
        vector_path = VECTORS / name
        assert vector_path.is_file(), name
        raw = json.loads(vector_path.read_text())
        event = LifecycleEvent.from_dict(raw["event"])
        assert event.digest() == raw["digest"], name
        if "canonical_json" in raw and name.endswith("minimal.json"):
            assert canonicalize(event.to_dict()) == raw["canonical_json"]
        if name.endswith("numeric.json"):
            canon = canonicalize(event.to_dict())
            for needle in raw["assert_substring"]:
                assert needle in canon


def test_fake_provider_scope_filter_and_healthy_empty():
    provider = FakeMemoryProvider()
    kept = provider.put(
        _event(
            content="alpha budget note",
            identity=EventIdentity(
                source_harness="verdict",
                project="verdict",
                session_id="s1",
            ),
            scope="alpha",
        )
    )
    provider.put(
        _event(
            content="beta budget note",
            identity=EventIdentity(
                source_harness="verdict",
                project="verdict",
                session_id="s2",
            ),
            scope="beta",
        )
    )
    provider.put(
        _event(
            content="other project",
            identity=EventIdentity(
                source_harness="verdict",
                project="other",
                session_id="s3",
            ),
            scope="alpha",
        )
    )

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


def test_redact_secrets_before_persist_semantics():
    text = "Authorization: Bearer canary-secret-TOKEN-9f3c2b1a"
    assert "canary-secret" not in (redact_secrets(text) or "")


def test_capture_policy_blocks_secrets_and_noise():
    policy = MemoryCapturePolicy()
    secret = policy.evaluate(_event(content="api_key=sk-abcdefghijklmnopqrstuv"))
    assert secret.eligible is False
    assert secret.reason == "secret_detected"
    noise = policy.evaluate(_event(kind="after_tool", memory_kind="tool", content="tool stdout"))
    assert noise.eligible is False
    assert noise.reason == "low_value_tool_noise"
    ok = policy.evaluate(_event())
    assert ok.eligible is True


def test_outbox_dedupe_same_logical_source_once(tmp_path: Path):
    box = MemoryOutbox(tmp_path / "outbox.sqlite")
    e1 = _event(observed_at=1.0)
    e2 = _event(observed_at=2.0)
    assert e1.digest() == e2.digest()
    first = box.enqueue(e1)
    second = box.enqueue(e2)
    assert first is not None and second is not None
    assert first.idempotency_key == second.idempotency_key
    assert box.count("pending") == 1
    due = box.due()
    assert len(due) == 1
    box.mark_acked(due[0], external_id="ext-1")
    assert box.count("pending") == 0
    assert box.count("acked") == 1
    third = box.enqueue(e1)
    assert third is not None
    assert third.state == "acked"
    assert box.count("pending") == 0


def test_outbox_rejects_ineligible(tmp_path: Path):
    box = MemoryOutbox(tmp_path / "outbox.sqlite")
    assert box.enqueue(_event(content="password=hunter2")) is None
    assert box.count() == 0
