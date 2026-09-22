# ADR-034: Durable outbox mirror and fail-open shared recall

- **Status:** Accepted — BOD-146
- **Date:** 2026-09-22
- **Related:** ADR-004, ADR-009, ADR-022, ADR-027, ADR-033

## Context

The local SQLite MemoryPlane is Verdict's authority. A shared provider is useful for cross-session recall, but network availability cannot enter the authoritative write transaction. Empty healthy search and provider failure must remain distinguishable. Remote semantic scores are evidence, not authority.

## Decision

Eligible writes create a canonical `SharedMemoryEnvelope` outbox row in the same SQLite transaction as the local record. The capture policy is deterministic: secret-bearing and non-standard sensitivity records are denied, and low-value tool/runtime noise is skipped. It uses no model. Outbox identity is the envelope idempotency key and is unique.

A bounded worker performs remote `put` after commit. Success is durably acknowledged. Transient unavailable, timeout, rate-limit, and server failures use exponential backoff. Authentication, schema/protocol, and invalid-request failures enter a named dead letter. A crash after remote success but before acknowledgement can replay; provider idempotency makes that replay one logical write.

Shared recall implements the existing `CapabilityProvider` contract for `memory.search`. The resolver fans out to local and optional shared providers. Shared hits become `ContextUnit` values with remote-advisory authority, original content hash, source provider and external-id provenance, tenant/project scope, and score only as confidence metadata. Existing scope, status, safety, and token-budget gates still compile the pack. Unavailable, timeout, auth, schema, stale/filtered, and healthy-zero outcomes remain named and distinct. Provider exceptions fail open.

## Consequences

- `MemoryPlane` works unchanged when no outbox/provider is configured.
- No remote operation is made by `put`.
- Secrets are rejected before durable remote payload creation and never enter worker errors.
- Outbox retry is restart-safe and duplicate-safe, though physical providers must honor the canonical idempotency key as required by ADR-033.
- Shared recall cannot grant authority or bypass compiler scope, policy, safety, or budget checks.
