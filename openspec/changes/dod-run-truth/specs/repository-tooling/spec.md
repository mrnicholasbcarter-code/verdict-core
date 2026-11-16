# Spec Delta

## Purpose
Extend repository tooling so definition-of-done evidence cannot claim an orchestration run without the command's structured result stdout.

The gate checks that a recorded run is internally consistent and matches its own result; it cannot prove the result JSON was produced by the command, because a hand-edited valid result would pass. Duplicate detection compares file digests, so two different files describing the same `run_id` are not detected by digest comparison alone.

## ADDED Requirements

### Requirement: Definition-of-done evidence proves orchestration run truth
The definition-of-done checker MUST preserve its existing artifact, expected-text, exit-code, ancestry, and timestamp checks. When an acceptance or parent-confirmation raw output is a top-level JSON object containing `outcome`, `run_id`, `started_at`, `finished_at`, and `events_digest`, it MUST additionally verify that a recorded zero exit code agrees with `outcome: COMPLETE` (and a non-zero exit agrees with any non-COMPLETE outcome), that a zero-exit result has review status `PASS`, and that the recorded `at` is within five minutes before `started_at` through five minutes after `finished_at`. Each failure MUST identify the field and both compared values. Raw outputs that do not meet the structured-result shape MUST retain existing behavior.

#### Scenario: Outcome agrees with recorded exit
- **WHEN** structured output says `COMPLETE` and the recorded exit code is zero
- **THEN** the run passes; a mismatch fails naming exit code and outcome
- **TEST** `tests/test_check_dod.py::test_structured_result_exit_and_outcome_must_agree`

#### Scenario: Recorded time is within the result window
- **WHEN** structured output gives a run interval and the recorded `at` is within five minutes either side
- **THEN** the run passes; otherwise it fails naming `at` and the result window
- **TEST** `tests/test_check_dod.py::test_structured_result_time_must_be_in_own_window`

#### Scenario: Successful structured result has a passing review
- **WHEN** recorded exit code is zero and structured output review status is `PASS`
- **THEN** the run passes; another status fails naming review status and `PASS`
- **TEST** `tests/test_check_dod.py::test_structured_result_review_must_pass_for_zero_exit`

### Requirement: Orchestration JSON commands require structured stdout
When an acceptance or parent-confirmation command invokes `verdict orchestrate` with a `--json` token, the checker MUST require raw output to be a structured result object containing `outcome`, `run_id`, `started_at`, `finished_at`, and `events_digest`. The matcher MUST recognize a `verdict` token by basename, including path-prefixed executables and `python -m verdict`, and MUST NOT match `verdict route --json` or `verdict orchestrate` without `--json`. Missing, empty, hand-built, event-list, or noisy output MUST fail and identify the missing keys and that stdout must be the command's result JSON.

#### Scenario: Hand-built orchestration summary is rejected
- **WHEN** an orchestrate JSON command records a summary without the five result keys
- **THEN** the evidence fails with a structured-result error naming missing keys
- **TEST** `tests/test_check_dod.py::test_orchestrate_command_rejects_hand_built_summary`

#### Scenario: Result stdout with trailing newline is accepted
- **WHEN** an orchestrate JSON command records a complete result object followed by a newline
- **THEN** structured truth validation proceeds normally
- **TEST** `tests/test_check_dod.py::test_orchestrate_command_accepts_result_json`

#### Scenario: Leading stderr noise is rejected
- **WHEN** an orchestrate JSON command records a stderr line before the result JSON
- **THEN** the evidence fails because stdout must contain only result JSON
- **TEST** `tests/test_check_dod.py::test_orchestrate_command_rejects_leading_noise`

#### Scenario: Command matching is conservative
- **WHEN** commands are path-prefixed verdict invocations or `python -m verdict` with `orchestrate` and `--json`
- **THEN** they match; route commands and orchestrate commands without `--json` do not
- **TEST** `tests/test_check_dod.py::test_orchestrate_command_matcher`

#### Scenario: Non-orchestration JSON remains unchanged
- **WHEN** another command records arbitrary JSON
- **THEN** existing substring and exit checks apply without requiring result keys
- **TEST** `tests/test_check_dod.py::test_non_orchestrate_json_is_unaffected`

### Requirement: Structured parent confirmations prove successful runs
When a parent confirmation raw output is a structured result, it MUST have `outcome: COMPLETE`, review status `PASS`, and a recorded `at` within the result window with five-minute tolerance. A BLOCKED result MUST fail confirmation even though confirmations have no recorded exit-code field.

#### Scenario: BLOCKED confirmation is rejected
- **WHEN** parent confirmation records a structured BLOCKED result
- **THEN** the evidence fails naming the outcome and COMPLETE requirement
- **TEST** `tests/test_check_dod.py::test_blocked_structured_parent_confirmation_fails`

#### Scenario: Confirmation outside result window is rejected
- **WHEN** a COMPLETE/PASS confirmation's recorded time is outside the tolerated window
- **THEN** the evidence fails naming `at` and the result window
- **TEST** `tests/test_check_dod.py::test_structured_parent_confirmation_time_must_be_in_window`

#### Scenario: Plain text remains supported
- **WHEN** raw output is a transcript or unrelated JSON without all result keys
- **THEN** existing substring validation is used without structured-result checks
- **TEST** `tests/test_check_dod.py::test_non_structured_raw_output_is_unchanged`

### Requirement: Structured orchestration results use distinct run ids
Structured results used by first_run, fresh_run, and parent_confirmation MUST have three different `run_id` values. Reuse MUST fail with `the same run_id cannot serve as two runs`.

#### Scenario: Reused run id is rejected
- **WHEN** two structured run outputs have the same run_id but different bytes
- **THEN** the evidence fails with the repeated-run-id message
- **TEST** `tests/test_check_dod.py::test_same_run_id_cannot_serve_as_two_structured_runs`

#### Scenario: Distinct run ids pass
- **WHEN** each structured run output has a different run_id
- **THEN** the evidence remains valid
- **TEST** `tests/test_check_dod.py::test_distinct_structured_run_ids_pass`

### Requirement: Definition-of-done evidence uses distinct run outputs
Acceptance runs and parent confirmation MUST NOT reuse the same raw-output SHA-256. If any two such runs share a digest, the checker MUST fail with `the same raw output cannot serve as two runs`.

#### Scenario: Reused output is rejected
- **WHEN** two acceptance runs or an acceptance run and parent confirmation point to the same raw artifact
- **THEN** the evidence fails with the duplicate-output message
- **TEST** `tests/test_check_dod.py::test_same_raw_output_cannot_serve_as_two_runs`

#### Scenario: Distinct outputs pass
- **WHEN** every acceptance and confirmation run has a distinct raw artifact
- **THEN** the evidence remains valid
- **TEST** `tests/test_check_dod.py::test_distinct_raw_outputs_are_valid`
