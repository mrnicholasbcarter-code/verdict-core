"""Outbox drain: outage retry, restart, replay-once ack, dead-letter."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from verdict_memory.contracts import (
    CertificationState,
    EventIdentity,
    ExternalMemoryRef,
    LifecycleEvent,
    MemoryProviderError,
    ProviderErrorCode,
    ProviderHealth,
    ProviderResultStatus,
    RecallQuery,
    RecallResult,
)
from verdict_memory.fake import FakeMemoryProvider
from verdict_memory.mirror import MemoryMirrorWorker
from verdict_memory.outbox import MemoryOutbox


class _Clock:
    def __init__(self, value: float = 1_700_000_000.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


@dataclass
class _ScriptedProvider:
    provider_id: str = "scripted"
    protocol_version: str = "1"
    fail_with: ProviderErrorCode | None = None
    puts: list[LifecycleEvent] = field(default_factory=list)
    store: dict[str, LifecycleEvent] = field(default_factory=dict)

    def health(self) -> ProviderHealth:
        return ProviderHealth(
            self.provider_id,
            ProviderResultStatus.AVAILABLE,
            CertificationState.HEALTHY,
            self.protocol_version,
        )

    def put(self, event: LifecycleEvent) -> ExternalMemoryRef:
        self.puts.append(event)
        if self.fail_with is not None:
            raise MemoryProviderError(self.fail_with, f"forced:{self.fail_with.value}")
        self.store.setdefault(event.idempotency_key, event)
        return ExternalMemoryRef(self.provider_id, event.idempotency_key, event.revision)

    def search(self, query: RecallQuery) -> RecallResult:
        return RecallResult(ProviderResultStatus.AVAILABLE, ())

    def delete(self, ref: ExternalMemoryRef) -> bool:
        return self.store.pop(ref.external_id, None) is not None


def _event(**overrides):
    base = {
        "kind": "manual_capture",
        "content": "drain payload",
        "identity": EventIdentity(
            source_harness="verdict",
            project="bod80-memory-test",
            session_id="s1",
            source_event_id="drain-evt-1",
            workspace="default",
        ),
        "created_at": 1_700_000_000.0,
    }
    base.update(overrides)
    return LifecycleEvent(**base)


def test_drain_acks_on_success(tmp_path: Path):
    clock = _Clock()
    box = MemoryOutbox(tmp_path / "o.sqlite", clock=clock)
    provider = FakeMemoryProvider()
    box.enqueue(_event())
    worker = MemoryMirrorWorker(box, provider, clock=clock)
    result = worker.run_once()
    assert result == result.__class__(1, 1, 0, 0)
    assert box.count("acked") == 1
    assert box.count("pending") == 0


def test_outage_retries_then_acks_after_recovery(tmp_path: Path):
    clock = _Clock()
    box = MemoryOutbox(tmp_path / "o.sqlite", clock=clock)
    provider = _ScriptedProvider(fail_with=ProviderErrorCode.UNREACHABLE)
    box.enqueue(_event())
    worker = MemoryMirrorWorker(box, provider, clock=clock, base_delay_seconds=1.0)
    r1 = worker.run_once()
    assert r1.retried == 1 and r1.acked == 0
    assert box.count("pending") == 1
    # Not due until backoff elapses
    assert box.due(now=clock()) == ()
    clock.advance(1.0)
    provider.fail_with = None  # recovery
    r2 = worker.run_once()
    assert r2.acked == 1
    assert box.count("acked") == 1
    # Replay-once: drain again does nothing
    r3 = worker.run_once()
    assert r3.processed == 0
    assert len(provider.puts) == 2  # fail attempt + success


def test_restart_preserves_pending_and_acks_once(tmp_path: Path):
    clock = _Clock()
    path = tmp_path / "o.sqlite"
    box = MemoryOutbox(path, clock=clock)
    box.enqueue(_event())
    # "Restart": new outbox + worker on same sqlite
    box2 = MemoryOutbox(path, clock=clock)
    provider = FakeMemoryProvider()
    worker = MemoryMirrorWorker(box2, provider, clock=clock)
    assert box2.count("pending") == 1
    assert worker.run_once().acked == 1
    assert box2.count("acked") == 1
    # Second worker instance must not re-put
    worker2 = MemoryMirrorWorker(MemoryOutbox(path, clock=clock), provider, clock=clock)
    assert worker2.run_once().processed == 0


def test_auth_failure_dead_letters(tmp_path: Path):
    clock = _Clock()
    box = MemoryOutbox(tmp_path / "o.sqlite", clock=clock)
    provider = _ScriptedProvider(fail_with=ProviderErrorCode.AUTH_FAILED)
    box.enqueue(_event())
    worker = MemoryMirrorWorker(box, provider, clock=clock)
    result = worker.run_once()
    assert result.dead_lettered == 1
    assert box.count("dead_letter") == 1
    assert box.count("pending") == 0
