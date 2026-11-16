# Spec Delta

## MODIFIED Requirements

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

#### Scenario: Plain text remains supported
- **WHEN** raw output is a transcript or unrelated JSON without all result keys
- **THEN** existing substring validation is used without structured-result checks
- **TEST** `tests/test_check_dod.py::test_non_structured_raw_output_is_unchanged`

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
