# ADR-033: SharedMemoryProvider boundary and advisory envelope

- **Status:** Accepted — implemented
- **Date:** 2026-09-22
- **Deciders:** Product / Architect lock (MEMORY M1)
- **Related:** [ADR-004](ADR-004-local-first-memory-plane.md), [ADR-009](ADR-009-durable-memory-write-gate.md), [ADR-005](ADR-005-code-intelligence-graph-memory-bridge.md)

## Context

Harnesses need one network-reachable shared memory service so agents can
store and recall cross-session context. A remote store must never become
Verdict's local authority. SQLite-vec behind `doobidoo/mcp-memory-service`
is a useful first deployment, but Core must not absorb backend-specific
APIs or generative LLM dependencies for health, store, or search.

## Decision

Introduce a versioned, backend-neutral `SharedMemoryProvider` protocol and a
canonical `SharedMemoryEnvelope` (`shared-memory-envelope/v1`). The provider
is an optional anti-corruption layer:

1. **Local authority unchanged.** `MemoryPlane` (ADR-004) and `MemoryGate`
   (ADR-009) remain the durable SoT. Shared hits normalize into
   `MemoryRecord` with `authority_verified=false` by default and
   `authority="shared-memory-advisory"`.
2. **Canonical envelope.** Identity, project/scope, provenance, sensitivity,
   classification (`source`|`derived`), content hash, and idempotency key are
   Core-owned. Backend fingerprints live only under an explicit metadata
   extension namespace.
3. **First adapter.** `MCPMemoryServiceProvider` talks HTTP to MCP Memory
   Service with bounded timeouts, bearer auth from env/config references,
   redaction, and a named error/certification taxonomy. SQLite-vec and local
   ONNX embeddings are deployment details, not Core schema.
4. **Setup/doctor.** Discovery reuses an existing instance and never silently
   installs or mutates third-party software. Doctor reports endpoint,
   provider/protocol version, auth state, health, and backend type with
   secrets redacted. Named states include `not-installed` … `incompatible`.
5. **Empty ≠ unavailable.** A successful search with zero hits is success;
   timeout/unreachable/auth failures are distinct named statuses.

## Consequences

- Harnesses integrate against `SharedMemoryProvider`, not SQLite-vec.
- Remote records cannot grant tenant/project access or bypass MemoryGate.
- CI proves the contract with a deterministic fake; live smoke is optional
  and env-gated. Core unit tests require no Internet and no generative API.
- Replacing the first backend is an adapter swap, not a Core schema change.
