# Feature Specification: Core model metadata store (BOD-108)

## Issue

Linear BOD-108. Researcher source map 2026-09-18.

## Goal

Give Verdict Core its own provenanced model-capability store from free public
sources so later BOD-100 can gate on tools/vision/structured/context without
treating OmniRoute catalog rows as metadata authority.

## Functional requirements

1. Store schema records id, hard caps, nullable soft scores, and per-field provenance (`source` + `version|fetched_at`).
2. P0 fetchers: models.dev `api.json` / `models.json` (primary) and LiteLLM `model_prices_and_context_window.json` (secondary fill only).
3. Explicit OmniRoute → models.dev map; unmapped or required-unknown is a named drop.
4. `verdict metadata refresh|show|lookup` persists and reads `~/.verdict/model-metadata.json`.
5. Unit tests use vendored fixtures and do not use the network.
6. Soft ranks are stored only when fetched; P1 sources may skip with a documented reason.

## Non-goals

- BOD-107 worthiness classifier
- BOD-100 admit-path capability gate wiring
- BOD-109 free-first fallback
- OmniRoute as metadata source of truth
- Inventing agentic/Elo/BFCL scores
