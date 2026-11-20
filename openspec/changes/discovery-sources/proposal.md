# Discovery sources (S2)

## Why

BOD-342 (model discovery S2): Verdict needs a stable, offline-testable set of
adapters that collect raw model facts from multiple public sources.  These facts
feed the discovery registry (S3) and, eventually, OmniRoute evidence-based
admission.  Without them every capacity decision relies on a single source of
truth (OmniRoute itself), which breaks when the gateway is unavailable.

## What Changes

- New package `verdict/discovery/sources/` with five source adapters:
  - **OmniRoute** — reads `OmniRouteEvidence` already in memory; no extra HTTP.
  - **OpenRouter** — public `/api/v1/models` pricing catalog.
  - **Kilo** — public `/api/gateway/models` with `isFree` flag.
  - **models.dev** — public `/api/v0/models` catalog.
  - **Community lists** — two pinned GitHub commits (free-coding-models,
    dsh-our-free-model); JS and JSON parsers; license checked, not cached.
- `DiscoveryRow` frozen dataclass: facts only, never capacity decisions.
- Injectable `Fetcher` protocol for fully offline unit tests.
- `collect_sources(...)` aggregate entry point in `verdict/discovery/collect.py`.
- `scripts/refresh_discovery_sources.py` CLI that writes cache under
  `$VERDICT_HOME/discovery/sources/`.

## Impact

- No changes to `verdict/capacity.py`, admission, routing, or the existing
  `verdict/discovery/` command layer.
- `verdict/discovery/__init__.py` replaces the flat `verdict/discovery.py`
  module; public API is identical.
- All new code is standard-library Python; no new runtime dependencies.
