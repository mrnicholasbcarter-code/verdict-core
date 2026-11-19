## ADDED Requirements

### Requirement: Identify SHALL read connection evidence from a valid connections snapshot
When `VERDICT_CONNECTIONS_SNAPSHOT` is set, identify SHALL validate it with the
same reader and freshness rule orchestrate uses and SHALL build connection
evidence from its rows without calling the management endpoints. Route
capacity SHALL come from the existing classification rules applied to those
rows.

#### Scenario: Restricted key with a valid snapshot
- **WHEN** every management read returns 401 and a valid snapshot is set
- **THEN** no management endpoint is called, routes are classified from the snapshot, and the FREE screen checks more than 0 routes

#### Scenario: Restricted key without a snapshot
- **WHEN** every management read returns 401 and no snapshot is set
- **THEN** every route's capacity is `unknown` and the FREE screen checks 0 routes

### Requirement: An invalid snapshot SHALL fail closed
A stale, tampered or missing snapshot SHALL raise `Invalid connections
snapshot` before any gateway read, and SHALL NOT fall back to management
reads or to any capacity class.

#### Scenario: Stale snapshot
- **WHEN** the snapshot was captured more than 6 hours ago
- **THEN** identify exits non-zero naming the invalid snapshot and sends no request

### Requirement: The preflight summary SHALL name failing evidence reads
The rehearsal preflight summary SHALL include `source_errors`, mapping each
failed source name to its HTTP status or exception class. Unknown source names
SHALL be dropped and unknown kinds SHALL be `unclassified`, so no URL, key or
body reaches `certification-failure.json`.

#### Scenario: Management reads rejected
- **WHEN** the screen reports `providers` failed with HTTP 401
- **THEN** the failure artifact holds `"source_errors": {"providers": "http_401"}` and no URL or key
