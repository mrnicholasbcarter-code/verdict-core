# ADR-025: Node Envelope Enforcement

**Status**: Accepted (implemented 2026-08-20)
**Date**: 2026-08-16 (proposed), 2026-08-20 (accepted)
**Story**: VERDICT-NOD-002 (verdict-ecosystem)

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

## Decision

`verdict-node` enforces the same `ExecutionEnvelope` invariants that
`verdict-core` enforces on construction — not merely mirror the schema's
field shapes:

- Added `ExecutionEnvelope` Zod schema (`executionEnvelopeSchema`) to
  `@bodanglin/verdict-contracts` (`contracts/src/index.ts`) with all 10
  canonical fields matching Python `ExecutionEnvelope` dataclass:
  - `task_spec` (TaskSpec)
  - `eligibility_decision` (jsonObject)
  - `policy_digest` (nonEmptyString)
  - `allowed_capabilities` (array of nonEmptyString)
  - `execution_constraints` (jsonObject)
  - `verification_requirements` (VerificationPlan)
  - `evidence_ids` (array of nonEmptyString)
  - `routing_decision` (nullable jsonObject, default null)
  - `created_at` (nullableString, default null)
  - `schema_version` (default '1')

- Forwarder middleware (`verdict-node/src/middleware/forwarder.ts`) uses
  `parseContract('execution_envelope', envelope)` for canonical validation
  before applying forwarder-specific checks (expiration via `expires_at`,
  model/tool/budget constraints from `execution_constraints`).

- The forwarder strips `expires_at` (a forwarder-specific extension not in
  the canonical schema) before canonical validation, then checks expiration
  separately.

- Test envelopes updated to match canonical schema exactly (all 10 required
  fields, `workflow` nullable with default null, `workflow.steps` requires
  min 1 element if present).

## Consequences (actual implementation behavior)

- **Enforcement parity required fixes on the Python side too.** The audit
  found that `ExecutionEnvelope.from_dict` could not accept *any* JSON
  payload: `_validate_field_value` rejected `dict` values for
  Contract-typed fields (`task_spec: TaskSpec`,
  `verification_requirements: VerificationPlan`) before
  `_coerce_field_value` had a chance to convert them. Fixed in
  `verdict/contracts.py` — Contract-typed fields now validate a nested
  dict payload via the nested contract's own `from_dict`, which also means
  nested `TaskSpec`/`VerificationPlan` invariants (empty objective,
  unknown fields, budget rules, …) are enforced through the envelope.

- Python gained three invariants the Zod schema already enforced, closing
  the reverse-direction gap (Python accepting what TypeScript rejects):
  `policy_digest` must be a non-empty string; `allowed_capabilities` and
  `evidence_ids` items must be non-empty strings; and
  `VerificationPlan.on_failure` must be `deny` or `replan_or_deny`.

- Parity is defined as: identical accept/reject **verdict** per payload
  and a `ContractValidationError` type in both runtimes. Error *message
  text* is runtime-specific (Python message strings vs. Zod issue text)
  and is intentionally not part of the parity contract.

- Shared invalid-envelope fixture set (14 fixtures) lives canonically in
  `verdict-core/test_fixtures/envelopes/` and is mirrored verbatim in
  `verdict-node/test_fixtures/envelopes/`. The documented
  invariant-per-fixture manifest is `tests/fixtures/invalid_envelopes.py`;
  the full table is in `CONTRACT_PARITY.md`
  ("ExecutionEnvelope Enforcement Parity").

- Parity suites run in both repositories' CI:
  - verdict-core: `tests/test_envelope_parity.py` (pytest) and
    `contracts/tests/envelope-parity.test.ts` (vitest).
  - verdict-node: `tests/contract-parity.test.ts` (jest) plus a dedicated
    `contract-parity` job in `.github/workflows/ci.yml` that (a) checks
    the fixture mirror is byte-identical to verdict-core's canonical copy
    and (b) diffs per-fixture verdict JSON emitted by both runtimes
    (`scripts/envelope-parity-verdicts.mjs` vs.
    `scripts/envelope_parity_verdicts.py`) and fails on any divergence.

- Cross-repository sequencing: the verdict-node `contract-parity` job
  installs verdict-core from `main`, so it stays red until the Python
  fixes land on verdict-core `main` — which is the gate working as
  intended (published Python main rejects `valid_envelope.json` that
  TypeScript accepts). A `@bodanglin/verdict-contracts` release is needed
  only when the Zod schema itself changes; this ADR's TypeScript checks
  were already present in the published 0.1.0.

- The 6 pre-existing streaming test failures in
  `streaming-field-preservation.test.ts` are unrelated to envelope
  enforcement and tracked separately.

## Verification

- Divergence detection demonstrated end-to-end: temporarily reverting the
  Python `_validate_field_value` fix flipped 3 fixture verdicts
  (`valid_envelope.json` and both empty-array fixtures) and the verdict
  diff gate exited non-zero; re-applying the fix restored a clean diff.

- verdict-core: `tests/test_envelope_parity.py` 15/15 pass;
  `tests/test_contracts.py` + `tests/test_pipeline_invariants.py` 44/44
  pass; contracts vitest suite (incl. `envelope-parity.test.ts`) 29/29
  pass.

- verdict-node: `tests/contract-parity.test.ts` 37/37 pass against the
  shared fixtures; local run of the CI gate's verdict comparison reports
  zero divergences across all 14 fixtures.

- Adversarial probes (secret-bearing keys at any depth, invalid
  `on_failure`, numeric `schema_version`, unknown/negative budget fields,
  whitespace-only digest, non-string `created_at`, array
  `eligibility_decision`) produce identical verdicts in both runtimes.

## Links

- `CONTRACT_PARITY.md` — "ExecutionEnvelope Enforcement Parity" section
  (shared fixture table).
- `verdict-node/.github/workflows/ci.yml` — `contract-parity` job.
- ADR-020 (gateway adapter contracts), ADR-021 (deterministic provider
  receipts) — sibling contract-boundary decisions.