# Spec Delta: Discovery prober and registry

## ADDED Requirements

### Requirement: Prober SHALL only probe FREE and SUBSCRIPTION routes

`verdict discovery refresh` SHALL only probe routes whose capacity class is
`"free"` or `"subscription"` as decided by a capacity fact.  Routes classified
`"unknown"` or `"priced"` MUST NOT be probed, so no paid provider calls are
ever made by the prober.

#### Scenario: Priced route is not probed

- **GIVEN** a route whose capacity_class is `"priced"`
- **WHEN** `verdict discovery refresh` is called
- **THEN** no HTTP request is made to that route and it does not appear in the
  registry

#### Scenario: Unknown route is not probed

- **GIVEN** a route whose capacity_class is `"unknown"`
- **WHEN** `verdict discovery refresh` is called
- **THEN** no HTTP request is made to that route and it does not appear in the
  registry

### Requirement: Registry SHALL be written atomically with a 12-hour TTL

The registry file at `$VERDICT_HOME/discovery/registry.json` SHALL be written
by writing a temporary file first and then calling `os.replace()` so that
readers never observe a torn file.  The registry SHALL carry a `ttl_s` field of
43200 (12 hours).  A registry older than its `ttl_s` MUST be treated as missing
(returns `None` from `load_registry`).

#### Scenario: Stale registry reads as None

- **GIVEN** a registry file whose `written_at` is more than 12 hours ago
- **WHEN** `load_registry` is called
- **THEN** `None` is returned

#### Scenario: No .tmp files remain after save

- **GIVEN** `save_registry` completes successfully
- **THEN** no `.tmp` file remains in `$VERDICT_HOME/discovery/`

### Requirement: Per-pool circuit breaker SHALL open on 429

When a probe returns `RATE_LIMITED` (HTTP 429), the pool identified by
`upstream` (for OmniRoute routes) or `provider` (for direct routes) SHALL be
marked open for the remainder of the refresh run.  No further routes in that
pool SHALL be probed in the same run.

#### Scenario: 429 opens the pool breaker

- **GIVEN** three routes in the same provider pool
- **AND** the first probe returns 429
- **WHEN** `run_refresh` processes the remaining routes
- **THEN** the remaining routes in that pool appear in the registry with
  `breaker_state="open"` and `answered=false`

#### Scenario: Breaker does not bleed across pools

- **GIVEN** pool A returns 429 and pool B returns 200
- **THEN** pool B routes appear with `answered=true` and `breaker_state="closed"`

### Requirement: Tool probe SHALL pad context to agent size

The tool probe SHALL include a system message padding the context to at least
18 000 tokens before the task message.  Padding ensures the probe exercises the
model at the token depth used by agentic workloads.

#### Scenario: Context padding present in tool probe request

- **GIVEN** `tool_probe` is called with `context_tokens=18000`
- **THEN** the outgoing request includes a system message whose content is at
  least 18 000 * 4 / 2 characters long

### Requirement: Transport SHALL be injectable for offline tests

`verdict/discovery/probe.py` SHALL expose `set_transport(fn)` and restore the
real urllib backend when `fn=None` is passed.  Tests MUST use this mechanism;
no test MAY make a real network call.

#### Scenario: Injectable transport called by probe_one

- **GIVEN** a custom transport is installed via `set_transport`
- **WHEN** `probe_one` is called
- **THEN** the custom transport function is invoked and no real HTTP call is made

### Requirement: Credentials SHALL NOT appear in probe detail fields

When a route's auth carries a secret (literal token or env-var value), that
secret MUST be sanitized out of `ProbeResult.detail` and `RegistryEntry.auth`.
`RegistryEntry.auth` for a `literal:xxx` credential MUST be stored as
`"literal"`, not `"literal:xxx"`.

#### Scenario: Literal secret does not appear in detail

- **GIVEN** a route with `auth="literal:supersecret"`
- **AND** the transport raises an exception containing the literal value
- **THEN** `ProbeResult.detail` does not contain `"supersecret"`

#### Scenario: Registry auth field is sanitized

- **GIVEN** a route with `auth="literal:supersecret"`
- **WHEN** a registry entry is built for that route
- **THEN** `RegistryEntry.auth` is `"literal"` not `"literal:supersecret"`

### Requirement: `verdict discovery refresh` SHALL accept --dry-run

When `--dry-run` is passed, `run_refresh_action` SHALL return immediately with
`source="dry_run_requires_catalog"` and an empty entry list.  It SHALL NOT make
any network call and SHALL NOT write the registry file.  The OmniRoute catalog
requires a live fetch and is therefore unavailable offline.

#### Scenario: Dry-run makes no network calls and writes no file

- **GIVEN** `--dry-run` is passed
- **WHEN** `run_refresh_action` returns
- **THEN** no HTTP transport call is made, no registry file is written to disk,
  and `result["source"] == "dry_run_requires_catalog"`
