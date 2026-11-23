## Why

The certification gateway key has only the `self:usage` scope (the #807
design). The identify screen read its connection, quota and cooldown evidence
from five management endpoints, which that key cannot read. Every route's
capacity became `unknown`, `--capacity free` dropped all of them, and the
GitHub certification preflight checked 0 routes. Orchestrate already reads
the sanitized `VERDICT_CONNECTIONS_SNAPSHOT` the workflow supplies; identify
did not.

## What Changes

- When `VERDICT_CONNECTIONS_SNAPSHOT` is set, `fetch_omniroute_evidence` reads
  it with `read_connections_snapshot` (the reader and 6-hour freshness rule
  orchestrate uses) and does not call the management endpoints. Inference
  reads (`/v1/models`, candidates) are unchanged.
- An invalid, stale or missing snapshot raises the existing
  `Invalid connections snapshot` error. There is no fallback.
- Snapshot rows go through the existing usability and capacity rules. A
  connection with a current rate-limit window is not usable; quota stays
  unknown.
- identify reports `source_error_kinds` (HTTP status or exception class only),
  and the rehearsal preflight summary copies them into
  `certification-failure.json` as `source_errors` through a closed vocabulary.

## Capabilities

### Modified Capabilities

- **identify**: when `VERDICT_CONNECTIONS_SNAPSHOT` is set, reads connection
  evidence from the snapshot instead of calling management endpoints. The
  snapshot is validated with the same reader and freshness rule that
  `orchestrate` uses.
- **preflight summary**: includes `source_errors` mapping each failed source
  name to its HTTP status or exception class only; no URL or key is forwarded.

## Impact

- `verdict/omniroute_evidence.py`, `verdict/identify.py`,
  `scripts/run_certification_rehearsals.py`.
- No change to admission, ranking, review, capacity rules or preflight bounds.

## Evidence boundary

KNOWN: the failing tests (1.1, 1.3) were red on the unfixed code and green on
the fix; the snapshot reader and freshness rule are shared with `orchestrate`.

INFERRED: `source_errors` closed-vocabulary enforcement prevents URL or key
leakage; the vocabulary is defined in the implementation and tested in 1.3.

NOT AVAILABLE: end-to-end rehearsal confirmation that `certify-rehearsal`
preflight checks more than 0 routes (tracked as a post-merge task).

## Acceptance criteria

- Restricted key plus a valid snapshot: no management endpoint is called,
  routes are classified from the snapshot rows, and the FREE screen checks
  more than 0 routes.
- No snapshot set: every route's capacity is `unknown` and the FREE screen
  checks 0 routes.
- Stale, tampered or missing snapshot: `identify` exits non-zero naming the
  invalid snapshot; no management read is attempted.
- `source_errors` in the preflight summary holds source names and kinds only;
  no URL or key is present.
- Tests for all four scenarios were red on the unfixed code and green on the fix.
