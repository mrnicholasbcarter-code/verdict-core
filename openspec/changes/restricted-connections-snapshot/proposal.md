# Restricted gateway connections evidence

## Why
Inference-scoped keys can read models but cannot read admin provider inventory.
Sending an admin key through a public certification tunnel exposes credentials.

## What Changes
- Capture sanitized connection evidence through loopback-only gateway administration.
- Validate a versioned, SHA-256-bound snapshot with a six-hour freshness limit.
- Use the snapshot in the existing fetch_connections path without an HTTP fallback.
- Require a masked protected snapshot secret for certification rehearsals.

## Capabilities

### New Capabilities

- **connections snapshot**: loopback-only snapshot capture writes a
  versioned, SHA-256-bound JSON file of sanitised connection evidence with a
  six-hour freshness limit. The snapshot is consumed by the existing
  `fetch_connections` path without an HTTP fallback.

## Impact

Admission and ranking authority stay unchanged. Unknown evidence stays unknown.
The digest detects corruption; it is not a signature or admission authority.
No new dependencies, provider calls in tests, or gateway configuration changes.

## Evidence boundary

KNOWN: the snapshot is captured through loopback-only gateway administration;
the digest is SHA-256-bound and checked on every read; the freshness limit is
6 hours; the schema is validated on write and read.

INFERRED: masking the protected snapshot secret in rehearsals prevents
credential exposure; the CI workflow only forwards the masked value.

NOT AVAILABLE: long-term operational evidence that the 6-hour freshness window
is sufficient for all certification workflows.

## Acceptance criteria

- Loopback-only snapshot capture with private atomic writes completes without
  exposing credentials.
- The snapshot is validated against schema, freshness, payload digest and
  sanitised evidence on every read.
- Certification rehearsals require a masked protected snapshot secret and fail
  if it is absent.
- Regression tests were red on the unfixed code and green on the fix.
