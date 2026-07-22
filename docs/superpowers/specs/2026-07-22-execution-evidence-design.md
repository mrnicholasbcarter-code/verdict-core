# Execution Evidence for Issue #53

**Status:** Approved for implementation
**Issue:** #53
**Date:** 2026-07-22
**Related ADR:** [`ADR-001`](../../architecture/ADR-EVIDENCE-LEDGER.md)

## Goal

Make route decisions and transport-observed outcomes inspectable through a
privacy-safe, versioned explain contract without changing the legacy `/v1/route`
response shape or claiming verification that the proxy did not perform.

## Design

### Boundary and compatibility

`/v1/route` remains a legacy decision response. It does not embed execution
evidence. Evidence is exposed through a dedicated execution-evidence selector
on the explain surface, with an explicit response discriminator so availability
explanations and execution evidence cannot be confused by clients.

The existing shared `RoutingDecisionContract` and `OutcomeEvent` remain the
inner v1 contracts. A new envelope identifies the evidence kind, envelope
version, opaque server-issued evidence ID, authorization scope, and the
correlated decision/outcome records.

### Evidence lifecycle

At route admission, Verdict creates a decision-time snapshot containing only
redacted protocol metadata and the candidate records available at that moment.
It then records an `execution_started` event. Terminal transport events are
appended in order and are finalized exactly once. The initial event is never
overwritten in the lifecycle representation.

The first backend remains bounded process-local storage behind an `EvidenceStore`
interface. Its limitations are explicit: it is not restart-safe and is not a
cross-worker ledger. Durable SQLite/WAL persistence is a follow-up ticket and
must implement the same interface before being enabled as a production backend.

### Truthful outcome semantics

The proxy records facts it can observe:

- buffered HTTP success or upstream error;
- dispatch cancellation/error;
- stream exhaustion, cancellation, explicit close, disconnect, or iterator
  failure;
- status code, latency, protocol feature shape, and cleanup result.

It does not infer task correctness from HTTP success. Verification, quality,
cost, provider version, model version, retry, and fallback fields are marked as
not observed or empty unless a later execution adapter supplies authoritative
receipts.

### Identity and authorization

Caller request and correlation IDs are metadata, never storage keys. Each
execution receives an opaque server-generated evidence ID. The evidence lookup
scope is derived from the authenticated deployment boundary and is checked for
every lookup. In this slice the boundary is the configured
bearer-authenticated deployment (`server-auth`); a caller cannot select the
scope, and tenant-level isolation is explicitly out of scope. Reused caller IDs are allowed but ambiguous selectors
must return a conflict directing the caller to the opaque evidence ID.

Verdict-local controls are removed before an upstream request is sent.

### Privacy

Evidence contains no prompt, completion, tool arguments, authorization values,
or raw upstream body. Task identity is represented by a bounded fingerprint and
length; protocol features and sanitized tool names are retained only where
needed for routing explainability. Redaction occurs before storage and again at
contract serialization.

## Failure handling

- No configured evidence backend: routing remains functional; explain-by-
  evidence returns an explicit unavailable response.
- Unknown evidence ID or scope mismatch: return not found without revealing
  whether another scope owns the ID.
- Multiple matches by caller request/correlation ID: return conflict and require
  the opaque evidence ID.
- Terminal finalization after eviction: do not raise from cleanup; the upstream
  resource is still closed and the request outcome remains transport-visible in
  logs/metrics owned by the caller.
- Any stream termination path is terminalized at most once, including both
  cancellation and generator close notifications.

## Verification plan

The implementation must include deterministic tests for:

1. schema-valid, redacted decision evidence and immutable candidate snapshots;
2. buffered success, buffered upstream error, denied route, dispatch error, and
   cancellation;
3. normal stream exhaustion and upstream iterator failure;
4. explicit async-generator close after partial consumption;
5. unconsumed stream cleanup and upstream `aclose()`;
6. ASGI disconnect/cancellation cleanup;
7. exactly-once terminalization under close plus cancellation;
8. duplicate caller IDs, ambiguous selectors, opaque-ID disambiguation, and
   scope isolation;
9. preservation of the legacy `/v1/route` response shape;
10. full package, type, lint, security, and graph impact verification.

## Explicit non-goals

- durable multi-process evidence storage;
- task-level correctness verification;
- automatic retries or fallbacks that the current proxy does not execute;
- model quality ranking, tier redesign, or the broader autonomous-delivery
  controller;
- CLI implementation beyond documenting the follow-up command contract.
