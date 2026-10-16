"""Bounded outbox drain worker (operational replay, not a retrieval store)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from verdict_memory.contracts import MemoryProviderError, ProviderErrorCode
from verdict_memory.outbox import MemoryOutbox, OutboxEvent
from verdict_memory.provider import MemoryProvider

# Reused from existing Verdict memory_mirror bounds (not a new SLA invention).
DEFAULT_BASE_DELAY_SECONDS = 1.0
DEFAULT_MAX_DELAY_SECONDS = 300.0

_DEAD_LETTER_CODES = frozenset(
    {
        ProviderErrorCode.AUTH_FAILED,
        ProviderErrorCode.SCHEMA_INCOMPATIBLE,
        ProviderErrorCode.PROTOCOL_INCOMPATIBLE,
        ProviderErrorCode.INVALID_REQUEST,
        ProviderErrorCode.NOT_CONFIGURED,
        ProviderErrorCode.CONFLICT,
    }
)


class _Clock(Protocol):
    def __call__(self) -> float: ...


@dataclass(frozen=True)
class MirrorBatchResult:
    processed: int
    acked: int
    retried: int
    dead_lettered: int


class MemoryMirrorWorker:
    """Drain due outbox events through a MemoryProvider (put + ack)."""

    def __init__(
        self,
        outbox: MemoryOutbox,
        provider: MemoryProvider,
        *,
        base_delay_seconds: float = DEFAULT_BASE_DELAY_SECONDS,
        max_delay_seconds: float = DEFAULT_MAX_DELAY_SECONDS,
        clock: _Clock | None = None,
    ) -> None:
        if base_delay_seconds <= 0 or max_delay_seconds < base_delay_seconds:
            raise ValueError("invalid backoff configuration")
        self.outbox = outbox
        self.provider = provider
        self.base_delay_seconds = base_delay_seconds
        self.max_delay_seconds = max_delay_seconds
        self._clock = clock

    def run_once(self, *, limit: int = 20) -> MirrorBatchResult:
        events = self.outbox.due(limit=limit, now=self._now())
        acked = retried = dead = 0
        for event in events:
            outcome = self._process(event)
            if outcome == "acked":
                acked += 1
            elif outcome == "retry":
                retried += 1
            else:
                dead += 1
        return MirrorBatchResult(len(events), acked, retried, dead)

    def _process(self, event: OutboxEvent) -> str:
        try:
            ref = self.provider.put(event.event)
        except MemoryProviderError as exc:
            return self._fail(event, exc.code, exc.safe_message)
        except Exception as exc:  # noqa: BLE001 — map unknown to typed retry
            return self._fail(event, ProviderErrorCode.UNKNOWN, type(exc).__name__)
        self.outbox.mark_acked(event, ref.external_id)
        return "acked"

    def _fail(self, event: OutboxEvent, code: ProviderErrorCode, message: str) -> str:
        if code in _DEAD_LETTER_CODES:
            self.outbox.mark_dead_letter(event, error_code=code.value, safe_error=message)
            return "dead"
        delay = min(
            self.max_delay_seconds, self.base_delay_seconds * (2 ** min(event.attempts, 32))
        )
        self.outbox.mark_retry(
            event, error_code=code.value, safe_error=message, delay_seconds=delay
        )
        return "retry"

    def _now(self) -> float:
        if self._clock is None:
            import time

            return time.time()
        return float(self._clock())


__all__ = [
    "DEFAULT_BASE_DELAY_SECONDS",
    "DEFAULT_MAX_DELAY_SECONDS",
    "MemoryMirrorWorker",
    "MirrorBatchResult",
]
