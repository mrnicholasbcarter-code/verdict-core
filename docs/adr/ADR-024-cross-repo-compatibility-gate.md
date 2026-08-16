# ADR-024: Cross-Repo Compatibility Gate

**Status**: Proposed (not yet implemented)
**Date**: 2026-08-16
**Story**: [VERDICT-CON-001](https://github.com/mrnicholasbcarter-code/verdict-ecosystem/blob/main/VERDICT-CON-001.md) (verdict-ecosystem)

## Context

The Verdict ecosystem spans multiple independently-versioned repositories
(`verdict-core`, `verdict-node`, `verdict-risk`, `verdict-strategy`,
`verdict-backtest`, `verdict-cockpit`) that share contract schemas
(`TaskSpec`, `ExecutionEnvelope`, routing decision contracts, and others).
Nothing currently prevents a repo from integrating against a contract
version it has not verified compatibility with — a schema change in
`verdict-core` could silently break a downstream consumer at runtime rather
than being caught before integration.

Per `verdict-core`'s own operating principle (CLAUDE.md, applied
consistently across ADR-002's gate/orchestrator separation and ADR-022's
provider conformance suite): advisory signals must never silently stand in
for a verified guarantee. Compatibility between repos should be no
exception — an unverified or absent compatibility declaration should fail
closed, not be assumed compatible.

`verdict-core/IMPLEMENTATION_STATUS.md` (verdict-ecosystem, as of
2026-08-02) lists CON-001 as **Planned**. This ADR records the intended
design direction; it intentionally does not claim implementation.

## Decision (proposed)

Establish a compatibility manifest and gate, following the same
fail-closed shape as the routing eligibility gate:

- `verdict-core` publishes a machine-readable compatibility manifest
  (contract schema versions and hashes) alongside each release, analogous
  to `canonical_hash()` in `verdict/provider_receipts.py`.
- Each downstream repo declares the manifest version(s) it has verified
  compatibility against.
- A gate (CLI command and/or CI check, run in each downstream repo) checks
  the declared version against the manifest actually in use and fails
  closed — blocking the integration — on a mismatch or missing
  declaration, rather than assuming compatibility.
- This gate is deterministic and offline-checkable, consistent with the
  eligibility gate's "no LLM in the enforcement path" principle
  (README, ADR-002).

## Consequences (anticipated)

- Requires coordinated work across `verdict-core` (manifest
  publication) and each consumer repo (declaration + gate wiring) — this is
  cross-repository scope, not a `verdict-core`-only change.
- Without this gate, contract drift between repos is currently
  undetected until runtime failure; that risk remains open until CON-001 is
  implemented.
- Tracked for implementation as a follow-up issue (not part of PR #263)
  since it requires design work beyond what this ADR trail can respond
  for in a documentation-only pass.
