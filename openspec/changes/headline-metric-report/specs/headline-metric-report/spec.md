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
- **AND** `headline_accepted_per_paid_share_note` is "no runs in selected
  period" (not the free-only wording)
- **AND** `runs_processed` is 0
- **AND** the command exits 0

#### Scenario: Missing data reported as missing, never zero-filled

- **GIVEN** receipts where some attempts have unknown cost (dollars = null)
- **WHEN** `verdict metrics --daily --json` is run
- **THEN** the `dollars` field is null, not 0, and `dollars_note` is a
  non-empty string saying the cost is unknown
- **AND** `has_unknown_cost` is true
- **AND** `tasks_per_dollar` is JSON null, not 0 and not a string
- **AND** `tasks_per_dollar_note` is a non-empty string giving the reason

#### Scenario: No recorded spend — tasks per dollar is not a division by zero

- **GIVEN** verified receipts where every attempt has a recorded cost and the
  recorded cost sums to 0.0
- **WHEN** `verdict metrics --daily --json` is run
- **THEN** `dollars` is 0.0
- **AND** `tasks_per_dollar` is JSON null (no division is attempted)
- **AND** `tasks_per_dollar_note` is a non-empty string giving the reason

#### Scenario: Per-run rows explain their null figures

- **GIVEN** several runs, one with unknown cost and one with no recorded spend
- **WHEN** `verdict metrics --daily --json` is run
- **THEN** every `per_run` row whose `dollars` or `tasks_per_dollar` is null
  has a non-empty `dollars_note` or `tasks_per_dollar_note` respectively
- **AND** a row whose figure is a number has a null note

#### Scenario: No string stands in for a missing number

- **GIVEN** any report, including an empty one
- **WHEN** `verdict metrics --daily --json` is run
- **THEN** no numeric field, including each `per_run` row, holds a string such
  as `"undefined"`, `"n/a"` or `"unknown"`; it holds a number or JSON null

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
`dollars` (float or null), `dollars_note` (string or null),
`tasks_per_dollar` (float or null), `tasks_per_dollar_note` (string or null),
`paid_model_share` (float), `accepted_tasks` (int), `runs_processed` (int),
`fixture_runs_excluded` (int), and `since` (ISO date string or null).

#### Scenario: JSON output is machine-parseable

- **GIVEN** `verdict metrics --daily --json`
- **THEN** stdout is valid JSON with no extra trailing text
- **AND** `headline_accepted_per_paid_share` is either a JSON number or JSON null
