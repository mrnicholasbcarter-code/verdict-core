# ADR-022: Context Provider Conformance Suite

**Status**: Accepted
**Date**: 2026-08-16 (remainder closed 2026-08-27, issue #287)
**Story**: [VERDICT-CTX-002](https://github.com/mrnicholasbcarter-code/verdict-ecosystem/blob/main/VERDICT-CTX-002.md) (verdict-ecosystem)

## Context

Verdict Core's memory plane can be backed by multiple context providers (local
SQLite, external adapters registered through `verdict.memory_adapters`).
Callers treat retrieval as advisory input, never as authorization — the same
principle the routing gate applies to model candidates (see ADR-002). Three
failure modes needed explicit, test-enforced guarantees so a misbehaving or
unavailable provider cannot silently corrupt that guarantee:

1. Retrieval ranking/scoring is informational metadata, not a trust signal —
   a highly-ranked result must not be treated as more "authoritative" than
   its recorded trust level.
2. A caller writing to memory cannot claim a higher `AuthorityLevel` than it
   was granted, regardless of what it asserts in the write request.
3. An unreachable or unregistered provider must report an explicit
   `unavailable`/`unknown` status — never be silently treated as empty
   (which is indistinguishable from "provider is healthy, no results").

## Decision

Add a conformance test suite (`tests/test_context_provider_conformance.py`)
that pins these three properties as regression-tested contracts:

- `test_retrieval_is_advisory_and_preserves_score_and_rank`: confirms
  `MemoryPlane.search_ranked()` returns `score`/`rank` alongside each record
  without altering the record's `trust` field.
- `test_unverified_write_cannot_claim_verified_authority`: confirms
  `MemoryGate.write()` rejects a request whose declared `authority_level`
  exceeds what its `authority` is entitled to, returning
  `reason="authority_level_mismatch"` rather than silently downgrading it.
- `test_provider_outage_returns_explicit_unavailable_state`: confirms
  `AdapterRegistry.resolve()` on an unregistered provider returns
  `available=False, status="unknown"`, and that `ingest_many()` against an
  unreachable adapter reports `status="unavailable"` on both the summary and
  the per-adapter report — not a silently empty result set.

## Consequences

- These three properties are now regression-tested; a future change that
  weakens any of them fails CI immediately.
- Establishes the pattern (mirrors ADR-002's gate/orchestrator separation):
  retrieval intelligence proposes, it never silently substitutes for
  authorization or availability truth.

## Remainder closed (issue #287, 2026-08-27)

The two items this ADR originally left open are now closed:

- **Multi-adapter staleness windows.** `AdapterDescriptor.ttl_seconds`
  (optional, default `None`) declares a provider's freshness window.
  `MemoryPlane.search_ranked(..., ttl_lookup=...)` annotates each result's
  new `stale: bool` field by comparing the record's existing `created_at`
  against the declared TTL — computed purely from already-recorded data,
  never a live provider call. A provider with no declared TTL fails open:
  its records are never marked stale. This is deliberately a read-time
  annotation, not a background eviction process — it does not delete
  records or interact with the existing hard `expires_at` exclusion, which
  remains a separate, namespace-scoped write policy owned by
  `verdict/memory_gate.py`.
- **Conformance coverage beyond one adapter type.** The
  explicit-unavailable-state guarantee is now proven, by a single
  parametrized test enumerating the real registry
  (`build_default_adapter_registry().list()`), against every adapter type
  it ships. That enumeration discovered a fifth registered type,
  `code-graph-manifest`, that neither this ADR nor issue #287 had named —
  exactly the class of gap a hardcoded id list would have kept missing.
  The `masterdocs-sqlite` legacy refusal is now asserted by test
  (`test_masterdocs_sqlite_legacy_id_is_refused`), not only documented.
  No per-adapter code change was needed: the existing outage-reporting
  mechanism (`AdapterRegistry.declare_unavailable`) already generalized
  correctly to every real adapter type without modification.
