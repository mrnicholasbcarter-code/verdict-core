"""Normalize and locally filter an OpenAI-compatible model catalog."""

from __future__ import annotations

import json
from typing import Any


def _model_set(value: str | None) -> frozenset[str]:
    if not value:
        return frozenset()
    return frozenset(item.strip() for item in value.split(",") if item.strip())


def normalize_catalog(
    body: bytes, *, allowlist: frozenset[str] = frozenset(), denylist: frozenset[str] = frozenset()
) -> bytes:
    """Return a filtered catalog with conservative availability metadata.

    A catalog row proves only that the upstream listed an identifier. The
    ``availability_state`` is therefore intentionally ``unknown`` until a
    bounded health/headroom adapter establishes stronger evidence.
    """
    try:
        document = json.loads(body)
    except (TypeError, ValueError, json.JSONDecodeError):
        return body
    if not isinstance(document, dict) or not isinstance(document.get("data"), list):
        return body

    rows: list[dict[str, Any]] = []
    for row in document["data"]:
        if not isinstance(row, dict):
            continue
        model_id = row.get("id")
        if not isinstance(model_id, str) or not model_id:
            continue
        if allowlist and model_id not in allowlist:
            continue
        if model_id in denylist:
            continue
        normalized = dict(row)

        capabilities = row.get("capabilities", {})
        if not isinstance(capabilities, dict):
            capabilities = {}
        provider_name = row.get("owned_by")
        if not isinstance(provider_name, str) or not provider_name:
            provider_name = None

        normalized["verdict"] = {
            "eligible": True,
            "availability_state": "unknown",
            "capability_profile": {
                "tier": None,
                "context": capabilities.get("context"),
                "tools": capabilities.get("tools"),
                "structured_output": capabilities.get("structured_output"),
                "vision": capabilities.get("vision"),
                "streaming": capabilities.get("streaming"),
                "reasoning": capabilities.get("reasoning"),
                "provider": provider_name,
                "model_family": capabilities.get("model_family"),
            },
        }
        rows.append(normalized)

    normalized_document = dict(document)
    normalized_document["data"] = rows
    return json.dumps(normalized_document, ensure_ascii=False, separators=(",", ":")).encode()


def configured_catalog_filters(
    allowlist_value: str | None, denylist_value: str | None
) -> tuple[frozenset[str], frozenset[str]]:
    """Parse comma-separated local model policy values."""
    return _model_set(allowlist_value), _model_set(denylist_value)
