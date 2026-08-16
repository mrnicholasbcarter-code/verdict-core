# ADR-025: Node Envelope Enforcement

**Status**: Proposed (not yet implemented)
**Date**: 2026-08-16
**Story**: [VERDICT-NOD-002](https://github.com/mrnicholasbcarter-code/verdict-ecosystem/blob/main/VERDICT-NOD-002.md) (verdict-ecosystem)

## Context

`verdict-node` (`@bodanglin/verdict-contracts`, `@bodanglin/verdict-client`)
is the TypeScript surface for Verdict contracts such as `ExecutionEnvelope`.
Python↔TypeScript field-level parity is verified in CI
(`CONTRACT_PARITY.md`), but parity of *schema fields* does not by itself
guarantee parity of *enforcement*: `verdict-core`'s Python side validates
envelope invariants at construction (`verdict/contracts.py`,
`ExecutionEnvelope.__post_init__`-style validation, consistent with the
`ProviderReceipt`/`MemoryWriteRequest` validate-on-construct pattern used
throughout this codebase). If the TypeScript client accepts a
structurally-valid-but-semantically-invalid envelope (for example, one
whose fields satisfy the schema but violate a documented invariant) and
forwards it to a Python-side consumer, that consumer would be trusting an
envelope that was never actually validated.

`verdict-core/IMPLEMENTATION_STATUS.md` (verdict-ecosystem, as of
2026-08-02) lists NOD-002 as **Planned**. This ADR records the intended
design direction; it intentionally does not claim implementation, and no
NOD-002-specific code exists yet in `verdict-core` or `verdict-node`.

## Decision (proposed)

`verdict-node` should enforce the same `ExecutionEnvelope` invariants that
`verdict-core` enforces on construction — not merely mirror the schema's
field shapes:

- Port the Python-side envelope invariant checks (whatever
  `ExecutionEnvelope.__post_init__` enforces in `verdict/contracts.py` at
  the time of implementation) to `@bodanglin/verdict-contracts`'
  `ExecutionEnvelope` constructor/factory.
- Add a parity test asserting that a representative set of
  invalid-envelope fixtures is rejected identically by both the Python and
  TypeScript constructors (extending the existing `CONTRACT_PARITY.md`
  suite rather than creating a second, divergent one).
- A `verdict-node` client must not be able to construct or pass through an
  envelope its own runtime would reject if it originated on the Python
  side — enforcement parity, not just schema parity.

## Consequences (anticipated)

- Requires TypeScript implementation work in `verdict-node`, not just
  `verdict-core` — cross-repository scope.
- Until implemented, a `verdict-node` client can construct or forward an
  envelope that passes TypeScript's schema check but would fail Python's
  invariant check, creating a silent enforcement gap at the language
  boundary.
- Tracked for implementation as a follow-up issue (not part of PR #263).
