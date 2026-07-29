# ADR-013: Qualify Chat and Responses protocol surfaces independently

- **Status:** Proposed — implementation tracked by #172
- **Date:** 2026-07-29
- **Related:** [ADR-001](ADR-001-evidence-ledger.md), [ADR-007](ADR-007-omniroute-catalog-qualification.md), [ADR-010](ADR-010-fail-closed-capability-passports.md), [ADR-012](ADR-012-consented-budgeted-probes.md), [#106](https://github.com/mrnicholasbcarter-code/verdict-core/issues/106), [#172](https://github.com/mrnicholasbcarter-code/verdict-core/issues/172)

## Context

An OmniRoute catalog row and a successful HTTP response identify a route but do
not establish that the route implements Chat Completions or Responses. The two
OpenAI-compatible surfaces have different request, response, streaming, and
termination contracts. A route can support one while failing the other, and a
stream that disconnects after emitting text is not a completed capability
observation.

## Decision

Verdict defines versioned, hermetic protocol probe cases for Chat Completions
and Responses. Each case is bound to the complete `RouteIdentity` and emits a
sanitized observation with a protocol identifier, case version, status,
freshness, confidence, deterministic evidence digest, response bounds, and
limitations. The observation converts directly to the existing
`CapabilityEvidence` contract; it does not create a second passport or receipt
family.

Non-stream cases require a protocol-specific valid response shape. Streaming
cases require output plus the protocol's terminal event. Truncated,
malformed, oversized, cancelled, unauthorized, rate-limited, timeout, and
upstream-error cases are non-ready and retain only normalized classifications.
Unknown or missing capability evidence remains fail-closed.

The existing ADR-012 scheduler remains the owner of provider consent, request,
token, byte, duration, cooldown, quarantine, and cross-model scheduling
budgets. Protocol cases use injected transports in CI; any live caller must
carry the same explicit consent boundary. No raw payload, prompt, credential,
authorization header, query-bearing URL, or provider response is persisted.

## Consequences

- Chat and Responses qualification cannot be conflated through a shared alias.
- Complete-stream behavior is observable separately from non-stream behavior.
- Hermetic tests cover protocol semantics without hosted credentials or live
  provider access.
- Protocol evidence can be attached to a v1 passport after route and receipt
  ownership work is complete; this ADR does not add persistence, ranking, or
  promotion.

## Verification

`tests/test_protocol_probes.py` covers both request/response surfaces,
protocol-specific shape validation, complete and truncated streams,
cancellation, normalized HTTP failures, redacted route identity, response and
event limits, deterministic digests, and conversion to fail-closed capability
evidence.
