# ADR-007: Qualify OmniRoute catalog identity separately from liveness

- **Status:** Accepted
- **Date:** 2026-07-28
- **Decision owners:** Verdict Core maintainers
- **Ticket:** #153
- **Completion:** Implemented and merged by PR #155; refreshed after PR #157 and
  verified after PR #159. Ticket #153 is complete; future catalog refreshes
  remain runtime evidence only.

The latest shared catalog records remain qualified snapshots of 3,977 rows,
3,964 unique IDs, and a 13-row duplicate delta on both documented endpoints.
The public snapshot hash is
`eaaeb37b9b01771f904398668ec04a27f90228fa812004a172fbd640da59548c`; the
management snapshot hash is
`25e568789598bbdd80cbcca3eae0b07906f3e2b4101cccb661a6e095a73ab2f1`. The
sanitized committed evidence hash is
`1ee9e5288c6778b6f30a1a755b89f1028fa90a3e56ba6a49be0b410e682b8d43`. These
hashes identify different capture encodings and must not be conflated.

## Context

OmniRoute exposes a large OpenAI-compatible model catalog. A catalog row is
useful identity and capability evidence, but it does not prove that a provider
is reachable, authorized, within quota, or suitable for protected work. The
public `/v1/models` projection and the management `/api/models/catalog`
projection also contain duplicate model IDs and can drift in schema or
freshness.

## Decision

Verdict qualifies catalog snapshots as sanitized, deterministic summaries. It
records the endpoint, schema/version, capture time and freshness deadline,
SHA-256 payload hash, row/unique-ID/malformed counts, every duplicate-ID
classification, provider and capability counts, context/output bounds, and
explicit profile evidence. The public endpoint is the default identity source;
the management endpoint is an explicit fallback.

Catalog qualification is fail-closed: malformed schema is `unknown`, stale
data is `stale`, malformed or row-count drift is `partial`, and only a fresh
complete snapshot is `qualified`. Raw catalog responses are never committed or
stored in shared memory.

Liveness is a separate, explicitly bounded operation. `ProbeRunner` selects at
most 16 deterministic, provider-diverse IDs and sends only its fixed one-token
probe. Results preserve ready, unavailable/error, unauthorized, timeout,
quota/rate-limit, skipped, and malformed/usage-unavailable distinctions while
redacting response bodies and credentials. A liveness sample cannot promote a
catalog-only record to readiness; protected routing continues to require the
existing availability policy.

Qualified summaries may be stored in shared `~/.verdict/memory.db` under
`omniroute-catalog`, with the payload and qualification hashes, schema,
capture/freshness provenance, and deterministic idempotency identity. Stale or
partial snapshots may be retained with zero confidence for diagnostics; schema
unknown responses cannot be stored.

## Evidence

The 2026-07-28 live instance returned 3,977 rows, 3,964 unique IDs, zero
malformed rows, and 13 duplicate-row deltas on both documented endpoints. The
public and management qualified records are active in shared memory with
freshness and provenance metadata; raw JSON remains outside the repository.
The bounded eight-model sample recorded zero ready results (one HTTP error and
seven timeouts in the initial run; a later refresh recorded one rate limit and
six timeouts), so no provider liveness claim is made.

## Consequences

Catalog review can cover all models without hardcoded provider lists, while
protected work remains safe when runtime truth is missing. Live probing is
bounded and auditable, but it is only a sample and is not a complete
3,977-model availability certification. A later ticket may add an authenticated
management liveness route or a scheduled qualification report; that work must
retain the same fail-closed and privacy boundaries.
