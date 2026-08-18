"""Dynamic model discovery from OpenAI-compatible /v1/models endpoints."""

import json
import time
import urllib.request

from verdict.classifier import classify
from verdict.models import ModelInfo, ProviderConfig

_CACHE: dict[str, dict[str, float | list[ModelInfo]]] = {}


def normalize_model_record(provider_name: str, record: object) -> ModelInfo | None:
    """Normalize common public catalog row shapes without asserting runtime health."""
    if not isinstance(record, dict):
        return None
    model_id = record.get("id") or record.get("name")
    if not isinstance(model_id, str) or not model_id or model_id.startswith("auto/"):
        return None
    raw_capabilities = record.get("capabilities", {})
    if isinstance(raw_capabilities, dict):
        capabilities = frozenset(key for key, value in raw_capabilities.items() if value is True)
        context = raw_capabilities.get("context", record.get("context_window", -1))
    elif isinstance(raw_capabilities, (list, tuple, set, frozenset)):
        capabilities = frozenset(item for item in raw_capabilities if isinstance(item, str))
        context = record.get("context_window", -1)
    else:
        capabilities, context = frozenset(), record.get("context_window", -1)
    return ModelInfo(
        id=model_id,
        provider=provider_name,
        capability_tier=classify(model_id),
        context_window=context if isinstance(context, int) else -1,
        capabilities=capabilities,
        is_available=True,
        availability_state="eligible",
        source="catalog",
    )


def fetch_models(provider_name: str, config: ProviderConfig, ttl: int = 60) -> list[ModelInfo]:
    now = time.time()
    cached = _CACHE.get(provider_name)
    if cached and (now - float(cached["ts"])) < ttl:  # type: ignore
        return cached["models"]  # type: ignore

    url = f"{config.base_url.rstrip('/')}{config.models_endpoint}"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})

    # Try fetching from environment if token isn't explicit
    import os

    token = config.api_key or (os.environ.get(config.api_key_env) if config.api_key_env else None)
    if token:
        req.add_header("Authorization", f"Bearer {token}")

    try:
        with urllib.request.urlopen(req, timeout=10) as response:  # nosec B310
            data = json.loads(response.read().decode())
            models = []
            for m in data.get("data", []):
                model = normalize_model_record(provider_name, m)
                if model is not None:
                    models.append(model)
            _CACHE[provider_name] = {"ts": now, "models": models}
            return models
    except Exception:
        return []
