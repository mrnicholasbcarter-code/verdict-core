# Spec Delta

## Purpose

Run the full proof as a declared number of legs plus a fail-closed aggregate, so a pull request round is no longer bounded by one long job, while every test and every gate the proof checks today is still checked.

## ADDED Requirements

### Requirement: The combined `unit_full` and `unit_timing` gates run every collected test exactly once across the legs
In a sharded full proof, every test that `pytest tests/` collects SHALL run exactly once across the combined `unit_full` and `unit_timing` gates: in one leg's `unit_full` gate, or, for tests marked `timing`, in the `unit_timing` gate of leg 1. This accounting covers those two gates only. The targeted gate (`unit_targeted`, run with the workflow's `--targets`) SHALL stay in every leg, as it is in the single job today, so the tests it selects run in each leg in addition to their one run in `unit_full` or `unit_timing`; that repetition is intended and outside the exactly-once accounting. The number of legs SHALL be the single number the proof contract declares for the full mode. A run without a leg index SHALL run the whole suite in `unit_full`, as before.

#### Scenario: Every test runs once
- **WHEN** the legs' selected tests are compared with a set collected independently by the aggregate
- **THEN** the union of the `unit_full` selections equals the collected set minus the `timing` tests, no test is in two legs, and the `unit_timing` selection equals the `timing` tests

#### Scenario: A test is in no leg
- **WHEN** a collected test appears in no leg's selection
- **THEN** the aggregate fails and names the missing test

#### Scenario: A test is in two legs
- **WHEN** a test appears in the selection of two legs
- **THEN** the aggregate fails and names the duplicated test

#### Scenario: A leg executed a different set than it selected
- **WHEN** a leg's JUnit file records one test it did not select and omits one it did, so the count is unchanged
- **THEN** the aggregate fails and names the leg and the substituted test

#### Scenario: A leg ran a test that was not collected
- **WHEN** a leg's selection holds a test id absent from the independently collected set
- **THEN** the aggregate fails and names the unexpected test

#### Scenario: The targeted gate repeats in every leg
- **WHEN** a sharded full proof's leg reports are read
- **THEN** each leg records `unit_targeted` with the same targets and the contract's command, the aggregate does not count those runs towards the exactly-once accounting, and a leg whose `unit_targeted` gate is missing or has another command fails the aggregate

#### Scenario: Unsharded run
- **WHEN** the full proof runs without a leg index
- **THEN** `unit_full` runs the same arguments as the contract's command, with no marker filter and no split, and `unit_timing` is recorded as skipped because `unit_full` runs the `timing` tests

#### Scenario: Leg index outside the declared count
- **WHEN** a leg index is below 1 or above the declared leg count, or the contract declares no leg count
- **THEN** the runner exits with a usage error and runs no gate

### Requirement: The timing tests run serially in exactly one leg
The `timing` tests SHALL be excluded from every leg's `unit_full` and SHALL run in `unit_timing` in leg 1 only, with parallel test execution disabled by construction: the runner SHALL launch the `unit_timing` gate without `PYTEST_ADDOPTS`, so no xdist option set by the workflow reaches it, and the gate's command SHALL keep `-p no:xdist` as a second guard. Every other gate keeps the environment it has today. In every other leg `unit_timing` SHALL be recorded as skipped. The set `unit_timing` runs SHALL equal the set `unit_full` excludes, and SHALL NOT be empty.

#### Scenario: Leg 1 runs the timing tests alone
- **WHEN** leg 1 of a sharded full proof reaches `unit_timing`
- **THEN** it runs only the tests marked `timing`, in one process, and the report records the gate as passed

#### Scenario: The timing gate cannot inherit parallel options
- **WHEN** a leg launches `unit_timing` in an environment where `PYTEST_ADDOPTS` is `-p xdist -n auto`
- **THEN** the environment the gate's subprocess receives holds no `PYTEST_ADDOPTS`, holds every other variable, and the recorded command still holds `-p no:xdist`; `unit_full` in the same run still receives `PYTEST_ADDOPTS`

#### Scenario: Other legs skip the timing gate
- **WHEN** leg 2 or a later leg runs
- **THEN** its report records `unit_timing` as skipped and its `unit_full` selection holds no `timing` test

