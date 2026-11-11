## ADDED Requirements

### Requirement: Repair renews documents inside the renewal margin
`run_documentation_preflight(fix=True)` SHALL re-ingest every manifest that is still fresh but has
less than `renew_margin_seconds` (default 12 hours) before its expiry, and SHALL leave a manifest
with more than the margin remaining untouched.

#### Scenario: Fresh document outside the margin
- **WHEN** a repair runs and a document has more than 12 hours left
- **THEN** it is skipped as fresh and its expiry does not change

#### Scenario: Fresh document inside the margin
- **WHEN** a repair runs and a still-fresh document has under 12 hours left
- **THEN** it is re-ingested, reported as `renewed` (not `stale`), and expires 24 hours from now

#### Scenario: Expired document
- **WHEN** a repair runs and a document has expired
- **THEN** it is re-ingested and reported as `stale`

### Requirement: The read-only documentation check stays strict
`run_documentation_preflight(fix=False)` SHALL ignore the renewal margin. An expired document
SHALL be `stale` and a document with no manifest SHALL be `missing`; either blocks the preflight.

#### Scenario: Missing document
- **WHEN** doctor checks a document that was never ingested
- **THEN** the preflight is blocked with state `missing`

#### Scenario: Inside the margin but unexpired
- **WHEN** doctor checks a document with under 12 hours left
- **THEN** the preflight passes and nothing is written

### Requirement: Explicit six-hourly documentation refresh timer
`verdict memory docs --install-service` SHALL install a oneshot service running
`verdict memory docs --fix --repo-root <installing checkout>` and a timer firing every six hours.
`--dry-run` SHALL write nothing and run no `systemctl`; `--uninstall` SHALL remove only these
units. The timer SHALL be planned by `verdict setup` as `host.docs-refresh-timer` and reported by
`verdict doctor`.

#### Scenario: Dry run
- **WHEN** the operator runs `verdict memory docs --install-service --dry-run`
- **THEN** the rendered units are printed and no file or unit is touched
