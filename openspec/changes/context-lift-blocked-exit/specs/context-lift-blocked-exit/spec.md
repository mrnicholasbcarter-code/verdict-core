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

1. The service's `ExecMainStatus` is `2`, the intentional blocked-exit code.
2. The most recent bench receipt has `"conclusion": "blocked"`.
3. The receipt was written by that same unit run: its `invocation_id` equals
   the service's `InvocationID`.
4. The timer's own `ActiveState` is `active`, so the bench will run again.

The bench SHALL record the `$INVOCATION_ID` that systemd passes to the unit's
processes as `invocation_id` in the receipt, and `null` when the variable is
absent, as in a manual or `--dry-run` run outside the unit. systemd gives every
process of one runtime cycle of a unit the same ID and exposes it as the unit's
`InvocationID` property. The two IDs SHALL be compared after trimming whitespace
and ignoring case, and an ID that is absent, empty or not a string on either
side SHALL never match. Ownership SHALL NOT be decided by comparing times: a
time cannot tell the receipt of the failed unit run from one that a later
manual run wrote.

When any condition is absent or unmet the row SHALL have status `failure`, and
doctor SHALL NOT raise. A `warning` row SHALL include the `block_reason` from the
receipt in the detail text. A `latest.json` that is valid JSON but not an object
SHALL be treated as no receipt.

#### Scenario: Blocked bench produces warning row
- **WHEN** timer state is `failed` with result `exit-code`, `ExecMainStatus=2`,
  the bench receipt shows `conclusion=blocked` with an `invocation_id` equal to
  the service's `InvocationID`, and the timer's `ActiveState` is `active`
- **THEN** the doctor row status is `warning` and the detail includes the
  block_reason

#### Scenario: First blocked run produces warning row
- **WHEN** the unit has never succeeded, so `ActiveEnterTimestamp` is `n/a`, and
  the blocked receipt carries the service's `InvocationID`
- **THEN** the doctor row status is `warning`

#### Scenario: Crashed timer produces failure row
- **WHEN** timer state is `failed` with result `exit-code` but no blocked
  receipt exists
- **THEN** the doctor row status is `failure`

#### Scenario: Stale blocked receipt does not mask a crash
- **WHEN** timer state is `failed` with result `exit-code` and
  `ExecMainStatus=1` (crash), and a bench receipt with `conclusion=blocked`
  exists from a previous run
- **THEN** the doctor row status is `failure` (the stale receipt is ignored)

#### Scenario: Receipt from a different run does not mask a failure
- **WHEN** `ExecMainStatus=2` and the receipt's `invocation_id` differs from the
  service's `InvocationID`
- **THEN** the doctor row status is `failure`

#### Scenario: Manual run after a usage-error exit does not mask the failure
- **WHEN** the unit exited 2 from a command-line usage error and wrote no
  receipt, and the operator then ran the bench by hand (or with `--dry-run`),
  which wrote a blocked receipt with `invocation_id` null
- **THEN** the doctor row status is `failure`

#### Scenario: Unit without an InvocationID is a failure
- **WHEN** systemd reports no `InvocationID` for the service (before v232)
- **THEN** the doctor row status is `failure`

#### Scenario: Stopped or failed timer is not downgraded
- **WHEN** the service failed with a matching blocked receipt but the timer's
  `ActiveState` is `inactive`, `failed`, `activating` or `deactivating`
- **THEN** the doctor row status is `failure`, because the bench will not run again

#### Scenario: Receipt that is not a JSON object is ignored
- **WHEN** `latest.json` is valid JSON but not an object (for example a list)
- **THEN** it is treated as no receipt and the doctor row status is `failure`
