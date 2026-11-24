# Spec Delta

## ADDED Requirements

### Requirement: A blocked context-lift bench SHALL exit non-zero
When `verdict bench context-lift --json` completes with
`"conclusion": "blocked"` and was not run with `--dry-run`, the process SHALL
exit with code 2.  A passing run (`"conclusion": "pass"`) SHALL still exit 0.
A dry-run SHALL always exit 0 regardless of conclusion.

#### Scenario: Blocked bench exits 2
- **WHEN** `verdict bench context-lift --json` runs (not dry-run) and returns conclusion `blocked`
- **THEN** the process exits with code 2

#### Scenario: Passing bench exits 0
- **WHEN** `verdict bench context-lift --json` runs (not dry-run) and returns conclusion `pass`
- **THEN** the process exits with code 0

#### Scenario: Dry-run exits 0
- **WHEN** `verdict bench context-lift --dry-run --json` runs
- **THEN** the process exits with code 0

### Requirement: Doctor SHALL surface a blocked bench as a warning
When `verdict doctor` checks the context-lift timer and the timer service last
exited with `result=exit-code`, the doctor row SHALL have status `warning`
(not `failure`) only when ALL of the following hold:

1. The service's `ExecMainStatus` is `"2"` — the intentional blocked-exit code.
2. The most recent bench receipt has `"conclusion": "blocked"`.
3. The receipt's `finish_time` is not earlier than the service's
   `ActiveEnterTimestamp` — the receipt belongs to this invocation, not a
   previous blocked run.

When any condition is absent or unmet the row SHALL have status `failure`.
A `warning` row SHALL include the `block_reason` from the receipt in the
detail text.

#### Scenario: Blocked bench produces warning row
- **WHEN** timer state is `failed` with result `exit-code`, `ExecMainStatus=2`,
  and the bench receipt shows `conclusion=blocked` with a `finish_time` that
  post-dates the service's `ActiveEnterTimestamp`
- **THEN** the doctor row status is `warning` and the detail includes the
  block_reason

#### Scenario: Crashed timer produces failure row
- **WHEN** timer state is `failed` with result `exit-code` but no blocked
  receipt exists
- **THEN** the doctor row status is `failure`

#### Scenario: Stale blocked receipt does not mask a crash
- **WHEN** timer state is `failed` with result `exit-code` and
  `ExecMainStatus=1` (crash), and a bench receipt with `conclusion=blocked`
  exists from a previous run
- **THEN** the doctor row status is `failure` (the stale receipt is ignored)
