"""Source adapters for model discovery.

Each adapter returns a list of :class:`~verdict.discovery.sources._types.DiscoveryRow`
objects containing facts about models.  Adapters never decide capacity;
that is ``verdict/capacity.py``'s responsibility.

Adapters:
- :mod:`~verdict.discovery.sources.omniroute` — OmniRoute /v1/models +
  connections + optional free-tier summary
- :mod:`~verdict.discovery.sources.openrouter` — OpenRouter public
  pricing catalog
- :mod:`~verdict.discovery.sources.kilo` — Kilo public model listing
  with ``isFree`` flag
- :mod:`~verdict.discovery.sources.models_dev` — models.dev public
  catalog
- :mod:`~verdict.discovery.sources.community` — free-coding-models
  ``sources.js`` and dsh-our-free-model ``registry.json`` (community
  lists, fetched at refresh time from pinned GitHub commits)
"""

from verdict.discovery.sources._fetcher import Fetcher, default_fetcher, make_stub_fetcher
from verdict.discovery.sources._types import DiscoveryRow
from verdict.discovery.sources.community import (
    CommunityConfig,
    fetch_dsh,
    fetch_fcm,
    parse_dsh,
    parse_fcm,
)
from verdict.discovery.sources.kilo import fetch_kilo, parse_kilo
from verdict.discovery.sources.models_dev import fetch_models_dev, parse_models_dev
from verdict.discovery.sources.omniroute import parse_free_tier_ids, rows_from_evidence
from verdict.discovery.sources.openrouter import fetch_openrouter, parse_openrouter

__all__ = [
    "CommunityConfig",
    "DiscoveryRow",
    "Fetcher",
    "default_fetcher",
    "fetch_dsh",
    "fetch_fcm",
    "fetch_kilo",
    "fetch_models_dev",
    "fetch_openrouter",
    "make_stub_fetcher",
    "parse_dsh",
    "parse_fcm",
    "parse_free_tier_ids",
    "parse_kilo",
    "parse_models_dev",
    "parse_openrouter",
    "rows_from_evidence",
]
