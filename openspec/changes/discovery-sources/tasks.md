## Implementation

- [x] Create `verdict/discovery/sources/` package with `_types.py`, `_fetcher.py`.
- [x] Implement OmniRoute adapter (`sources/omniroute.py`).
- [x] Implement OpenRouter adapter (`sources/openrouter.py`).
- [x] Implement Kilo adapter (`sources/kilo.py`).
- [x] Implement models.dev adapter (`sources/models_dev.py`).
- [x] Implement community list adapters (`sources/community.py`): FCM and DSH,
      regex-only JS parser (never evaluates JS), license checked.
- [x] Export all public symbols from `verdict/discovery/sources/__init__.py`.
- [x] Add `verdict/discovery/collect.py` aggregate entry point.
- [x] Add `scripts/refresh_discovery_sources.py` CLI.
- [x] Convert `verdict/discovery.py` → `verdict/discovery/__init__.py`
      (identical public API).
- [x] Add `.gitignore` negation for `verdict/discovery/`.
- [x] 50 offline unit tests in `tests/test_discovery_sources.py` (all pass).
- [x] `uv run ruff check . && uv run ruff format --check .` — clean.
- [x] `uv run mypy verdict --strict` — clean.
- [x] `uv run python scripts/check_reachability.py --ratchet-base origin/main`
      — 0 new findings.
- [ ] (post-merge) Confirm `verdict doctor` is green with new package present.
