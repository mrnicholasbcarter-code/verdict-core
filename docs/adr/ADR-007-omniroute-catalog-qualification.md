# ADR-007: Qualify OmniRoute catalog identity separately from liveness

- **Status:** Accepted — implemented and merged in PR #155; refresh in #162 is
  diagnostic and remains partial under the recorded baseline policy
- **Date:** 2026-07-28
- **Decision owners:** Verdict Core maintainers
- **Ticket:** #153

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

The CLI fetches the public identity projection first and the management
projection as an explicit consistency check by default; either projection can
still be requested alone. Projection consistency never overrides freshness,
schema, or expected-count qualification.

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

The prior 2026-07-28 live instance returned 3,977 rows, 3,964 unique IDs, zero
malformed rows, and 13 duplicate-row deltas on both documented endpoints. A
fresh 2026-07-29 refresh observed 4,031 rows, 4,018 unique IDs, zero malformed
rows, and the same 13-row duplicate delta on both projections. Under the
unchanged 3,977-row expected-count policy, both snapshots are `partial` with
target delta `+54`; the expected policy is not silently changed. The public
payload hash is
`e5670120560aca1612298922abcfbc126d02ca80536a439abdbc9583f7f6f6bd`; the
management payload hash is
`e550cc4415dbb5cbd1d3f058c112b9df4a36276969ce44267ae63f4819b157cc`. The
projections reconcile consistently on identity, duplicate, provider,
capability, profile, and bounds fields. Sanitized evidence is in
`docs/evidence/omniroute-catalog-qualification-2026-07-29.json`; raw payloads
remain outside the repository. No liveness probe was run in this refresh, so
no provider liveness claim is made.

## Consequences

Catalog review can cover all models without hardcoded provider lists, while
protected work remains safe when runtime truth is missing. The 2026-07-29
refresh is retained as partial diagnostic evidence until the expected-count
change is independently reviewed. A later ticket may approve a new baseline or
add an authenticated management liveness route; that work must retain the same
fail-closed and privacy boundaries.
