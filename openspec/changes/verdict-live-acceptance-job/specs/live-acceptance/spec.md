# Live acceptance

## ADDED Requirements

### Requirement: Scheduled live acceptance run
`verdict acceptance run` SHALL run `doctor`, `eligibility`, `choose --role implement`,
`route`, one `serve` chat request and `harness prime status` from the installed
Verdict. It SHALL write `$VERDICT_HOME/acceptance/<ts>.json` with pass/fail, a
reason and output digests for every check, and keep the newest results.

#### Scenario: Result file per run
- **WHEN** `verdict acceptance run` finishes
- **THEN** a result file holds one entry per check with `passed`, `reason` and `stdout_sha256`
- **AND** older result files beyond the keep limit are removed

#### Scenario: Unimplemented command fails
- **WHEN** `verdict choose --role implement --json` exits non-zero
- **THEN** the choose check is FAIL with a reason starting "not implemented"

### Requirement: Never green without an eligible FREE route
The run SHALL be green only when every check passed and at least one FREE
tool-capable route is eligible. The eligibility check SHALL compare the list
with an independent 2-turn tool check on a bounded sample. It SHALL require
precision >= 95% and estimated recall >= 90%, after removing routes that answer
401, 402, 403 or 429.

#### Scenario: Zero eligible
- **WHEN** eligibility lists no eligible FREE tool route
- **THEN** the run is red even if every other check passed

#### Scenario: Listed route not served
- **WHEN** a listed route is missing from `/api/synced-available-models`
- **THEN** it counts against precision without a probe

### Requirement: Installed and checked
`verdict setup services` SHALL install an hourly `verdict-acceptance` timer.
`verdict doctor` SHALL report the newest result and its age. It SHALL fail when
that result is red or older than twice the interval.

#### Scenario: Red result
- **WHEN** the newest result is red
- **THEN** `verdict doctor` exits non-zero and names the failed checks

#### Scenario: Red is not a unit failure
- **WHEN** a run records a red result
- **THEN** it exits 3 and the unit's `SuccessExitStatus=3` keeps the unit from failing

### Requirement: One alert per regression
The job SHALL send one notification when a job that has been green is red for
two runs in a row, and one when it turns green again. It SHALL NOT notify while
the job has never been green.

#### Scenario: Known red state
- **WHEN** the job has been red since install
- **THEN** no notification is sent

#### Scenario: Regression
- **WHEN** a green job is red for two runs, then red again
- **THEN** exactly one notification is sent for that streak
