"""Compatibility entry point for canonical subscription-aware admission."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Any

from verdict.admission import AdmittedSet, RuntimeEvidence, admit


def admit_with_subscription(
    inventory_rows: Sequence[Mapping[str, Any]] | None,
    connections: Sequence[Mapping[str, Any]] | None,
    runtime: RuntimeEvidence | None,
    *,
    now: datetime,
    required_capabilities: frozenset[str] = frozenset(),
    min_context_tokens: int = 0,
) -> AdmittedSet:
    """Forward legacy callers to the canonical ``admit`` implementation.

    ``admit`` projects legacy account-scoped subscription budgets itself.  The
    wrapper intentionally does not mint snapshots or inspect private state.
    """
    return admit(
        inventory_rows,
        connections,
        runtime or RuntimeEvidence(),
        now=now.astimezone(timezone.utc),
        require_runtime=False,
        required_capabilities=required_capabilities,
        min_context_tokens=min_context_tokens,
    )


__all__ = ["admit_with_subscription"]
