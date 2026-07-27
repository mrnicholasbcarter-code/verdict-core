# ADR-010: Fail-closed capability passports for exact executable routes

- **Status:** Accepted — v1 contract implemented in #167
- **Date:** 2026-07-29
- **Related:** [ADR-001](ADR-001-evidence-ledger.md), [ADR-007](ADR-007-omniroute-catalog-qualification.md), [#106](https://github.com/mrnicholasbcarter-code/verdict-core/issues/106), [#167](https://github.com/mrnicholasbcarter-code/verdict-core/issues/167)

## Context

OmniRoute and upstream catalogs expose useful model aliases and claimed
capabilities, but catalog presence, an HTTP success response, model
self-report, or gateway defaults do not prove that an exact route supports a
capability. Provider routes, connections, protocol surfaces, and revisions can
also differ while sharing a model-family name. Treating these claims as
eligibility would make protected routing optimistic and non-replayable.

## Decision

Verdict represents qualification with a versioned capability passport for one
exact route. Route identity includes gateway, provider, connection or account
scope, endpoint, protocol, upstream model ID, and optional model revision.
Claims and observations are retained in separate evidence maps.

Capability values are `supported`, `unsupported`, or `unknown`. A hard
requirement is satisfied only by a fresh observed `supported` value. Missing,
expired, stale, malformed, contradictory, or claim-only evidence resolves to
`unknown` and fails closed; a fresh negative observation takes precedence over
a conflicting claim. Passport and evidence expiry are enforced at decision
time.

Each evidence item records its source, observation and expiry times,
confidence, evidence digest, and limitations. Canonical JSON serialization and
a SHA-256 digest provide stable replay and bind future receipts to the exact
passport content. The v1 parser and JSON Schema reject unknown fields,
optimistic boolean capability shortcuts, invalid capability names, and
malformed digests.

## Consequences

- catalog metadata remains valuable provenance without becoming execution
  permission;
- distinct provider routes and protocol surfaces cannot be collapsed by an
  alias or model-family slug;
- later probe, strength, receipt, ranking, and promotion layers can consume a
  stable contract without creating a second evidence vocabulary;
- the v1 passport is deliberately not a live probe scheduler, transport
  adapter, task-strength score, signed attestation, or durable store.

## Verification

`tests/test_capability_passports.py` covers route separation, claim versus
observation precedence, expiry, fail-closed resolution, canonical digests,
strict parsing, and JSON Schema rejection of optimistic capability shapes.
The full local CI-equivalent Python suite passes with the repository's VCR
fallback exclusion, and TypeScript workspace parity tests pass. No provider
credentials, raw prompts, raw responses, or private OmniRoute/Ruflo databases
are part of this contract.
