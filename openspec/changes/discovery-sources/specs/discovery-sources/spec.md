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

### Requirement: `collect_sources` SHALL swallow per-adapter failures
If any adapter raises, `collect_sources` SHALL continue and return rows from
the remaining adapters.  No exception SHALL propagate to the caller.

#### Scenario: One failing adapter does not block others
- **WHEN** the OpenRouter adapter raises and other adapters succeed
- **THEN** `collect_sources` returns rows from the succeeding adapters only

### Requirement: `refresh_discovery_sources.py` SHALL write a cache index
The script SHALL write `{source}.json` per enabled source and `_index.json`
summarising row counts and errors to `$VERDICT_HOME/discovery/sources/`.

#### Scenario: Index is written on success
- **WHEN** `run()` is called with one enabled source that returns rows
- **THEN** `_index.json` exists and its `total_rows` equals the row count
