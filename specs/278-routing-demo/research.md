# Research: Routing Demo Cost vs Quality

**Feature**: `278-routing-demo`
**Date**: 2026-08-30

## R1. Reuse cheaper-first; do not rewrite policy

- **Decision**: Call `classify_identities` + `select_route` / ordered kept candidates from `verdict.live_routing`. Demo only adds request-mix filtering, baseline (costliest kept), pricing aggregation, and presentation.
- **Rationale**: Issue/assignment mandate reuse; Feature 276 already encodes COST_RANK and paid-while-cheaper guards.
- **Alternatives considered**: Port logic from `scripts/demo-routing.py` MOCK_CATALOG auto-router. Rejected: invented fixtures.

## R2. Numeric savings need live pricing index, not only cost_class

- **Decision**: Keep the gateway `/api/pricing` (and row-embedded pricing) index beside `ConcreteIdentity` rows. Estimate per-request USD as `(input_price * est_input_tokens + output_price * est_output_tokens) / 1e6` using published $/MTok units from OmniRoute pricing. Baseline uses the same estimator on the costliest qualified identity for that request.
- **Rationale**: `ConcreteIdentity` stores `cost_class` only; savings charts need numbers. Pricing is already fetched in `live_routing_gateway._pricing_index`.
- **Alternatives considered**: Rank-only “savings” without USD. Rejected: issue asks cost comparison. Invent fixed Opus/Haiku sticker prices. Rejected: fixtures-as-demo.

## R3. 100 requests under 60 seconds

- **Decision**: One catalog+pricing fetch. Build a deterministic 100-request mix (simple/complex). Route all 100 locally with cheaper-first. Perform bounded executes (short `max_tokens`) for latency/success — prefer unique chosen identities and/or a capped parallel sample so wall clock stays under 60s. Do not require 100 full long completions.
- **Rationale**: Clarification session; catalog listing is ~3.6k models and is the slow network part once; selection is CPU. OmniRoute may 503 under pressure — record attempts honestly.
- **Alternatives considered**: 100 serial rich completions. Rejected: misses SC-002. Pure offline mock. Rejected: FR-004.

## R4. Blocked vs degraded execute

- **Decision**: If catalog/pricing fetch cannot reach the live gateway, live mode is **blocked** (no fake savings). If catalog succeeds but some/all bounded executes fail (e.g. 503 pressure), still emit the live catalog-priced comparison and report real success rate / latency from attempts; do not invent successful executes. Recorded mode requires an explicit capture path labeled `recorded`.
- **Rationale**: Assignment: down → blocked, don’t fake. Catalog-up with execute pressure is still live catalog evidence; lying about success is not.
- **Alternatives considered**: Treat any execute failure as full blocked. Rejected: would hide real cheaper-first savings when gateway only throttles completions. Silent fixture fallback. Forbidden.

## R5. Baseline definition

- **Decision**: Among kept candidates that qualify for the request class, baseline identity = max by `(COST_RANK desc, identity_id)` i.e. costliest class then stable id; cost = pricing estimator on that identity.
- **Rationale**: Clarification Q3.
- **Alternatives considered**: Fixed “Opus-only” name. Rejected: may be absent/unqualified on a given capture.

## R6. Out of scope fences

- **Decision**: No Spec 272 Phase 3 `context_ablation` / `context_pack` changes; no ADK; do not modify `scripts/demo-routing.py` MOCK path as the proof (may leave legacy script untouched or clearly deprecate in docs only if needed).
- **Rationale**: Assignment isolation.
- **Alternatives considered**: Replace MOCK script in place. Rejected: ownership/clarity; new module is cleaner.

## R7. Existing mock demo

- **Decision**: `scripts/demo-routing.py` remains non-authoritative. Authoritative demo is `verdict/routing_demo.py` + `docs/benchmarks/routing-demo.md`.
- **Rationale**: MOCK_CATALOG cannot satisfy FR-004.
- **Alternatives considered**: Teach MOCK script to call live gateway. Possible later; new module avoids mixing vaporware defaults with live path.
