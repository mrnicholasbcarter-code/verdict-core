"""Provider quota and headroom checks."""

__all__ = [
    "UNKNOWN_HEADROOM",
    "Affordability",
    "affordability",
    "check_headroom",
    "headroom_is_unknown",
]

from dataclasses import dataclass

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


@dataclass(frozen=True)
class Affordability:
    """Whether observed capacity can finish a unit of the estimated size (FR-029)."""

    admitted: bool
    state: str
    reason: str


def affordability(*, estimated_tokens: int | None, remaining_tokens: int | None) -> Affordability:
    """Admission floor: exclude only on observed capacity below the estimated unit cost.

    Unobserved capacity, or an unestimated unit, yields ``UNKNOWN`` and never excludes —
    the gateway exposing no quota is the normal case, and inventing a number to exclude on
    would violate the observed-only rule (FR-015).
    """
    if estimated_tokens is None or remaining_tokens is None:
        return Affordability(True, "UNKNOWN", "no observed capacity or no cost estimate")
    if remaining_tokens < estimated_tokens:
        return Affordability(
            False,
            "insufficient",
            f"observed remaining {remaining_tokens} tokens "
            f"cannot complete estimated {estimated_tokens}",
        )
    return Affordability(
        True, "sufficient", f"observed remaining {remaining_tokens} >= estimated {estimated_tokens}"
    )


def headroom_is_unknown(result: tuple[bool, float] | None) -> bool:
    """Check if headroom result is unknown.

    Returns True only when result is None (unknown capacity).
    Returns False for any valid tuple (whether available or not).
    """
    return result is None
