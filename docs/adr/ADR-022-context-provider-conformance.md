# ADR-022: Context Provider Conformance Suite

**Status**: Accepted (partially implemented)
**Date**: 2026-08-16
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
- This suite covers the fail-closed core of CTX-002, not its full acceptance
  criteria. Multi-adapter staleness windows, TTL-based cache invalidation
  across providers, and adapter-specific conformance (beyond
  `masterdocs-sqlite`) remain open — tracked separately so this ADR does not
  overstate completion.
- Establishes the pattern (mirrors ADR-002's gate/orchestrator separation):
  retrieval intelligence proposes, it never silently substitutes for
  authorization or availability truth.
