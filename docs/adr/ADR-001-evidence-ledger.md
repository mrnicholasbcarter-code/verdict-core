# ADR-001: Versioned, Privacy-Safe Execution Evidence

- **Status**: accepted
- **Date**: 2026-07-22
- **Deciders**: Verdict Core maintainers
- **Tags**: evidence, provenance, privacy, transport, lifecycle
- **Amends**: `ADR-ORCHESTRATOR-ROUTING.md`
- **Related**: Ruflo ADR-103, ADR-131, ADR-144, ADR-150, ADR-171, ADR-176

## Context

Verdict needs to explain routing and execution behavior, but the current proxy
cannot prove task correctness, does not yet have durable multi-worker storage,
and must not persist prompts, completions, credentials, or guessed model-quality
claims. Caller-provided request IDs are not safe as storage keys because they
can be reused concurrently.

Ruflo's existing patterns establish the required constraints: append-only
temporal history (ADR-103), content-boundary safety (ADR-131), delegated
authorization scope (ADR-144), graceful optional integration (ADR-150),
provenance-labelled evaluation (ADR-171), and receipt-backed reversible
improvement (ADR-176).

## Decision

Verdict exposes execution evidence as a versioned, tagged envelope around the
existing decision and outcome contracts. The envelope is selected through an
authenticated, scope-bound explain API and uses a server-generated opaque
evidence ID. The decision snapshot is immutable; lifecycle events are
append-only; terminalization is idempotent.

The request path records only facts directly observed by the transport adapter.
Task correctness, model quality, cost, retries, fallbacks, and runtime versions
remain explicitly unobserved unless supplied by authoritative execution
receipts. A bounded process-local store is acceptable for the first slice only
when its restart and multi-worker limitations are documented. Durable SQLite/WAL
storage is a separate follow-up and must preserve this interface and invariants.

The legacy `/v1/route` response remains unchanged. Execution evidence is not
embedded into it. Availability explanations and execution-evidence explanations
use distinct tagged response shapes.

The first scope implementation is intentionally deployment-wide: every request
authenticated by the configured `LLMGATE_AUTH_TOKEN` uses the fixed
`server-auth` scope. The caller cannot select or forge a scope header. Tenant
isolation requires an authenticated-principal resolver and is a separate
follow-up; this slice must never be described as multi-tenant evidence
isolation.

## Consequences

Positive:

- duplicate caller IDs cannot corrupt another execution;
- explanations cannot silently recompute mutable candidate state;
- stream cancellation and resource cleanup become testable contract behavior;
- transport success cannot be advertised as verified task success;
- the later durable ledger can be substituted without changing callers.

Negative:

- the initial backend is not restart-safe;
- the explain API carries more explicit version/provenance metadata;
- durable storage, CLI inspection, and task verification require follow-up work.

## Rejected alternatives

- embedding evidence in `/v1/route`: breaks compatibility and expands disclosure;
- using caller IDs as keys: permits collisions and cross-finalization;
- treating HTTP 2xx as task verification: violates ADR-171 provenance rules;
- implementing the full durable delivery controller in issue #53: too broad and
  would couple evidence, orchestration, SCM, and learning changes into one
  unreviewable patch.

## Validation

Issue #53 must pass schema, privacy, lifecycle, duplicate-ID, scope,
compatibility, stream-close, and full repository verification tests. A later
durable-ledger ticket must add restart, multi-worker, retention, deletion, and
tamper-evident replay tests before process-local evidence can be called
production-grade.

## Links

- Supersedes: none
- Amended by: none
- Related: `docs/patterns/privacy-safe-execution-evidence.md`
