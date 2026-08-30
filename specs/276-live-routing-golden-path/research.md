# Research: Live Routing Golden Path

**Feature**: `276-live-routing-golden-path`  
**Date**: 2026-08-30

## R1. Catalog membership vs qualification

- **Decision**: A catalog row is identity + published specs only. `availability_state` stays unknown until a bounded probe. Name-derived capability guesses are forbidden.
- **Rationale**: `verdict/catalog.py` currently infers vision/tools/family from the id string and defaults `tools`/`structured_output` to true. That contradicts FR-001b. OmniRoute already treats `/v1/models` as a listing, not eligibility (`specs/012-portfolio-mvp-launch/omniroute-task-routing-spec.md`).
- **Alternatives considered**: Keep name heuristics as a bootstrap. Rejected: they silently qualify unpaid-looking paid models and fail paid-usage proofs.

## R2. Specification fetch

- **Decision**: Fetch published facts from the gateway/provider catalog the same class of way OmniRoute syncs models and pricing: identity, pricing/cost class, context and output limits, tools, modalities. Required missing fields stay unclassified and exclude the candidate.
- **Rationale**: User-confirmed. OmniRoute: `MODEL_SYNC_INTERVAL_HOURS`, `/v1/models`, `/api/pricing`. Hermes Agent: provider routing + named fallback chains, not keyword guessing as policy.
- **Alternatives considered**: Operator-hand-labeled cost class. Rejected: labels drift and aliases lie. Hermes `evey-delegate-model` keyword task-type routing. Rejected: not fail-closed, not explainable as policy.

## R3. Four-layer detection

- **Decision**: Gateway → Provider → Concrete identity → Mix. Mix is an inspectable sequence of named identities. Opaque `auto/*`, virtual factories, and unnameable combo steps are dropped.
- **Rationale**: OmniRoute combos are `provider + model + connection` steps with `compositeTiers`. Auto Combo is a scorer, not a candidate. Neural MoE is unrelated. Mix cost class is the first remaining qualified step that would run (clarify Q2).
- **Alternatives considered**: Treat combo id as one model. Rejected: paid first step could hide behind a cheaper later step.

## R4. Execution repository and stack

- **Decision**: Implement only in `verdict-core` (Python, pytest, uv). Reuse `catalog`, `omniroute_catalog`, `availability`, probes, `dispatcher`, `enforcement`, receipts, flagship demo. Add a golden-path orchestrator; do not add a new repo.
- **Rationale**: Constitution III. Existing modules already filter snapshots; they do not yet own live fetch → qualify → select → explain → one named check.
- **Alternatives considered**: New microservice. Rejected: overkill. Drive routing only through OmniRoute auto-combo. Rejected: Core would cease to be policy authority.

## R5. Bounded unit

- **Decision**: Named check is live completion whose body must be exactly JSON `{"golden_path":"ok"}`. Checker parses JSON and requires `golden_path == "ok"`. Not “any model reply.”
- **Rationale**: Employment-proof and SC-004.
- **Alternatives considered**: Chat completion as success. Rejected in clarify.

## R6. Failover

- **Decision**: Cheaper-first through remaining unique qualified identities; paid only after cheaper unused identities are gone; no same-identity retry; stop on first checker pass or exhaustion (clarify Q4).
- **Rationale**: User chose D without removing cheaper-first.
- **Alternatives considered**: Cap at one failover. Rejected by user. Fail over paid while cheaper remains. Forbidden by FR-005.

## R7. Freshness

- **Decision**: This-run capture or operator-declared freshness window (clarify Q5). Degraded provided catalog must carry capture time. Identity-name match does not override staleness.
- **Rationale**: Aligns with existing `DEFAULT_CATALOG_FRESHNESS_SECONDS` (3600) in `omniroute_catalog.py` as the default window unless the operator declares another.
- **Alternatives considered**: Always-valid cache. Rejected.

## R9. First-party usage probes (same method as CodexBar, not the app)

- **Decision**: Reimplement the detection pipeline, do not call CodexBar/Toolbar. Per provider: (1) `isAvailable` via well-known credential JSON/env, (2) timeout-bounded fetch of that provider’s documented usage endpoint, (3) map to allowlisted remaining-quota. Strategy order: OAuth/file token, then API key, then skip. No cookie import, no Keychain, no PTY scrape, no writes to auth files. MVP providers: Codex (`~/.codex/auth.json` → `chatgpt.com/backend-api/wham/usage`), Claude (`~/.claude/.credentials.json` → `api.anthropic.com/api/oauth/usage`), OpenRouter credits if env key present, xAI management balance if management key+team present.
- **Rationale**: Operator asked to check the same way the toolbar does, without depending on the app. CodexBar docs confirm file+endpoint pairs ([codex.md](https://github.com/steipete/CodexBar/blob/main/docs/codex.md), [claude.md](https://github.com/steipete/CodexBar/blob/main/docs/claude.md)). Cookie strategies need Full Disk Access and are Mac-browser specific; they are out of scope here.
- **Alternatives considered**: Shell out to `codexbar usage` (app dependency). Mutate `auth.json` on refresh (CodexBar itself refuses this). Cookie scrape in v1 (deferred to US6, opt-in, after live demo).

## R8. Live surface required

- **Decision**: Golden-path pass requires a live gateway or provider: fetch `/v1/models` (and pricing facts if separate) from a real endpoint, then execute the named check on the selected identity through that same class of surface. Default live target is the operator’s OmniRoute-compatible endpoint. Fixture catalogs are unit-test only. Unreachable live surface → blocked, not passed.
- **Rationale**: Operator rejected fixture-pass as vaporware (2026-08-30).
- **Alternatives considered**: Provided-catalog degraded success. Rejected.
