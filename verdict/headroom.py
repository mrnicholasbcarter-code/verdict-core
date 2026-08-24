"""Provider quota and headroom checks."""

__all__ = ['check_headroom']

from verdict.models import ProviderConfig


def check_headroom(model_id: str, provider_name: str, config: ProviderConfig) -> tuple[bool, float] | None:
    """Check if a model has capacity.

    Returns (is_available, headroom_pct) or None if headroom cannot be determined.
    If no headroom endpoint is configured, returns None (unknown/unavailable).
    """
    # When no headroom endpoint is configured, we cannot determine capacity.
    # Return None (unknown) instead of fabricating 100% headroom.
    if config.headroom_endpoint is None:
        return None

    return None
