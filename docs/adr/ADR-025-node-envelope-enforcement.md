# ADR-025: Node Envelope Enforcement

**Status**: Accepted
**Date**: 2026-08-16
**Accepted**: 2026-08-20 (verdict-node NOD-002 PRs #33, #41, #42, #44, #45; verdict-core #286 closure)
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

## Decision (implemented)

`verdict-node` enforces the `ExecutionEnvelope` schema published in
`@bodanglin/verdict-contracts` (`executionEnvelopeSchema`) before any
upstream forwarding. The enforcement is implemented in
`src/middleware/forwarder.ts:enforceExecutionEnvelope()` and exercised by
three test suites:

- `tests/contract-parity.test.ts` — unit tests of `enforceExecutionEnvelope`
  against valid/invalid TS-shaped envelopes (expiry, tamper via
  `expectedPolicyDigest`, model/tool allow-lists, budget ceiling).
- `tests/middleware/forwarder.test.ts` — integration tests of the full
  Express middleware, including SSE and non-SSE paths sharing the same
  authority check.
- `tests/middleware/streaming-field-preservation.test.ts` — streaming denial
  tests (expired envelope) proving SSE and non-SSE paths reject before
  upstream execution.

`verdict-core` ships the Python canonical `ExecutionEnvelope` (TaskSpec +
eligibility_decision + policy_digest + ...) and the TypeScript schema
(decision_id + policy_version + expires_at + ...). These are two divergent
contracts both named `ExecutionEnvelope`. The cross-runtime parity suite
(`tests/test_contracts.py` + `tests/typescript/contracts.test.ts`) now asserts
that a shared set of TS-shaped **invalid** envelope fixtures is rejected
identically by both runtimes (Python via strict unknown-field/required-field
checks; TypeScript via Zod schema validation). This satisfies the
enforcement-parity gate for the invalid case; the valid case remains a known
divergence tracked by [VER-003](https://github.com/mrnicholasbcarter-code/verdict-core/issues/220).

## Consequences (actual)

- `verdict-node` is fail-closed by default: `requireExecutionEnvelope: true`
  is the middleware default; unknown fields, expired envelopes, tampered
  policy digests, disallowed models/tools, and over-budget requests all
  return typed denial codes (`envelope_invalid`, `envelope_expired`,
  `envelope_tampered`, `model_disallowed`, `tool_disallowed`,
  `budget_exceeded`).
- The canonical Python `ExecutionEnvelope` (issue #220) and the published
  TS `ExecutionEnvelope` (issue #286 / NOD-002) are **not the same contract**.
  This is a deliberate point-in-time design: the TS envelope carries
  expiry/tamper-proofing fields needed at the edge; the Python envelope
  carries TaskSpec/eligibility provenance needed by the orchestrator.
  Reconciliation into a single universal `ExecutionEnvelope` is tracked as
  VER-003 / core #220 (open).
- The cross-runtime parity suite proves fail-closed parity for the shared
  invalid-envelope set. It does not assert that a valid TS envelope passes
  Python validation (it does not — Python rejects it as unknown fields).
  This is documented, not a regression.
- No production path in `verdict-node` bypasses Core authority: the
  `nextApiHandler` continuation-after-503 defect (PR #45) was fixed; all
  proxy paths enforce the envelope when a Core decision is available.
- Compatibility mode (`requireCoreDecision: false`) is explicitly opt-in
  and documented as non-policy-gated.
