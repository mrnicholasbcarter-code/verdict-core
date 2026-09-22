# BOD-146 Implementation Plan

1. Define ADR-034 contracts for local-first outbox, deterministic capture, retry/dead-letter, and fail-open recall.
2. Add a SQLite durable outbox and deterministic capture policy.
3. Add a bounded mirror worker using canonical envelope idempotency keys.
4. Wire optional outbox enqueue into the same `MemoryPlane.put` SQLite transaction.
5. Add a SharedMemory `CapabilityProvider` and multi-provider `memory.search` fan-out.
6. Prove acceptance, regression, static typing, formatting, and lint gates.
