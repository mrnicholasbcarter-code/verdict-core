"""Capacity evidence adapter protocol and registry (BOD-129).

Adapters are evidence producers only. Adding a provider/gateway registers a
plugin — it does not require a central brand switch or a second router.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from verdict.capacity_models import (
    CapacityEvidenceError,
    CapacitySignal,
    CapacitySnapshot,
    ConnectionIdentity,
    DiagnoseReport,
    RefreshPolicy,
    _non_empty,
)


@runtime_checkable
class CapacityAdapter(Protocol):
    """Provider/gateway-neutral capacity observation surface."""

    @property
    def adapter_id(self) -> str: ...

    def discover(self) -> Sequence[ConnectionIdentity]:
        """Return identities this adapter can observe without exposing secrets."""

    def capabilities(self) -> Mapping[CapacitySignal, bool]:
        """Which capacity/balance/reset signals this adapter can observe."""

    def observe_capacity(
        self, identity: ConnectionIdentity | None = None, *, now: datetime | None = None
    ) -> CapacitySnapshot:
        """Return one normalized CapacitySnapshot (unknown remains unknown)."""

    def refresh_policy(self) -> RefreshPolicy:
        """TTL/reset/backoff hints where supported."""

    def diagnose(self) -> DiagnoseReport:
        """Explain unavailable/auth-expired/unsupported state."""


@dataclass
class CapacityAdapterRegistry:
    """Plugin registry — no provider-specific branching in consumers."""

    _adapters: dict[str, CapacityAdapter]

    def __init__(self) -> None:
        self._adapters = {}

    def register(self, adapter: CapacityAdapter) -> None:
        adapter_id = _non_empty("adapter_id", adapter.adapter_id)
        if adapter_id in self._adapters:
            raise CapacityEvidenceError(f"adapter already registered: {adapter_id}")
        self._adapters[adapter_id] = adapter

    def get(self, adapter_id: str) -> CapacityAdapter:
        try:
            return self._adapters[_non_empty("adapter_id", adapter_id)]
        except KeyError as exc:
            raise CapacityEvidenceError(f"unknown adapter: {adapter_id}") from exc

    def list_adapters(self) -> tuple[str, ...]:
        return tuple(sorted(self._adapters))

    def discover(self) -> tuple[ConnectionIdentity, ...]:
        found: list[ConnectionIdentity] = []
        for adapter_id in self.list_adapters():
            found.extend(self._adapters[adapter_id].discover())
        return tuple(found)

    def capabilities(self, adapter_id: str) -> Mapping[CapacitySignal, bool]:
        return self.get(adapter_id).capabilities()

    def observe_capacity(
        self,
        adapter_id: str,
        identity: ConnectionIdentity | None = None,
        *,
        now: datetime | None = None,
    ) -> CapacitySnapshot:
        return self.get(adapter_id).observe_capacity(identity, now=now)

    def observe_all(self, *, now: datetime | None = None) -> tuple[CapacitySnapshot, ...]:
        snapshots: list[CapacitySnapshot] = []
        for adapter_id in self.list_adapters():
            adapter = self._adapters[adapter_id]
            for identity in adapter.discover():
                snapshots.append(adapter.observe_capacity(identity, now=now))
        return tuple(snapshots)

    def refresh_policy(self, adapter_id: str) -> RefreshPolicy:
        return self.get(adapter_id).refresh_policy()

    def diagnose(self, adapter_id: str) -> DiagnoseReport:
        return self.get(adapter_id).diagnose()

    def diagnose_all(self) -> tuple[DiagnoseReport, ...]:
        return tuple(self._adapters[adapter_id].diagnose() for adapter_id in self.list_adapters())


def default_registry(
    adapters: Iterable[CapacityAdapter] | None = None, *, include_local: bool = False
) -> CapacityAdapterRegistry:
    """Build a registry; callers supply adapters (OmniRoute optional).

    By default the registry stays empty so fixture tests and callers remain
    explicit. Pass ``include_local=True`` to auto-wire ``discover_local_adapters()``
    (evidence only — never routing).
    """

    registry = CapacityAdapterRegistry()
    selected: Iterable[CapacityAdapter]
    if adapters is not None:
        selected = adapters
    elif include_local:
        selected = discover_local_adapters()
    else:
        selected = ()
    for adapter in selected:
        registry.register(adapter)
    return registry


def discover_local_adapters(**kwargs: Any) -> tuple[CapacityAdapter, ...]:
    """Discover live local capacity evidence adapters (BOD-129 follow-up)."""

    from verdict.capacity_live import discover_local_adapters as _discover

    return _discover(**kwargs)


__all__ = [
    "CapacityAdapter",
    "CapacityAdapterRegistry",
    "default_registry",
    "discover_local_adapters",
]
