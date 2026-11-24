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
3. The receipt's `finish_time` is not earlier than the service's
   `ExecMainStartTimestamp`, the start of the last invocation of the unit's
   main process. The receipt then belongs to that invocation and not to an
   older run. `ActiveEnterTimestamp` SHALL NOT be used for this: a one-shot
   unit exits while still activating, so that field is `n/a` or left over
   from an earlier run.

Both times SHALL be compared in UTC, independent of the host's timezone:

- Doctor SHALL request the unit's timestamps from systemd as explicit UTC
  (`systemctl --user show --timestamp=us+utc`). A systemd that rejects the
  flag SHALL be queried again without it.
- A start time is usable only when systemd prints it with a `UTC` suffix. A
  local-zone value (for example `EST`), `n/a` or any other text is unusable.
- A receipt `finish_time` is valid only when it is an ISO-8601 string with a
  UTC offset. A value without an offset, or one that is not a string, is
  invalid. It SHALL never be compared with the start time.

When any condition is absent, unmet, unusable or invalid the row SHALL have
status `failure`, and doctor SHALL NOT raise. A `warning` row SHALL include
the `block_reason` from the receipt in the detail text.

#### Scenario: Blocked bench produces warning row
- **WHEN** timer state is `failed` with result `exit-code`, `ExecMainStatus=2`,
  and the bench receipt shows `conclusion=blocked` with a `finish_time` that is
  not earlier than the service's `ExecMainStartTimestamp`
- **THEN** the doctor row status is `warning` and the detail includes the
  block_reason

#### Scenario: First blocked run produces warning row
- **WHEN** the unit has never succeeded, so `ActiveEnterTimestamp` is `n/a`, and
  the blocked receipt is not earlier than `ExecMainStartTimestamp`
- **THEN** the doctor row status is `warning`

#### Scenario: Host outside UTC produces warning row
- **WHEN** the host timezone is not UTC and the blocked receipt is not earlier
  than the invocation start
- **THEN** the doctor row status is `warning`, because the start time was
  requested as UTC and not read in the local zone

#### Scenario: Crashed timer produces failure row
- **WHEN** timer state is `failed` with result `exit-code` but no blocked
  receipt exists
- **THEN** the doctor row status is `failure`

#### Scenario: Stale blocked receipt does not mask a crash
- **WHEN** timer state is `failed` with result `exit-code` and
  `ExecMainStatus=1` (crash), and a bench receipt with `conclusion=blocked`
  exists from a previous run
- **THEN** the doctor row status is `failure` (the stale receipt is ignored)

#### Scenario: Receipt older than the invocation does not mask a failure
- **WHEN** `ExecMainStatus=2` but the blocked receipt's `finish_time` is earlier
  than `ExecMainStartTimestamp`
- **THEN** the doctor row status is `failure`

#### Scenario: Receipt time without an offset is invalid
- **WHEN** the blocked receipt's `finish_time` has no UTC offset
- **THEN** the doctor row status is `failure` and doctor does not raise

#### Scenario: Start time in a local zone is unusable
- **WHEN** systemd prints the start time in a local zone because it rejected
  `--timestamp`, and the host is not in UTC
- **THEN** the doctor row status is `failure`

#### Scenario: Receipt that is not a JSON object is ignored
- **WHEN** `latest.json` is valid JSON but not an object (for example a list)
- **THEN** it is treated as no receipt and the doctor row status is `failure`
