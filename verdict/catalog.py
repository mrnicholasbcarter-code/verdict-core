"""Normalize and locally filter an OpenAI-compatible model catalog."""

from __future__ import annotations


def _model_set(value: str | None) -> frozenset[str]:
    if not value:
        return frozenset()
    return frozenset(item.strip() for item in value.split(",") if item.strip())


def configured_catalog_filters(
    allowlist_value: str | None, denylist_value: str | None
) -> tuple[frozenset[str], frozenset[str]]:
    """Parse comma-separated local model policy values."""
    return _model_set(allowlist_value), _model_set(denylist_value)
