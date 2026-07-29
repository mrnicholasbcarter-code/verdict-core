# ADR-011: Keep OmniRoute catalog baseline separate from route qualification

- **Status:** Accepted — baseline and refresh evidence recorded in ADR-007/#153
- **Date:** 2026-07-29
- **Related:** [ADR-007](ADR-007-omniroute-catalog-qualification.md), [#106](https://github.com/mrnicholasbcarter-code/verdict-core/issues/106), [#153](https://github.com/mrnicholasbcarter-code/verdict-core/issues/153)

## Decision

The OmniRoute catalog is inventoried as identity and claimed-metadata
evidence, not execution eligibility. Every row remains tied to its provider
and route provenance; aliases are not collapsed into a model-family key.

Qualification records retain source endpoint, capture time, schema/version,
freshness deadline, payload and qualification hashes, row and unique-ID counts,
duplicate classifications, provider/capability/profile summaries, and
explicit status. The established policy baseline is 3,977 rows, 3,964 unique
IDs, 13 duplicate-row delta, and zero malformed rows for each documented
projection. A changed count remains `partial` until the expected-count policy
is independently reviewed; the policy is never silently updated from a
refresh.

Public and management projections are reconciled without retaining raw
payloads. Missing, stale, malformed, contradictory, or unavailable data is
`unknown` or `partial` and cannot promote a route. Bounded liveness results are
stored separately and do not turn a catalog record into a capability passport
or protected-work eligibility. Catalog claims, HTTP 200, model self-report,
and upstream generated metadata remain advisory.

## Consequences

- the complete 3,977-model inventory remains useful for future retrieval and
  qualification without overstating live support;
- passport, probe, strength, receipt, and promotion layers can use catalog
  records as inputs while retaining independent evidence authority;
- a future refresh must report policy deltas explicitly and preserve prior
  hashes/provenance for replay.

## Verification

ADR-007 and the sanitized evidence artifacts record the 2026-07-28 and
2026-07-29 snapshots, hashes, duplicate reconciliation, and bounded probe
outcomes. `verdict.omniroute_catalog` and its tests enforce freshness, schema,
duplicate, projection, hash, provenance, storage-idempotency, and bounded
probe behavior without storing raw catalog responses or credentials.
