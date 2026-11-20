"""verdict.discovery — model prober, coding scores, and proof registry.

Port of freescan's probe/toolprobe/scores into verdict-core (S3).
Only routes classified free or subscription *by a fact* are probed.

Public surface:
  verdict.discovery.model      — Route, ProbeResult, Score, status constants
  verdict.discovery.probe      — probe_one, probe_all, set_transport
  verdict.discovery.toolprobe  — tool_probe, tool_probe_all
  verdict.discovery.scores     — load_scores, ScoreIndex
  verdict.discovery.registry   — RegistryEntry, DiscoveryRegistry,
                                  load_registry, save_registry, run_refresh

Legacy re-exports (preserved from verdict/discovery.py for existing callers):
  fetch_models, normalize_model_record
"""

from __future__ import annotations

from verdict.discovery._legacy import fetch_models, normalize_model_record
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
    "ProbeResult",
    "RegistryEntry",
    "Route",
    "Score",
    "fetch_models",
    "load_registry",
    "normalize_model_record",
    "run_refresh",
    "save_registry",
]
