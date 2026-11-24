# Spec Delta — headline-metric-report

## ADDED Requirements

### Requirement: `verdict metrics --daily` reports accepted work per paid model share

`verdict metrics --daily` SHALL aggregate verified receipts from the configured
runs directory and emit the M0 headline metric: accepted tasks divided by the
fraction of model attempts that used paid routes (subscription or metered).
The command SHALL complete with exit code 0 even when the runs directory is
empty or no paid model attempts exist in the selected period.

#### Scenario: Normal case — paid attempts and accepted work exist

- **GIVEN** one or more verified receipts in the runs directory, at least one
  model attempt on a paid route (subscription or metered), and at least one
  accepted task
- **WHEN** `verdict metrics --daily` is run
- **THEN** `headline_accepted_per_paid_share` is a non-negative float
- **AND** `headline_accepted_per_paid_share_note` is null
- **AND** the command exits 0

#### Scenario: Paid attempts exist but zero accepted tasks

- **GIVEN** one or more verified receipts in the runs directory, at least one
  model attempt on a paid route, but zero accepted tasks (all runs BLOCKED or
  FAIL_CLOSED)
- **WHEN** `verdict metrics --daily` is run
- **THEN** `headline_accepted_per_paid_share` is 0.0 (not null — the
  denominator is non-zero; the numerator is zero)
- **AND** `headline_accepted_per_paid_share_note` is null
- **AND** the command exits 0

#### Scenario: No paid model attempts — denominator is zero

- **GIVEN** one or more verified receipts in the runs directory, all model
  attempts on free routes
- **WHEN** `verdict metrics --daily` is run
- **THEN** `headline_accepted_per_paid_share` is null (never a fabricated number)
- **AND** `headline_accepted_per_paid_share_note` is a non-empty string
  explaining why the value is unavailable
- **AND** the command exits 0

#### Scenario: Empty runs directory

- **GIVEN** the runs directory exists but contains no receipts
- **WHEN** `verdict metrics --daily` is run
- **THEN** `headline_accepted_per_paid_share` is null
- **AND** `runs_processed` is 0
- **AND** the command exits 0

#### Scenario: Missing data reported as missing, never zero-filled

- **GIVEN** receipts where some attempts have unknown cost (dollars = null)
- **WHEN** `verdict metrics --daily --json` is run
- **THEN** the `dollars` field is null, not 0
- **AND** `has_unknown_cost` is true
- **AND** `tasks_per_dollar` is null, not 0

### Requirement: Fixture runs are excluded from the live aggregate

Runs whose `run_id` begins with `test-` SHALL be excluded from the headline
metric aggregate.  The count of excluded fixture runs SHALL be reported
separately as `fixture_runs_excluded`.

#### Scenario: Fixture run present alongside live run

- **GIVEN** a runs directory containing one `test-*` run and one live run
- **WHEN** `verdict metrics --daily` is run
- **THEN** `fixture_runs_excluded` is 1
- **AND** `runs_processed` reflects only the live run

### Requirement: `--since DATE` filters runs by start date

When `--since YYYY-MM-DD` is supplied, only runs whose `started_at` falls on
or after that date SHALL be included.  Runs with unparseable `started_at` SHALL
be included (conservative: include rather than silently drop).

#### Scenario: Date filter applied

- **GIVEN** two receipts: one from yesterday, one from today
- **WHEN** `verdict metrics --since <today>` is run
- **THEN** only today's run is counted

### Requirement: `verdict metrics --daily --json` outputs valid JSON

The `--json` flag SHALL cause the command to print a single JSON object to
stdout.  The object SHALL include at minimum: `headline_accepted_per_paid_share`
(float or null), `headline_accepted_per_paid_share_note` (string or null),
`paid_model_share` (float), `accepted_tasks` (int), `runs_processed` (int),
`fixture_runs_excluded` (int), and `since` (ISO date string or null).

#### Scenario: JSON output is machine-parseable

- **GIVEN** `verdict metrics --daily --json`
- **THEN** stdout is valid JSON with no extra trailing text
- **AND** `headline_accepted_per_paid_share` is either a JSON number or JSON null
