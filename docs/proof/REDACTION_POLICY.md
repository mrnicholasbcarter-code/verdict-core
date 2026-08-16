# Public proof redaction policy

Status: active for the 2026-07-31 claims freeze.

The public proof matrix is a traceability artifact, not a raw telemetry dump.
It records enough information to reproduce a claim from checked-in source,
fixtures, tests, workflow definitions, and sanitized evidence while excluding
credentials, private endpoints, account identifiers, prompts, tool output, and
local machine paths.

## Classification

| Class | Public handling | Examples |
| --- | --- | --- |
| Public | May be linked directly | source paths, test names, commit IDs, schema versions, sanitized aggregate counts |
| Observed | May be summarized with date, scope, and limitations | catalog row counts, bounded liveness outcomes, fixture digests |
| Private | Never committed; replace with a digest or abstract reference | bearer tokens, account IDs, private URLs, raw provider payloads, local database paths |
| Sensitive | Publish only a redacted aggregate and document the transformation | prompts, tool/resource contents, request IDs, financial/trading records, user data |
| Unsupported | Do not promote to verified wording | un-reproduced speed, quality, adoption, or production-readiness claims |

## Required transformations

1. Replace endpoints and credential-bearing values with a stable `sha256:` digest
   or a public protocol label. Never publish an authorization header or token.
2. Hash prompts, raw tool/resource content, session identifiers, and account
   identifiers before they enter an evidence artifact. Prefer counts and
   classifications over hashes when even a hash could identify a subject.
3. Preserve the observation date, scope, source class, freshness, and
   limitations. Redaction must not turn an unknown or partial result into a
   passing result.
4. Keep source-relative paths only. Do not publish home-directory paths,
   private database filenames, environment dumps, or shell history.
5. For financial or trading evidence, publish methodology and aggregate
   outcomes only after removing account, order, position, and credential data.
6. Mark external or self-reported evidence as such. A narrative, screenshot,
   or claim-source document is not a substitute for a reproducible artifact.

## Review controls

- `scripts/verify_proof_matrix.py` rejects absolute/traversal paths, missing
  evidence, invalid statuses, stale review metadata, and common secret-bearing
  patterns in the public JSON ledgers.
- Every `verified` or `observed` entry must name evidence and a falsification
  test. Every blocked or unsupported entry must name its missing evidence and a
  downgrade wording.
- Claims are frozen on the date in each ledger. A later release must update the
  freeze date, re-run the verifier, and review changed claims before publishing.
- Raw evidence can remain in an access-controlled external system, but the
  public ledger must contain its redacted substitute and provenance boundary.

## Non-goals

This policy does not authorize publication of secrets, grant evidence authority
to an untrusted source, or certify that a provider, host, or portfolio claim is
production-ready.
