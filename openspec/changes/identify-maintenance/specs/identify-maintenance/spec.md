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