#### Scenario: Timing set is empty or different
- **WHEN** the `unit_timing` selection is empty, or differs from the tests collected with the `timing` marker
- **THEN** the aggregate fails

### Requirement: Sharding does not weaken any gate
Every leg SHALL run every gate of the full mode with the command and `required` flag the contract declares, plus only the arguments that sharding adds: for the split test gate the marker filter, the split arguments and a report file, and for the timing gate a report file. A gate SHALL be skipped only where the contract authorizes it: the timing gate outside leg 1, a gate conditioned on `llm_boundary` when that is not enabled, and the targeted test gate when no targets are given. Across the split gate and the timing gate together, no collected test SHALL be left unrun or run twice. Sharding SHALL introduce no additional skip and SHALL re-mark nothing: no marker, expected-failure or skip declaration SHALL change, and the marker filter and the split deselect a test only because another gate or leg runs it. Skips and expected failures that the tests declare themselves SHALL stay as they are: they run in their one leg, are recorded as skipped in the JUnit file, and are neither a failure nor an error. A failing or skipped gate that is not required SHALL keep its present meaning: listed in the report, without failing the proof.

#### Scenario: Leg runs the contract's commands
- **WHEN** a leg report is checked against the contract
- **THEN** each gate's recorded command equals the command recomputed from the contract for that leg, and each recorded `required` flag equals the contract's

#### Scenario: Altered command
- **WHEN** a leg's recorded `unit_full` command lacks the marker filter, the split arguments, or carries an extra deselect
- **THEN** the aggregate fails and names the leg and the gate

#### Scenario: Flipped required flag
- **WHEN** a leg records `required: false` for a gate the contract marks required
- **THEN** the aggregate fails and names the gate

#### Scenario: Missing gate
- **WHEN** a leg report holds fewer or more gates than the contract's full mode
- **THEN** the aggregate fails and names the difference

#### Scenario: Skips written by the tests stay
- **WHEN** a collected test skips itself or is an expected failure through a declaration in the test code
- **THEN** it runs in its one leg as it does today, its JUnit case records the skip, and the aggregate accepts the case and adds it to the recorded `skipped` sum

#### Scenario: Sharding adds no skip and re-marks nothing
- **WHEN** the change's diff against `origin/main` is read
- **THEN** it adds or removes no skip, expected-failure, deselect or marker decorator, call or assignment in a test under `tests/` (a `parametrize` mark aside), weakens no gate command or `required` flag in `proof/contract.yaml`, and changes neither the pytest settings in `pyproject.toml` nor `.test_durations`

#### Scenario: Words held as data are not a change to a test
- **WHEN** a test file added by the change holds `skip`, `xfail`, `deselect` or a marker name only as string data, such as an argv list or the source of a toy test written to a temporary directory
- **THEN** the check that reads the diff finds the code of the file by parsing and does not flag it

#### Scenario: Non-required gate keeps its meaning
- **WHEN** a gate with `required: false` fails or is skipped in every leg
- **THEN** it is listed in the merged report and the aggregate still passes if every other check holds

### Requirement: The aggregate fails closed
The aggregate SHALL pass only when every check holds: the leg job result is success; reports exist for exactly the declared leg indices; every report is a readable JSON object from a full-mode, no-cache run of the same contract with the right leg index and count; every report records the commit the aggregate itself runs on; every required gate has the status the contract implies for its leg (passed, or skipped only where the contract authorizes it) and every gate, required or not, has the contract's command and `required` flag; every leg's `ok` is true; the tests add up to the independently collected set; no two collected tests share one JUnit identity (`classname` and `name`); and the tests each leg's JUnit file records as run are exactly the tests it selected, same identities and same number, with no failure or error. Any missing, unreadable, malformed or wrongly typed input SHALL be a failure reason and SHALL NOT raise.

#### Scenario: Leg failed
- **WHEN** one leg's report says `ok: false`
- **THEN** the aggregate fails, names the leg and lists that leg's failures in the merged report

#### Scenario: Leg cancelled, skipped or its report is missing
- **WHEN** a leg was cancelled or skipped, or its report is absent
- **THEN** the aggregate fails and names the missing leg index

