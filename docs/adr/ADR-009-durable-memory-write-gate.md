# ADR-009: Durable local MemoryGate

- **Status:** Accepted — implementation in #126
- **Date:** 2026-07-29
- **Related:** [ADR-004](ADR-004-local-first-memory-plane.md), [#126](https://github.com/mrnicholasbcarter-code/verdict-core/issues/126)

## Decision

All new lifecycle/session memory writes pass through `verdict.memory_gate.MemoryGate`
before entering the local SQLite `MemoryPlane`. The gate derives authority from
an explicit registry, enforces namespace policy, TTL, confidence, provenance,
scope, and bounded content, redacts credentials/prompts, and records a
machine-readable decision event in the same plane.

An active record with different content is a contradiction. It is rejected
unless the request explicitly names the active record in `supersedes`. Duplicate
content is idempotent. External providers and caller-supplied authority levels
are never proof and cannot bypass the gate.

## Consequences

- accepted and rejected write decisions survive process restart;
- contradiction and supersession links are queryable through MemoryPlane
  history and gate events;
- offline operation needs no provider, network, embedding model, or API key;
- legacy async governor calls remain compatible while the storage boundary is
  now durable and testable;
- existing document and session adapters remain source adapters and may be
  unavailable without blocking the local plane.

## Verification

`tests/test_memory_gate.py` covers restart persistence, authority derivation,
redaction, contradiction/supersession, TTL/confidence/size bounds, and async
compatibility. `scripts/memory_offline_smoke.py` emits a redacted deterministic
offline report with search and export evidence.

The machine-readable report is intentionally limited to non-sensitive fields:

```json
{
  "deterministic_export": true,
  "gate_event_count": 2,
  "network": "disabled",
  "provider": "not_required",
  "redaction_proven": true,
  "status": "ready"
}
```
