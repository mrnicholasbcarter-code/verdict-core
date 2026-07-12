"""Dynamic model discovery from OpenAI-compatible /v1/models endpoints."""

import json
import time
import urllib.request

from llm_gate.classifier import classify
from llm_gate.models import ModelInfo, ProviderConfig

_CACHE: dict[str, dict[str, float | list[ModelInfo]]] = {}


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
        with urllib.request.urlopen(req, timeout=10) as response:
            data = json.loads(response.read().decode())
            models = []
            for m in data.get("data", []):
                mid = m.get("id")
                if mid:
                    tier = classify(mid)
                    models.append(ModelInfo(id=mid, provider=provider_name, capability_tier=tier))
            _CACHE[provider_name] = {"ts": now, "models": models}
            return models
    except Exception:
        return []
