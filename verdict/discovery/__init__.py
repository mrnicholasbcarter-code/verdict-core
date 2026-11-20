"""verdict.discovery — model prober, coding scores, and proof registry.

Port of freescan's probe/toolprobe/scores into verdict-core (S3).
Only routes classified free or subscription *by a fact* are probed.

Public surface:
  verdict.discovery.model      — Route, ProbeResult, Score, status constants
  verdict.discovery.probe      — probe_one, _set_transport
  verdict.discovery.toolprobe  — tool_probe
  verdict.discovery.scores     — load_scores, ScoreIndex
  verdict.discovery.registry   — RegistryEntry, DiscoveryRegistry,
                                  load_registry, save_registry, run_refresh

Legacy API (preserved from verdict/discovery.py for existing callers):
  fetch_models, normalize_model_record
"""

from __future__ import annotations

import json
import time
import urllib.request

from verdict.classifier import classify
from verdict.credentials_store import GATEWAY_KEY_ENV_NAMES, resolve_gateway_key
from verdict.discovery.model import (
    AUTH_ERROR,
    AUTH_NONE,
    DOWN,
    ERROR,
    GARBAGE,
    NO_KEY,
    RATE_LIMITED,
    TIMEOUT,
    UP,
    ProbeResult,
    Route,
    Score,
)
from verdict.discovery.registry import (
    REGISTRY_TTL_S,
    DiscoveryRegistry,
    RegistryEntry,
    load_registry,
    run_refresh,
    save_registry,
)
from verdict.http_safety import open_no_redirect
from verdict.models import ModelInfo, ProviderConfig

# ---------------------------------------------------------------------------
# Legacy API — preserved from verdict/discovery.py (module → package, S3)
# ---------------------------------------------------------------------------

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
    """Fetch models from an OpenAI-compatible /v1/models endpoint (cached)."""
    import os

    now = time.time()
    cached = _CACHE.get(provider_name)
    if cached and (now - float(cached["ts"])) < ttl:  # type: ignore[arg-type]
        return cached["models"]  # type: ignore[return-value]

    url = f"{config.base_url.rstrip('/')}{config.models_endpoint}"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})

    token = config.api_key or (os.environ.get(config.api_key_env) if config.api_key_env else None)
    if not token and config.api_key_env in GATEWAY_KEY_ENV_NAMES:
        resolved = resolve_gateway_key()
        token = resolved[0] if resolved else None
    if token:
        req.add_header("Authorization", f"Bearer {token}")

    try:
        with open_no_redirect(req, timeout=10) as response:
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


__all__ = [
    "AUTH_ERROR",
    "AUTH_NONE",
    "DOWN",
    "ERROR",
    "GARBAGE",
    "NO_KEY",
    "RATE_LIMITED",
    "REGISTRY_TTL_S",
    "TIMEOUT",
    "UP",
    "DiscoveryRegistry",
    "ModelInfo",
    "ProbeResult",
    "ProviderConfig",
    "RegistryEntry",
    "Route",
    "Score",
    "fetch_models",
    "load_registry",
    "normalize_model_record",
    "run_refresh",
    "save_registry",
]
