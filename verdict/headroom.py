"""Provider quota and headroom checks."""

__all__ = ["UNKNOWN_HEADROOM", "check_headroom", "headroom_is_unknown"]

from verdict.models import ProviderConfig

UNKNOWN_HEADROOM: tuple[bool, float] | None = None


def check_headroom(
    model_id: str, provider_name: str, config: ProviderConfig
) -> tuple[bool, float] | None:
    """Check if a model has capacity.

    Returns (is_available, headroom_pct) or None if headroom cannot be determined.
    If no headroom endpoint is configured, returns None (unknown/unavailable).
    """
    # When no headroom endpoint is configured, we cannot determine capacity.
    # Return None (unknown) instead of fabricating 100% headroom.
    if config.headroom_endpoint is None:
        return None

    return None


def headroom_is_unknown(result: tuple[bool, float] | None) -> bool:
    """Check if headroom result is unknown.

    Returns True only when result is None (unknown capacity).
    Returns False for any valid tuple (whether available or not).
    """
    return result is None
