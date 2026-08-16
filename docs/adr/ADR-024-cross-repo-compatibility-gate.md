# ADR-024: Cross-Repo Compatibility Gate

**Status**: Partially Implemented — `verdict-core` side complete (manifest + fail-closed gate CLI); downstream repo declarations and CI wiring still open
**Date**: 2026-08-16 (proposed); implemented 2026-08-16
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
2026-08-02) lists CON-001 as **Planned**. This ADR originally recorded the
intended design direction without claiming implementation; the
`verdict-core`-side half of that design has since been built (below).

## Decision

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

## Implementation (verdict-core side)

- `verdict/compatibility_manifest.py` — `build_compatibility_manifest()`
  builds a `CompatibilityManifest` (schema version, per-contract SHA-256
  hashes over `verdict/contracts.py`, and a combined `manifest_hash`) from
  the contracts currently defined in this repo. `check_compatibility()`
  compares a downstream repo's declared manifest against it and returns a
  `CompatibilityCheckResult(allowed, reason, mismatched_contracts)` —
  fails closed (`allowed=False`) on a missing declaration, an empty
  declaration, an unsupported schema version, or any hash mismatch.
  Unknown extra keys in the declared manifest are ignored (forward
  compatible). Covered by `tests/test_compatibility_manifest.py` (8 tests:
  determinism, pass/fail cases, round-trip, schema-version rejection).
- CLI gate, wired in `verdict/cli.py`:
  - `verdict compat manifest [--json]` — publishes the current manifest.
  - `verdict compat check --declared <path> [--json]` — reads a
    downstream repo's declared manifest JSON and exits 1 (failing closed)
    on a missing file, invalid JSON, a missing `contracts` object, or a
    `check_compatibility()` mismatch; exits 0 only when compatible.
    `--json` produces machine-readable output only (no mixed
    human/machine stdout), for CI consumption.
  - Covered by `tests/test_cli_inprocess.py` (`test_cmd_compat_*`: manifest
    JSON shape, matching declaration passes, missing file / mismatch fail
    closed, missing `--declared` fails closed).

## Consequences

- The `verdict-core` half (manifest publication + fail-closed gate CLI) is
  implemented and tested; contract drift originating in `verdict-core` is
  now machine-detectable via `verdict compat check`.
- **Still open**: each downstream repo (`verdict-node`, `verdict-risk`,
  `verdict-strategy`, `verdict-backtest`, `verdict-cockpit`) must itself
  declare the manifest version it has verified against and wire
  `verdict compat check` into its own CI — that is cross-repository scope
  this repo cannot complete unilaterally. Until each consumer repo does
  so, the gate exists but is not yet enforced anywhere.
- Tracked for the remaining cross-repo wiring as a follow-up issue per
  consumer repo.
