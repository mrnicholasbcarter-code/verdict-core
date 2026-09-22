"""Acceptance tests for durable MemoryPlane outbox mirroring (BOD-146)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from verdict.memory_capture_policy import MemoryCapturePolicy
from verdict.memory_mirror import MemoryMirrorWorker
from verdict.memory_outbox import MemoryOutbox
from verdict.memory_plane import MemoryPlane, MemoryRecord
from verdict.shared_memory import (
    CertificationState,
    ExternalMemoryRef,
    FakeSharedMemoryProvider,
    ProviderErrorCode,
    ProviderHealth,
    ProviderResultStatus,
    SharedMemoryEnvelope,
    SharedMemoryProviderError,
    SharedMemoryQuery,
    SharedMemorySearchResult,
)


class _Clock:
    def __init__(self, value: float = 1_700_000_000.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


@dataclass
class _ScriptedProvider:
    """Deterministic provider that can fail then recover."""

    provider_id: str = "scripted-shared-memory"
    protocol_version: str = "1"
    fail_with: ProviderErrorCode | None = None
    puts: list[SharedMemoryEnvelope] = field(default_factory=list)
    store: dict[str, SharedMemoryEnvelope] = field(default_factory=dict)

    def health(self) -> ProviderHealth:
        return ProviderHealth(
            self.provider_id,
            ProviderResultStatus.AVAILABLE,
            CertificationState.HEALTHY,
            self.protocol_version,
        )

    def put(self, envelope: SharedMemoryEnvelope) -> ExternalMemoryRef:
        self.puts.append(envelope)
        if self.fail_with is not None:
            raise SharedMemoryProviderError(self.fail_with, f"forced:{self.fail_with.value}")
        self.store.setdefault(envelope.idempotency_key, envelope)
        return ExternalMemoryRef(self.provider_id, envelope.idempotency_key, envelope.revision)

    def search(self, query: SharedMemoryQuery) -> SharedMemorySearchResult:
        return SharedMemorySearchResult(ProviderResultStatus.AVAILABLE)

    def delete(self, ref: ExternalMemoryRef) -> bool:
        return self.store.pop(ref.external_id, None) is not None


def _record(**overrides: Any) -> MemoryRecord:
    base = {
        "record_id": "r1",
        "namespace": "docs",
        "key": "note",
        "content": "routing budget decision for free models",
        "source": "test",
        "scope": "default",
        "created_at": 1_700_000_000.0,
        "updated_at": 1_700_000_000.0,
        "confidence": 0.9,
    }
    base.update(overrides)
    return MemoryRecord(**base)


def test_local_write_succeeds_when_provider_offline(tmp_path: Path) -> None:
    db = tmp_path / "memory.db"
    clock = _Clock()
    outbox = MemoryOutbox(db, project="verdict", clock=clock)
    offline = FakeSharedMemoryProvider(status=ProviderResultStatus.UNAVAILABLE)
    with MemoryPlane(db, outbox=outbox) as plane:
        stored = plane.put(_record())
        assert stored.content.startswith("routing budget")
        assert plane.get("docs", "note") is not None
    assert outbox.count("pending") == 1
    result = MemoryMirrorWorker(outbox, offline, clock=clock).run_once()
    assert result.retried == 1
    assert outbox.count("pending") == 1
    due = outbox.due(limit=1, now=clock() + 10_000)
    assert len(due) == 1
    assert outbox.get(due[0].idempotency_key) is not None


def test_mirror_survives_restart_exactly_once(tmp_path: Path) -> None:
    db = tmp_path / "memory.db"
    clock = _Clock()
    outbox = MemoryOutbox(db, project="verdict", clock=clock)
    provider = _ScriptedProvider(fail_with=ProviderErrorCode.UNREACHABLE)
    with MemoryPlane(db, outbox=outbox) as plane:
        plane.put(_record(content="exactly-once payload"))
    MemoryMirrorWorker(outbox, provider, base_delay_seconds=1.0, clock=clock).run_once()
    assert outbox.count("pending") == 1
    assert len(provider.puts) == 1

    # Process restart: new outbox/worker handles, same SQLite file.
    clock.advance(2.0)
    outbox2 = MemoryOutbox(db, project="verdict", clock=clock)
    provider.fail_with = None
    MemoryMirrorWorker(outbox2, provider, base_delay_seconds=1.0, clock=clock).run_once()
    assert outbox2.count("acked") == 1
    assert outbox2.count("pending") == 0
    # Two physical puts, one logical key.
    assert len(provider.puts) == 2
    assert provider.puts[0].idempotency_key == provider.puts[1].idempotency_key
    assert len(provider.store) == 1


def test_idempotent_replay(tmp_path: Path) -> None:
    db = tmp_path / "memory.db"
    outbox = MemoryOutbox(db, project="verdict")
    provider = FakeSharedMemoryProvider()
    with MemoryPlane(db, outbox=outbox) as plane:
        plane.put(_record(content="duplicate-safe note"))
        plane.put(_record(content="duplicate-safe note"))  # same record_id + hash
    assert outbox.count("pending") == 1
    worker = MemoryMirrorWorker(outbox, provider)
    first = worker.run_once()
    second = worker.run_once()
    assert first.acked == 1
    assert second.processed == 0
    event = outbox.get(
        provider.search(SharedMemoryQuery("duplicate", project="verdict")).hits[0].ref.external_id
    )
    assert event is not None and event.state == "acked"


def test_auth_schema_dead_letter(tmp_path: Path) -> None:
    db = tmp_path / "memory.db"
    clock = _Clock()
    outbox = MemoryOutbox(db, project="verdict", clock=clock)
    with MemoryPlane(db, outbox=outbox) as plane:
        plane.put(_record(record_id="auth", content="auth failure payload"))
        plane.put(_record(record_id="schema", key="schema", content="schema failure payload"))
    auth_provider = _ScriptedProvider(fail_with=ProviderErrorCode.AUTH_FAILED)
    schema_provider = _ScriptedProvider(fail_with=ProviderErrorCode.SCHEMA_INCOMPATIBLE)
    MemoryMirrorWorker(outbox, auth_provider, clock=clock).run_once(limit=1)
    MemoryMirrorWorker(outbox, schema_provider, clock=clock).run_once(limit=1)
    assert outbox.count("dead_letter") == 2
    assert outbox.count("pending") == 0
    # Advancing time does not resurrect dead letters into hot retries.
    clock.advance(10_000)
    assert outbox.due(now=clock()) == ()


def test_secret_filtering_before_mirror(tmp_path: Path) -> None:
    db = tmp_path / "memory.db"
    outbox = MemoryOutbox(db, project="verdict")
    provider = FakeSharedMemoryProvider()
    with MemoryPlane(db, outbox=outbox) as plane:
        plane.put(_record(content="api_key=super-secret-value-123456"))
        plane.put(_record(record_id="r2", key="sens", content="ok", sensitivity="secret"))
    assert outbox.count() == 0
    assert MemoryMirrorWorker(outbox, provider).run_once().processed == 0
    decision = MemoryCapturePolicy().evaluate(
        _record(content="password: hunter2-should-not-mirror")
    )
    assert decision.eligible is False
    assert decision.reason == "secret_detected"


def test_nonstandard_sensitivity_denied(tmp_path: Path) -> None:
    """ADR-034: only sensitivity=standard is eligible for remote mirror."""
    db = tmp_path / "memory.db"
    outbox = MemoryOutbox(db, project="verdict")
    with MemoryPlane(db, outbox=outbox) as plane:
        plane.put(_record(record_id="r-conf", key="conf", content="ok", sensitivity="confidential"))
        plane.put(_record(record_id="r-high", key="high", content="ok", sensitivity="high"))
        plane.put(_record(record_id="r-int", key="int", content="ok", sensitivity="internal"))
    assert outbox.count() == 0
    for sens in ("confidential", "high", "internal", "private", "restricted"):
        decision = MemoryCapturePolicy().evaluate(_record(content="ok", sensitivity=sens))
        assert decision.eligible is False, sens
        assert decision.reason == "sensitivity_denied", sens
    # Standard remains eligible.
    ok = MemoryCapturePolicy().evaluate(_record(content="ok", sensitivity="standard"))
    assert ok.eligible is True
    assert ok.reason == "eligible"


def test_capture_policy_skips_tool_noise(tmp_path: Path) -> None:
    db = tmp_path / "memory.db"
    outbox = MemoryOutbox(db, project="verdict")
    with MemoryPlane(db, outbox=outbox) as plane:
        plane.put(
            _record(
                record_id="tool1",
                namespace="tool",
                key="call",
                content="tool stdout noise about ls -la",
                source="tool-output",
            )
        )
        plane.put(_record(record_id="keep", content="architecture decision about routing"))
    assert outbox.count("pending") == 1
    pending = outbox.due(limit=10)
    assert len(pending) == 1
    assert "architecture decision" in pending[0].envelope.content


def test_memory_plane_usable_without_provider(tmp_path: Path) -> None:
    db = tmp_path / "memory.db"
    with MemoryPlane(db) as plane:
        stored = plane.put(_record(content="local only authority"))
        assert plane.get("docs", "note") == stored
        assert plane.search("authority")[0].record_id == "r1"
