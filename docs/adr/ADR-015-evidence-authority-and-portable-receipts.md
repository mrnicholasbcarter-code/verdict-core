# ADR-015: Evidence authority and portable receipts

- **Status:** accepted for issue #115
- **Date:** 2026-07-30
- **Scope:** Verdict evidence contracts; transport, storage, probes, and learning remain separate adapters

## Decision

Verdict represents evidence for an exact executable route with two related
contracts:

1. `RouteIdentity` preserves gateway instance, provider, connection scope,
   endpoint and protocol, upstream model and revision, account/endpoint class,
   transformation chain, and fallback chain. A requested alias is not a route
   identity and must remain separate at integration boundaries.
2. `EvidenceReceipt` is an append-only, portable envelope for decision,
   context, execution, verification, and outcome metadata. Its payload is
   JSON metadata only; raw prompts, completions, messages, tool arguments, and
   credentials are rejected at the contract boundary. Forward-compatible
   fields belong in `extensions`.

Every evidence item carries source, method, adapter version, scope, observed
and expiry times, confidence, sample count when meaningful, a content digest,
and explicit limitations. Receipts carry stable IDs and parent references so a
replay can reconstruct the evidence chain without copying private payloads.

## Authority semantics

`claimed` describes an upstream catalog or provider assertion. `observed`
describes a bounded direct observation. `verified` describes an observation
independently checked by a Verdict verifier. `inferred` describes a derived
or learned statement. Authority is provenance, not permission.

For hard capability requirements, the precedence rule is deliberately
fail-closed:

- only a fresh direct observation with status `supported` admits a capability;
- a fresh `unsupported` observation denies it;
- missing, expired, malformed, contradictory, or unmonitored evidence is
  `unknown`;
- claims, inferences, scores, and stale observations can explain a decision but
  cannot turn `unknown` into `supported`;
- when observations conflict, the conflict is retained in the evidence chain,
  and the required capability remains `unknown` until a newer, scoped
  observation resolves it under policy.

`verified` evidence is stronger for explanation and promotion, but it does not
override a newer contradictory observation. Every consumer must apply the
field-specific policy rather than comparing authority strings generically.

## Consequences

- Python and TypeScript schemas round-trip the same canonical receipt shape.
- The existing capability passport remains the hard-admission adapter and now
  carries the same provenance fields.
- `ReceiptStore` and future durable stores may persist these envelopes, but
  storage does not become the authority for routing policy.
- Signatures or keyed fingerprints may establish integrity and issuer
  identity; they do not prove that a model capability or quality claim is true.
