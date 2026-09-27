"""Compatibility wrapper for canonical subscription-aware admission.

Subscription evidence is owned by :func:`verdict.admission.admit`; this module
keeps the historical import path without maintaining a second admission path.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from typing import Any

from verdict.admission import AdmittedSet, RuntimeEvidence, admit
from verdict.capacity_models import CapacitySnapshot


def admit_with_subscription_snapshots(
    inventory_rows: Sequence[Mapping[str, Any]] | None,
    connections: Sequence[Mapping[str, Any]] | None,
    runtime: RuntimeEvidence | None,
    *,
    now: datetime,
    subscription_snapshots: Sequence[CapacitySnapshot] | None = None,
    required_capabilities: frozenset[str] = frozenset(),
    min_context_tokens: int = 0,
    deny: Callable[[str], str | None] | None = None,
) -> AdmittedSet:
    """Forward to canonical :func:`verdict.admission.admit`.

    The explicit name makes snapshot-aware callers clear while ensuring there
    is no compatibility-specific authority or construction of ``AdmittedSet``.
    """
    return admit(
        inventory_rows,
        connections,
        runtime,
        now=now,
        required_capabilities=required_capabilities,
        min_context_tokens=min_context_tokens,
        deny=deny,
        subscription_snapshots=subscription_snapshots,
    )


__all__ = ["admit_with_subscription_snapshots"]
