# Spec Delta — headline-metric-report

## ADDED Requirements

### Requirement: `verdict metrics --daily` reports accepted work per paid model share

`verdict metrics --daily` SHALL aggregate verified receipts from the configured
runs directory and emit the M0 headline metric: accepted tasks divided by the
fraction of model attempts that used paid routes (subscription or metered).
The command SHALL complete with exit code 0 even when the runs directory does
not exist, is empty, or no paid model attempts exist in the selected period.

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

#### Scenario: Runs directory does not exist (fresh install)

- **GIVEN** the runs directory does not exist
- **WHEN** `verdict metrics --daily` (or `--since DATE`) is run
- **THEN** the command exits 0 and prints an empty report: `Runs processed:   0`
  and a line saying the runs directory was not found
- **AND** with `--json`, `runs_processed` is 0, `headline_accepted_per_paid_share`
  is null with the note "no runs in selected period", `runs_dir` is the path that
  was read and `runs_dir_found` is false
- **AND** an existing but empty runs directory reports `runs_dir_found` true

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

- **GIVEN** several runs, one with unknown cost, one with only free attempts
  and one with paid attempts
- **WHEN** `verdict metrics --daily --json` is run
- **THEN** every `per_run` row whose `dollars`, `tasks_per_dollar` or
  `headline_accepted_per_paid_share` is null has the matching non-empty
  `dollars_note`, `tasks_per_dollar_note` or
  `headline_accepted_per_paid_share_note`
- **AND** a row whose figure is a number has a null note

#### Scenario: Empty report observes nothing, so measured figures are null

- **GIVEN** the runs directory contains no verified receipts
- **WHEN** `verdict metrics --daily --json` is run
- **THEN** `frontier_tokens.input`, `.output` and `.total` are null, not 0
- **AND** every `first_attempt_pass_rate` value is null
- **AND** counts such as `accepted_tasks` and `total_model_attempts` are 0

#### Scenario: Observed zero is reported as zero

- **GIVEN** a verified receipt that records only free attempts
- **WHEN** `verdict metrics --daily --json` is run
- **THEN** `frontier_tokens.total` is 0, not null

#### Scenario: No string stands in for a missing number

- **GIVEN** any report, including an empty one
- **WHEN** `verdict metrics --daily --json` is run
- **THEN** no numeric field, including each `per_run` row, holds a string such
  as `"undefined"`, `"n/a"` or `"unknown"`; it holds a number or JSON null

### Requirement: A first-attempt pass requires validated work

A node's first model attempt SHALL count as a first-attempt pass only when the
attempt's outcome is `success` AND the node's final state is `VALIDATED`. A
worker's successful exit alone SHALL NOT count.

#### Scenario: Worker returns ok, then validation rejects it

- **GIVEN** a node whose first attempt returned successfully and was then
  rejected by validation (the runtime records a failure for that attempt)
- **WHEN** the first-attempt pass rate is computed
- **THEN** that node is eligible and not a pass

#### Scenario: Worker returns ok and validation never finishes

- **GIVEN** a node whose first attempt returned successfully and whose
  validation never finished, so the node did not end `VALIDATED`
- **WHEN** the first-attempt pass rate is computed
- **THEN** that node is eligible and not a pass

#### Scenario: Worker returns ok and the node is validated

- **GIVEN** a node whose first attempt returned successfully and which ended
  `VALIDATED`
- **WHEN** the first-attempt pass rate is computed
- **THEN** that node is eligible and a pass

### Requirement: Verified receipt bytes are the aggregated bytes

For each run, `receipt.json` SHALL be read once, without following symlinks, and
the bytes that are verified SHALL be the bytes that are aggregated. A receipt
that fails verification SHALL NOT contribute to the report.

#### Scenario: receipt.json changes between reads

- **GIVEN** a receipt that fails verification when read first, and a file that
  holds a valid receipt on any later read
- **WHEN** the runs directory is loaded
- **THEN** the run is skipped and counted in `live_runs_skipped`
- **AND** `receipt.json` was read once for that run

#### Scenario: receipt.json is a symlink

- **GIVEN** a run whose `receipt.json` is a symlink
- **WHEN** the runs directory is loaded
- **THEN** the run is skipped and counted in `live_runs_skipped`

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

### Requirement: `verdict metrics <path>` reads the path it is given

When `verdict metrics <path>` is run with `--daily` or `--since`, the report
SHALL be computed from `<path>`. An existing directory SHALL be read as given;
anything else SHALL be resolved as a run id under `--runs-dir`. `<path>` SHALL
win over `--runs-dir`, which SHALL be used only when no path is given or the path
is a run id. A path that resolves to no directory SHALL exit 2 with an error that
names the resolved path, and no report SHALL be printed.

#### Scenario: Positional directory is read, not --runs-dir

- **GIVEN** two directories, A holding run `run-a` and B holding run `run-b`
- **WHEN** `verdict metrics A --since 2026-01-01 --runs-dir B --json` is run
- **THEN** the report contains `run-a` and not `run-b`
- **AND** swapping A and B reports `run-b` and not `run-a`

#### Scenario: Positional run id is resolved under --runs-dir

- **GIVEN** a directory A holding runs `run-a` and `run-a2`
- **WHEN** `verdict metrics run-a2 --since 2026-01-01 --runs-dir A --json` is run
- **THEN** the report contains `run-a2` only

#### Scenario: Positional path is not a directory

- **GIVEN** a path that is not a directory and not a run id under `--runs-dir`
- **WHEN** `verdict metrics <path> --since 2026-01-01` is run
- **THEN** the command exits 2 and writes an error naming the resolved path to
  stderr
- **AND** nothing is written to stdout

### Requirement: `verdict metrics --daily --json` outputs valid JSON

The `--json` flag SHALL cause the command to print a single JSON object to
stdout.  The object SHALL include at minimum: `headline_accepted_per_paid_share`
(float or null), `headline_accepted_per_paid_share_note` (string or null),
`dollars` (float or null), `dollars_note` (string or null),
`tasks_per_dollar` (float or null), `tasks_per_dollar_note` (string or null),
`paid_model_share` (float), `accepted_tasks` (int), `runs_processed` (int),
`fixture_runs_excluded` (int), `since` (ISO date string or null), `runs_dir`
(the path that was read) and `runs_dir_found` (bool).

#### Scenario: JSON output is machine-parseable

- **GIVEN** `verdict metrics --daily --json`
- **THEN** stdout is valid JSON with no extra trailing text
- **AND** `headline_accepted_per_paid_share` is either a JSON number or JSON null
