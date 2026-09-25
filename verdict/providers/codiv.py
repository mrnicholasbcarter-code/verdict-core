"""Codiv provider identification (BOD-198)."""

from __future__ import annotations

from collections.abc import Sequence

from verdict.gateway_adapter_runtime import AdapterRouteIdentity


def codiv_routes(
    routes: Sequence[AdapterRouteIdentity], *, provider_prefix: str = "codiv"
) -> tuple[AdapterRouteIdentity, ...]:
    """Filter routes to only those matching the Codiv provider prefix exactly.

    Args:
        routes: Sequence of route identities from adapter discovery
        provider_prefix: Exact provider prefix to match (default: "codiv")

    Returns:
        Tuple of routes where route.provider matches provider_prefix exactly
        (case-sensitive). Same model name under a different provider is excluded.

    Example:
        >>> routes = [
        ...     AdapterRouteIdentity(..., provider="codiv", model_id="diffusiongemma-26b"),
        ...     AdapterRouteIdentity(..., provider="nvidia", model_id="diffusiongemma-26b"),
        ... ]
        >>> codiv_routes(routes, provider_prefix="codiv")
        # Returns only the first route
    """
    return tuple(route for route in routes if route.provider == provider_prefix)
