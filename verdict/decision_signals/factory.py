"""Decision-signal provider factory (TYPESAFE credentials migration).

Returns a provider when VERDICT_DECISION_SIGNALS_MODE is SHADOW or ADVISORY
and TYPESAFE_API_KEY is present; returns None otherwise.

This is the only place that reads env vars and constructs the provider for
production callers.  Explicit injection always wins over the factory default.
"""

from __future__ import annotations

import os

from verdict.decision_signals.contracts import DecisionSignalProvider
from verdict.decision_signals.shadow import should_collect_signals


def provider_from_env() -> DecisionSignalProvider | None:
    """Return a configured OpenJevSystemOneProvider when all conditions hold:

    1. VERDICT_DECISION_SIGNALS_MODE is SHADOW or ADVISORY (not OFF).
    2. TYPESAFE_API_KEY is non-empty.

    Returns None otherwise (silently; callers MUST treat None as "no provider").
    The TYPESAFE_BASE_URL env var is consumed by the provider itself.
    """
    if not should_collect_signals():
        return None

    api_key = os.environ.get("TYPESAFE_API_KEY", "").strip()
    if not api_key:
        return None

    # Import here to avoid circular imports at module load.
    from verdict.decision_signals.openjev import OpenJevSystemOneProvider

    return OpenJevSystemOneProvider()


__all__ = ["provider_from_env"]
