# Restricted gateway connections evidence

## Why
Inference-scoped keys can read models but cannot read admin provider inventory.
Sending an admin key through a public certification tunnel exposes credentials.

## What Changes
- Capture sanitized connection evidence through loopback-only gateway administration.
- Validate a versioned, SHA-256-bound snapshot with a six-hour freshness limit.
- Use the snapshot in the existing fetch_connections path without an HTTP fallback.
- Require a masked protected snapshot secret for certification rehearsals.

## Impact
Admission and ranking authority stay unchanged. Unknown evidence stays unknown.
The digest detects corruption; it is not a signature or admission authority.
No new dependencies, provider calls in tests, or gateway configuration changes.
