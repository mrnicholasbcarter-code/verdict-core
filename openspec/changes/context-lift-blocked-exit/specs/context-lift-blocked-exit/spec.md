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
exited with `result=exit-code` and the most recent bench receipt has
`"conclusion": "blocked"`, the doctor row SHALL have status `warning` and
SHALL include the `block_reason` from the receipt in the detail text.

A timer that last exited with any other failure (crash, timeout) with no
blocked receipt SHALL have status `failure`.  An active or inactive timer
(no non-zero exit recorded) SHALL be treated as before.

#### Scenario: Blocked bench produces warning row
- **WHEN** timer state is `failed` with result `exit-code` and the bench receipt shows `conclusion=blocked`
- **THEN** the doctor row status is `warning` and the detail includes the block_reason

#### Scenario: Crashed timer produces failure row
- **WHEN** timer state is `failed` with result `exit-code` but no blocked receipt exists
- **THEN** the doctor row status is `failure`
