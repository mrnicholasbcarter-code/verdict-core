## ADDED Requirements

### Requirement: Explicit passive-first maintenance installation
The system SHALL install only the identify passive and screen unit pairs when the operator confirms identify maintenance in setup or invokes `identify install-service`. It SHALL NOT install or activate the retired prove-at-rest or census units.

#### Scenario: Install and remove the replacement
- **WHEN** the operator explicitly installs identify maintenance
- **THEN** a passive oneshot and three-hour timer plus a separate daily bounded screen oneshot and timer are installed
- **AND** uninstall disables the new units and removes only their files

### Requirement: Passive snapshot is private and explicit
Ordinary passive identification SHALL perform zero inference and no persistence. `--save-report` SHALL atomically replace `identify-latest.json` under Verdict home with mode 0600.

#### Scenario: Scheduled passive report
- **WHEN** the passive timer runs `identify --passive --json --save-report`
- **THEN** the complete passive JSON is atomically saved and printed without inference

### Requirement: Daily screening preserves consent and approved bounds
Scheduled screening SHALL use the existing missing-proof screen with explicit live-probe consent, free/subscription capacity, at most one route per provider and eight routes overall. Reinstalling unchanged units SHALL NOT manually start an extra screen run.

#### Scenario: Screen timer fires
- **WHEN** the daily timer runs
- **THEN** `identify --screen --allow-live-probe --capacity free,subscription --max-per-provider 1 --max-routes 8 --json` executes
- **AND** fresh proof remains skipped by the existing screen

### Requirement: Doctor reports replacement health honestly
Doctor SHALL fail if either identify timer is inactive/missing/failed or the passive report is missing, invalid, future-dated, at least six hours old, or has source errors. It SHALL retain the health-cache missing/unreadable/no-usable-route checks using evidence TTL and grace, not a five-minute producer cadence.

#### Scenario: Report is stale
- **WHEN** an active timer's latest passive report is at least twice the passive interval old
- **THEN** doctor reports a named failed identify report row

#### Scenario: Evidence remains usable without the retired prober
- **WHEN** a healthy cache entry is 601 seconds old but still inside its evidence lifetime
- **THEN** it is not rejected solely due to the retired prober cadence

### Requirement: Passive inventory separates management and inference authentication
Passive identify SHALL use the optional management read token only for the five management `/api` GETs and the resolved inference API key only for `/v1/models` and `/v1/auto-combo/auto/candidates`. It SHALL retain seven fixed source slots and attempt at most seven GETs under the shared aggregate deadline. Missing credentials SHALL make only their source slots locally unavailable without network access; management reads SHALL proceed when inference credentials are missing, while both credential families missing SHALL make all seven sources unavailable. When the management token is absent, the legacy `api_key` SHALL remain the management credential fallback, but never after a 401 or other request failure. It SHALL NOT retry a failed request with the other authentication family. The management resolver SHALL use stripped `OMNIROUTE_MANAGEMENT_TOKEN` from the environment or the same-named existing secure-store field only; it SHALL never use inference compatibility files or the credentials registry. Existing permission checks are mode checks and SHALL NOT be described as ownership or symlink protection. No new secret file SHALL be created.

#### Scenario: Split credentials stay on their endpoint families
- **WHEN** passive identify fetches its inventory sources with both credentials available
- **THEN** the five `/api` requests carry only the management read token
- **AND** the `/v1/models` and `/v1/auto-combo/auto/candidates` requests carry only the inference API key
- **AND** exactly seven bounded GETs are attempted

#### Scenario: Legacy management fallback remains supported
- **WHEN** no management read token is resolved and the legacy `api_key` is available
- **THEN** the five management requests use `api_key`
- **AND** `/v1/models` and `/v1/auto-combo/auto/candidates` still use only the inference API key
- **AND** a management-token 401 does not trigger a retry with `api_key`

#### Scenario: Missing inference credential is truthful
- **WHEN** no inference API key is resolved
- **THEN** `/v1/models` and `/v1/auto-combo/auto/candidates` are recorded as source errors or unavailable sources
- **AND** management reads do not receive a management token as an inference substitute
- **AND** an empty or deny-all inference catalog remains an honest empty/unknown result rather than evidence of an available route

#### Scenario: Authentication failures do not cross-retry
- **WHEN** a management or inference request fails authentication
- **THEN** that source is recorded as failed without retrying it with the other credential
- **AND** no additional request is made beyond the seven bounded GET contract

#### Scenario: Passive inventory has no writes or inference calls
- **WHEN** passive identify runs
- **THEN** it performs only the seven bounded inventory GETs
- **AND** it performs no POST, write, model-generation, or `/admin/health` compatibility call
- **AND** redirects, per-read and aggregate deadlines, decoded-byte limits, raw-row limits, and source-specific error truth remain unchanged