#### Scenario: Wrong number or repeated legs
- **WHEN** the reports hold one leg too few, one too many, a repeated index, or an index outside the declared range
- **THEN** the aggregate fails

#### Scenario: Leg count differs from the contract
- **WHEN** a report records a leg count different from the contract's declared number
- **THEN** the aggregate fails

#### Scenario: Wrong or mixed commit
- **WHEN** a report's commit differs from another leg's, or from the commit the aggregate runs on, or is missing
- **THEN** the aggregate fails

#### Scenario: Wrong mode, cache mode or contract
- **WHEN** a report is not a full-mode, no-cache run of the same contract
- **THEN** the aggregate fails

#### Scenario: JUnit record differs from the selection
- **WHEN** a leg's JUnit file records a different number of tests than it selected, or records a test it did not select in place of one it did (same count), or records a failure or error
- **THEN** the aggregate fails and names the leg

#### Scenario: Two collected tests share one JUnit identity
- **WHEN** two different collected test ids map to the same JUnit `classname` and `name`, for example `tests/test_a/test_b.py::test_x` and `tests/test_a.test_b.py::test_x`
- **THEN** the aggregate fails and names both ids, because a JUnit file could not show one of them being run in place of the other

#### Scenario: Authorized skips are accepted, others are not
- **WHEN** `unit_timing` is skipped in a leg other than leg 1, a `llm_boundary` gate is skipped, or `unit_targeted` is skipped because no targets were given
- **THEN** the aggregate accepts them; a required gate skipped anywhere else, including `unit_timing` in leg 1, fails the aggregate

#### Scenario: Unreadable or malformed input
- **WHEN** a report, selection file or JUnit file is missing, not valid, not an object, or has a wrong-typed field
- **THEN** the aggregate fails with a reason naming the file, and does not raise

#### Scenario: All checks hold
- **WHEN** every check above holds
- **THEN** the aggregate passes and writes a merged report with `ok: true`

### Requirement: The aggregate is always evaluated and keeps today's report shape
The aggregate job SHALL be named `proof-full` and SHALL run whatever the legs' results, including when a leg failed or was cancelled. It SHALL write a merged full-proof report that keeps the top-level keys of the single-job report (`ok`, `mode`, `cache_mode`, `runner`, `contract_name`, `head_sha`, `gates`, `failures`, `otel`), with one gate entry per gate id, and adds a `shards` key. Each aggregate failure SHALL appear in `failures` with a reason.

#### Scenario: Aggregate runs after a failed leg
- **WHEN** a leg fails
- **THEN** the aggregate job still runs, fails, and uploads a merged report whose `failures` hold the leg's failures and the aggregate's reason

#### Scenario: Merged gate entry
- **WHEN** a gate passed in some legs and failed in another
- **THEN** the merged entry for that gate is `fail`, with the maximum duration over the legs

### Requirement: Legs start without waiting and are time-bounded
The legs SHALL NOT wait for any other job of the proof workflow, and each leg and the aggregate SHALL have a timeout shorter than the platform default, so that a hung leg cannot hold a required check. The `proof-targeted` job SHALL stay in the workflow, and no job SHALL wait on it.

#### Scenario: Legs have no dependency
- **WHEN** the proof workflow is read
- **THEN** the leg job declares no `needs`, and its matrix lists exactly the leg indices 1 to the declared count

#### Scenario: The targeted proof job is kept and nothing waits on it
- **WHEN** the proof workflow is read
- **THEN** the `proof-targeted` job is still defined, and no job lists it in `needs`

#### Scenario: Timeouts
- **WHEN** the proof workflow is read
- **THEN** the leg job and the aggregate job each declare a timeout of minutes

### Requirement: Making the aggregate a required check is an operator step with documented commands
The change SHALL NOT modify branch protection. The proof documentation SHALL give the operator the exact command that adds `proof-full` to the required checks, a command that reads the result back, and the rollback command, and SHALL state that the command is applied only after the aggregate has been green on main.

#### Scenario: Commands are documented
- **WHEN** the proof documentation is read
- **THEN** it holds the apply, read-back and rollback commands, names the check `proof-full`, and states that no automation applies them

#### Scenario: No automation applies branch protection
- **WHEN** the repository's workflows and scripts are searched for the branch protection endpoint
- **THEN** none writes it
