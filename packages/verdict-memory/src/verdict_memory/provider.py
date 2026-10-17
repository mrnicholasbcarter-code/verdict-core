"""Replaceable MemoryProvider protocol. Implementations require no LLM."""

from __future__ import annotations

from typing import Protocol

from verdict_memory.contracts import (
    ExternalMemoryRef,
    LifecycleEvent,
    ProviderHealth,
    RecallQuery,
    RecallResult,
)


class MemoryProvider(Protocol):
    provider_id: str
    protocol_version: str

    def health(self) -> ProviderHealth: ...

    def put(self, event: LifecycleEvent) -> ExternalMemoryRef: ...

    def search(self, query: RecallQuery) -> RecallResult: ...

    def delete(self, ref: ExternalMemoryRef) -> bool: ...


__all__ = ["MemoryProvider"]
