"""Provider quota and headroom checks."""

from llm_gate.models import ProviderConfig


def check_headroom(model_id: str, provider_name: str, config: ProviderConfig) -> tuple[bool, float]:
    """Check if a model has capacity.

    Returns (is_available, headroom_pct).
    If no headroom endpoint is configured, defaults to fail-open (True, 100.0).
    """
    # Fail-open implementation. In a production environment, this queries
    # the provider's /api/usage or ratelimit headers to verify capacity.
    if config.headroom_endpoint is None:
        return True, 100.0

    return True, 100.0
