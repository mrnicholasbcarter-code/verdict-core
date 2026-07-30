# ADR-014: Qualify strict structured output and tool lifecycles independently

- **Status:** Accepted — implemented in #174
- **Date:** 2026-07-30
- **Related:** [ADR-001](ADR-001-evidence-ledger.md), [ADR-007](ADR-007-omniroute-catalog-qualification.md), [ADR-010](ADR-010-fail-closed-capability-passports.md), [ADR-012](ADR-012-consented-budgeted-probes.md), [ADR-013](ADR-013-independent-protocol-surface-qualification.md), [#106](https://github.com/mrnicholasbcarter-code/verdict-core/issues/106), [#174](https://github.com/mrnicholasbcarter-code/verdict-core/issues/174)

## Decision

Chat Completions and Responses strict structured output are qualified as
separate exact-route cases. An HTTP success response is insufficient: the
protocol envelope must contain assistant output and the decoded JSON must pass
the case's strict schema with required fields, supported types, enumerations,
and no additional properties. Missing, malformed, extra, or type-invalid
fields are `schema_invalid` and map to fail-closed unknown evidence.

Tool qualification is a bounded state machine over injected transports. Tool
names must be declared, arguments must decode as JSON and pass the declared
strict parameter schema, and a successful case must include a final response
after the tool-result round trip. Multiple calls in one model turn are
reported as parallel-call evidence. Tool errors may be recovered only when a
later final response terminates the case. Undeclared tools are rejected before
any handler runs, including when a prior tool result contains adversarial
instructions. Repeated calls stop at a fixed turn bound.

All observations are versioned, tied to the complete sanitized route identity,
freshness-bounded, deterministically digested, and payload-free. Existing
consent, cancellation, bounded-turn/request and output-token, response-byte,
timeout, cooldown/quarantine, redaction, and normalized failure boundaries
remain authoritative; this slice adds no live provider access, durable receipt
persistence, ranking, or promotion.

## Verification

`tests/test_structured_qualification.py` covers independent Chat/Responses
wire shapes, strict success and failure cases, consent, cancellation, timeout
and response bounds, redaction, and deterministic evidence.
`tests/test_tool_qualification.py` covers valid arguments, parallel calls,
result round trips, error recovery, invalid arguments, unavailable tools,
injection resistance, cancellation, normalized failures, bounded termination,
and independent Responses lifecycle wire shapes.
