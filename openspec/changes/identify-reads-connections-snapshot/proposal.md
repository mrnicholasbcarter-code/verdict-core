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

## Impact

- `verdict/omniroute_evidence.py`, `verdict/identify.py`,
  `scripts/run_certification_rehearsals.py`.
- No change to admission, ranking, review, capacity rules or preflight bounds.
