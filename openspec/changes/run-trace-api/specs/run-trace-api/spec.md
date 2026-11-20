## ADDED Requirements

### Requirement: Run ids SHALL be validated before any path is resolved

A `run_id` path parameter MUST match `^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$` and
MUST NOT be `.` or `..`.  The resolved run directory MUST be a direct child of
the configured runs root; any candidate that resolves outside the root MUST be
rejected with 404.

#### Scenario: Traversal component in run id

- **GIVEN** a request with `run_id` containing `..` or `/`
- **THEN** the server returns 404 and performs no filesystem access

#### Scenario: Valid run id that does not exist

- **GIVEN** a syntactically valid `run_id` that does not name a directory under the runs root
- **THEN** the server returns 404

### Requirement: Run trace endpoints SHALL be read-only

`GET /v1/runs`, `GET /v1/runs/{run_id}/events` and `GET /v1/runs/{run_id}/receipt`
MUST NOT create, modify or delete any file.  `EventLog` MUST NOT be instantiated
by these handlers; events SHALL be read by opening `events.jsonl` directly.

#### Scenario: Torn trailing line in events.jsonl

- **GIVEN** `events.jsonl` ends with a partial (non-decodable) JSON line
- **THEN** the events endpoint returns all complete events before it; no file is written

#### Scenario: No runs directory

- **GIVEN** `VERDICT_RUNS_DIR` names a path that does not exist
- **THEN** `GET /v1/runs` returns 200 with an empty `runs` list

### Requirement: GET /v1/runs SHALL list runs bounded by page size

`GET /v1/runs` MUST return at most `limit` runs (default 20, maximum 100).
Runs MUST be sorted by `started_at` descending.  For each run: `run_id`,
`started_at`, `outcome` and `receipt_status` (`present` | `missing` |
`unreadable`).

#### Scenario: Multiple runs

- **GIVEN** a runs root containing several run directories
- **THEN** the response lists them newest-first up to the limit

### Requirement: GET /v1/runs/{run_id}/events SHALL paginate with a byte budget

The events endpoint MUST accept `after_seq`, `seq_to`, `node_id` (repeatable),
`type` (repeatable) and `limit` (default 100, maximum 500) query parameters.
The response MUST include at most 1 MiB of serialised events.  The response
MUST include `next_seq` (the seq after the last returned event, or null if
exhausted) and `truncated` (true when the byte budget was reached before the
page limit).

#### Scenario: Filter by type

- **GIVEN** a run with events of mixed types
- **WHEN** `type=run_started` is supplied
- **THEN** only events whose `type` field equals `run_started` are returned

#### Scenario: Pagination

- **GIVEN** a run with more events than the requested limit
- **WHEN** `limit=1` is supplied
- **THEN** the response contains exactly one event and `next_seq` points to the
  next event

### Requirement: GET /v1/runs/{run_id}/receipt SHALL verify the receipt digest

The receipt endpoint MUST call `verify_run_receipt` on the run directory and
include the result in the response as `verification.ok` (bool) and
`verification.problems` (list of strings).  It MUST NOT modify `receipt.py`
internals.

#### Scenario: Intact receipt

- **GIVEN** `receipt.json` and `events.jsonl` are consistent
- **THEN** `verification.ok` is true and `verification.problems` is empty

#### Scenario: Tampered events.jsonl

- **GIVEN** `events.jsonl` has been modified after `receipt.json` was written
- **THEN** `verification.ok` is false and `verification.problems` contains an
  `events_digest mismatch` entry

### Requirement: Run trace endpoints SHALL require bearer authentication

All three endpoints MUST be covered by the existing `caller_authentication`
middleware.  A request without a valid `Authorization: Bearer <token>` header
MUST receive 401.

#### Scenario: Missing auth header

- **GIVEN** `LLMGATE_AUTH_TOKEN` is set and no `Authorization` header is sent
- **THEN** the server returns 401 with `www-authenticate: Bearer`

#### Scenario: Wrong token

- **GIVEN** a bearer token that does not match `LLMGATE_AUTH_TOKEN`
- **THEN** the server returns 403
