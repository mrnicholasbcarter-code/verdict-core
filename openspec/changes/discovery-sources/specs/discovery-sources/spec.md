# Spec Delta: Discovery sources (S2)

## ADDED Requirements

### Requirement: Discovery adapters SHALL return only facts, never capacity decisions
Each source adapter SHALL return `DiscoveryRow` objects.  A row SHALL record
`id`, `provider`, `source`, `source_url`, `fetched_at`, and optionally
`input_price`, `output_price`, `free_flag`, `context_length`,
`supports_tools`, `license`, `license_restricted`, `commit`, and `repo`.
No row SHALL record an admission decision, a capacity tier, or a health state.

#### Scenario: OmniRoute adapter returns rows from evidence
- **WHEN** `rows_from_evidence` is called with an `OmniRouteEvidence` object
- **THEN** it returns one `DiscoveryRow` per non-opaque route in the inventory
- **AND** opaque routes (`auto/*`, `combo/*`) are excluded

#### Scenario: Kilo `isFree` flag is reflected in `free_flag`
- **WHEN** a Kilo model carries `"isFree": true`
- **THEN** its `DiscoveryRow` has `free_flag=True`

### Requirement: Adapters SHALL be testable offline via an injectable Fetcher
The `Fetcher` protocol (`(url, *, timeout, max_bytes, headers) -> bytes`) SHALL
be accepted by every network adapter.  `make_stub_fetcher` SHALL return an
offline fetcher from a `{url: response}` dict so tests make no real HTTP calls.

#### Scenario: Stub fetcher serves pre-built responses
- **WHEN** `make_stub_fetcher({"https://example.com": b"data"})` is called
- **THEN** fetching `https://example.com` returns `b"data"` without network I/O

### Requirement: The FCM JavaScript parser SHALL never evaluate JS
The FCM `sources.js` parser SHALL use only regex matching.  It SHALL reject
any array body that contains `{`, `(`, or a backtick, failing closed with an
empty list rather than raising.

#### Scenario: Hostile JS body is rejected silently
- **WHEN** the FCM source file contains a function call or object literal in an array
- **THEN** `parse_fcm` returns an empty list and raises no exception

### Requirement: `collect_sources` SHALL record per-adapter failures as errors
If any adapter raises, `collect_sources` SHALL continue and collect rows from
the remaining adapters.  No exception SHALL propagate to the caller.  Each
failure SHALL be recorded as a `SourceError` in `CollectResult.errors` with
the source name, exception class name, and a short message (no secrets).  A
failed source SHALL NOT appear as an empty-success entry in `CollectResult.rows`;
empty rows and a recorded error are mutually exclusive per source.

#### Scenario: One failing adapter does not block others
- **WHEN** the OpenRouter adapter raises and other adapters succeed
- **THEN** `collect_sources` returns a `CollectResult` with rows from the
  succeeding adapters and an error entry naming the failed source

#### Scenario: Failed source is recorded, never treated as empty success
- **WHEN** an adapter raises
- **THEN** `CollectResult.errors` contains one entry for that source
- **AND** `CollectResult.rows` contains no rows with that `source` value

### Requirement: `refresh_discovery_sources.py` SHALL write cache files atomically
The script SHALL write `{source}.json` and `_index.json` via a temporary file
in the same directory followed by `os.replace`, so a reader never sees a
partial write.  Failed sources SHALL appear in the index with a non-null
`error` field; they SHALL NOT appear as `row_count: 0` with `error: null`.

#### Scenario: Index records a source error
- **WHEN** `run()` is called and one network adapter fails
- **THEN** `_index.json` has an entry for that source with a non-null `error`

#### Scenario: Index is written on success
- **WHEN** `run()` is called with one enabled source that returns rows
- **THEN** `_index.json` exists and its `total_rows` equals the row count

### Requirement: Stale cache files SHALL be removed when a source fails
When a source adapter fails, `run()` SHALL remove the previous `{source}.json`
cache file (if present) before writing `_index.json`.  A reader that sees a
non-null `error` in `_index.json` SHALL therefore find no corresponding cache
file; it must not treat stale rows as current data.

#### Scenario: Stale cache removed on failure
- **WHEN** a source succeeds (cache written), then the same source fails on a later run
- **THEN** the cache file is gone after the failed run
- **AND** the index entry has a non-null `error` and `row_count: 0`

### Requirement: `_index.json` entries SHALL share a uniform schema
Every entry in the `sources` array SHALL carry exactly the keys `source`,
`row_count`, `error`, `free_tier_warning`, and `fetched_at`.  Absent fields
SHALL be `null`, never omitted, so consumers need no field-presence checks.

#### Scenario: All entries share the same keys
- **WHEN** `run()` is called with both a succeeding and a failing source
- **THEN** every `sources` entry in `_index.json` has the same set of keys

### Requirement: Adapter failure logs SHALL not dump full tracebacks
On any adapter failure the script SHALL log only the exception type name and a
message truncated to at most 200 characters to stderr.  It SHALL NOT call
`traceback.print_exc` or equivalent; full stack frames can expose auth
credentials that appear in exception messages or locals.

#### Scenario: No traceback on adapter failure
- **WHEN** `run()` is called and an adapter raises
- **THEN** stderr does not contain "Traceback (most recent call last):"
- **AND** long exception messages are truncated before being logged
