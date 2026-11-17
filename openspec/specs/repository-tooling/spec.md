## Purpose

Extend repository tooling so definition-of-done evidence cannot claim an orchestration run without the command's structured result stdout.

## Requirements

### Requirement: Definition-of-done evidence proves orchestration run truth
The definition-of-done checker MUST preserve its existing artifact, expected-text, exit-code, ancestry, and timestamp checks. When an acceptance or parent-confirmation raw output is a top-level JSON object containing `outcome`, `run_id`, `started_at`, `finished_at`, and `events_digest`, it MUST additionally verify that a recorded zero exit code agrees with `outcome: COMPLETE` (and a non-zero exit agrees with any non-COMPLETE outcome), that a zero-exit result has review status `PASS`, and that the recorded `at` is within five minutes before `started_at` through five minutes after `finished_at`. Each failure MUST identify the field and both compared values. A structured result whose `run_id` is not a non-empty string MUST fail naming `run_id`. A result time the checker cannot compare, including one so close to the first or last representable date that the five-minute tolerance overflows, MUST fail as an invalid structured result time. Raw outputs that do not meet the structured-result shape MUST retain existing behavior. No raw output MUST make the checker raise: output the JSON parser rejects for any reason, including an integer over the digit limit or nesting deeper than the parser allows, is not a structured result.

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

#### Scenario: Zero exit with a blocked outcome is rejected
- **WHEN** structured output says `BLOCKED` with review status `PASS` and the recorded exit code is zero
- **THEN** the run fails naming exit code 0 and outcome BLOCKED
- **TEST** `tests/test_check_dod.py::test_zero_exit_with_a_blocked_outcome_fails_even_with_a_passing_review`

#### Scenario: The tolerance is exactly five minutes
- **WHEN** the recorded `at` is five minutes before `started_at` or five minutes after `finished_at`
- **THEN** the run passes; one second further out on either side fails
- **TEST** `tests/test_check_dod.py::test_result_window_tolerance_is_exactly_five_minutes`

#### Scenario: Run id is a non-empty string
- **WHEN** a structured result has a `run_id` that is a list, an object, null, a number, a boolean or an empty string
- **THEN** the run fails with an error naming `run_id`, and the checker does not raise
- **TEST** `tests/test_check_dod.py::test_structured_run_id_must_be_a_non_empty_string`

#### Scenario: Time arithmetic overflow is an invalid time
- **WHEN** `finished_at` is in year 9999 or `started_at` is in year 0001
- **THEN** the run fails with the invalid structured result time error, and the checker does not raise
- **TEST** `tests/test_check_dod.py::test_structured_result_time_arithmetic_overflow_is_an_invalid_time`

#### Scenario: Output the parser rejects under another command is plain text
- **WHEN** a command that is not an orchestrate JSON command records the marker, the expected text and a 5000-digit integer or unclosed deep nesting
- **THEN** the evidence passes as it does without this change
- **TEST** `tests/test_check_dod.py::test_unparseable_output_of_another_command_is_plain_text`

### Requirement: Orchestration JSON commands require structured stdout
When an acceptance or parent-confirmation command invokes `verdict orchestrate` with JSON output, and the acceptance item it belongs to expects exit code 0 or 1, the checker MUST require raw output to be a structured result object containing `outcome`, `run_id`, `started_at`, `finished_at`, and `events_digest`. Missing, empty, hand-built, event-list, or noisy output, output with whitespace before the result, and output the JSON parser rejects MUST fail and identify the missing keys and that stdout must be the command's result JSON.

The matcher MUST recognize a `verdict` token by basename, including path-prefixed executables, `python -m verdict` and `python -m verdict.cli`. It MUST treat every unambiguous prefix of `--json` of three or more characters (`--j`, `--js`, `--jso`, `--json`) as the JSON flag, because the command-line parser accepts each of them. It MUST split shell punctuation from the words it touches, so `--json>out.json`, `--json;` and `(verdict orchestrate goal --json)` match. When the command cannot be parsed as shell words, for example because of an unterminated quote, the matcher MUST fall back to splitting on whitespace and shell punctuation: a parse failure adds the requirement and never removes it. It MUST NOT match `-j`, `--json=true`, `verdict route --json`, `v orchestrate --json`, or `verdict orchestrate` without a JSON flag.

`verdict orchestrate` prints its result when it exits 0 or 1. Any other exit code is a startup block that prints one line to stderr and no result. When the acceptance item expects such an exit code (`expect_exit_code` other than 0 or 1), the checker MUST NOT require a structured result and MUST check the output as plain text, as it does without this change.

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

#### Scenario: Prefix, module and shell forms match
- **WHEN** the command uses `--j`, `--js` or `--jso`, runs `python -m verdict.cli`, or has `>`, `;` or parentheses touching its words
- **THEN** it matches; `-j`, `--json=true`, `verdict route --json`, `v orchestrate --json` and `verdict orchestrate` without a JSON flag do not
- **TEST** `tests/test_check_dod.py::test_orchestrate_command_matcher_forms`

#### Scenario: A command that cannot be parsed keeps the requirement
- **WHEN** an orchestrate command with an unterminated quote, with `--js`, or run as `python -m verdict.cli` records a hand-built summary for all three runs
- **THEN** each run fails with the "not the run's result JSON" error
- **TEST** `tests/test_check_dod.py::test_hand_built_summary_fails_under_every_orchestrate_json_form`

#### Scenario: Whitespace before the result is rejected
- **WHEN** an orchestrate JSON command records a space before a complete result object
- **THEN** the run fails with the "not the run's result JSON" error
- **TEST** `tests/test_check_dod.py::test_orchestrate_command_rejects_leading_whitespace_before_the_result`

#### Scenario: A result missing one key is rejected
- **WHEN** an orchestrate JSON command records a result that lacks exactly one of the five keys
- **THEN** each run fails naming that key as missing
- **TEST** `tests/test_check_dod.py::test_orchestrate_result_missing_one_key_names_that_key`

#### Scenario: Output the parser rejects is not a result
- **WHEN** an orchestrate JSON command records a result holding a 5000-digit integer, or nesting deeper than the parser allows
- **THEN** each run fails with the "not the run's result JSON" error, and the checker does not raise
- **TEST** `tests/test_check_dod.py::test_orchestrate_output_with_an_oversized_integer_is_not_a_result`
- **TEST** `tests/test_check_dod.py::test_orchestrate_output_nested_too_deep_is_not_a_result`

#### Scenario: A startup block is checked as plain text
- **WHEN** the acceptance item expects exit code 2 and all three runs record the one-line startup block with the marker
- **THEN** the evidence passes without a structured result
- **TEST** `tests/test_check_dod.py::test_expected_exit_two_is_checked_as_plain_text`

#### Scenario: Expected exit code 1 still requires the result
- **WHEN** the acceptance item expects exit code 1 and the runs record plain text
- **THEN** each run fails with the "not the run's result JSON" error
- **TEST** `tests/test_check_dod.py::test_expected_exit_one_still_requires_the_result_json`

#### Scenario: Non-orchestration JSON remains unchanged
- **WHEN** another command records arbitrary JSON
- **THEN** existing substring and exit checks apply without requiring result keys
- **TEST** `tests/test_check_dod.py::test_non_orchestrate_json_is_unaffected`

### Requirement: Structured parent confirmations end as the acceptance item expects
A parent confirmation records no exit code. The checker MUST judge it by the `expect_exit_code` (default 0) of the acceptance item whose command it re-runs. When that expected exit code is 0, a structured confirmation MUST have `outcome: COMPLETE` and review status exactly `PASS`. When it is not 0, the story proves a run that does not complete: a structured confirmation MUST NOT have `outcome: COMPLETE`, and its review status is not checked. In both cases the recorded `at` MUST be within the result window with five-minute tolerance, and the `run_id` rules apply. An honest story whose acceptance is a blocked run MUST be able to pass.

#### Scenario: BLOCKED confirmation is rejected
- **WHEN** parent confirmation records a structured BLOCKED result
- **THEN** the evidence fails naming the outcome and COMPLETE requirement
- **TEST** `tests/test_check_dod.py::test_blocked_structured_parent_confirmation_fails`

#### Scenario: Confirmation outside result window is rejected
- **WHEN** a COMPLETE/PASS confirmation's recorded time is outside the tolerated window
- **THEN** the evidence fails naming `at` and the result window
- **TEST** `tests/test_check_dod.py::test_structured_parent_confirmation_time_must_be_in_window`

#### Scenario: Honest blocked story passes
- **WHEN** the acceptance item expects exit code 1 and the first run, the fresh run and the confirmation are three BLOCKED results with distinct run ids and review status MISSING
- **THEN** the evidence passes
- **TEST** `tests/test_check_dod.py::test_honest_blocked_story_with_expected_exit_one_passes`

#### Scenario: COMPLETE confirmation fails a story that expects a blocked run
- **WHEN** the acceptance item expects exit code 1 and the confirmation is a COMPLETE result
- **THEN** the evidence fails naming the outcome and the expected exit code
- **TEST** `tests/test_check_dod.py::test_complete_confirmation_fails_a_story_that_expects_exit_one`

#### Scenario: Expected exit code 0 is unchanged
- **WHEN** the acceptance item expects exit code 0 and the confirmation is a BLOCKED result with review status MISSING
- **THEN** the evidence fails naming COMPLETE and PASS
- **TEST** `tests/test_check_dod.py::test_expected_exit_zero_confirmation_still_needs_complete_and_pass`

#### Scenario: Review status is exactly PASS
- **WHEN** the review status is `pass`, `Pass`, `PASSED` or `PASS` with a trailing space
- **THEN** the acceptance run and the confirmation each fail naming the status and `PASS`
- **TEST** `tests/test_check_dod.py::test_review_status_must_be_exactly_pass`

#### Scenario: Plain text remains supported
- **WHEN** raw output is a transcript or unrelated JSON without all result keys
- **THEN** existing substring validation is used without structured-result checks
- **TEST** `tests/test_check_dod.py::test_non_structured_raw_output_is_unchanged`

### Requirement: Structured orchestration results use distinct run ids
Structured results used by two different runs MUST have different `run_id` values: the first run, the fresh run and the parent confirmation of a story are three runs. Reuse MUST fail with `the same run_id cannot serve as two runs: <first location> and <second location>`, naming both run locations, for example `acceptance[0].fresh_run and parent_confirmation`.

One run cited by two acceptance criteria is one run. Acceptance runs of different criteria with the same `at`, command, raw-output path and raw-output SHA-256 MUST count once. The first and the fresh run of one criterion are always two runs.

#### Scenario: Reused run id is rejected
- **WHEN** two structured run outputs have the same run_id but different bytes
- **THEN** the evidence fails with the repeated-run-id message
- **TEST** `tests/test_check_dod.py::test_same_run_id_cannot_serve_as_two_structured_runs`

#### Scenario: Confirmation with the fresh run's id is rejected
- **WHEN** the parent confirmation is a different file that carries the fresh run's run_id
- **THEN** the evidence fails naming `acceptance[0].fresh_run and parent_confirmation`
- **TEST** `tests/test_check_dod.py::test_structured_confirmation_cannot_carry_the_fresh_run_id_in_other_bytes`

#### Scenario: Two criteria cite the same structured runs
- **WHEN** a second acceptance criterion cites the same first and fresh structured runs as the first criterion
- **THEN** the evidence passes
- **TEST** `tests/test_check_dod.py::test_two_criteria_may_cite_the_same_structured_runs`

#### Scenario: Distinct run ids pass
- **WHEN** each structured run output has a different run_id
- **THEN** the evidence remains valid
- **TEST** `tests/test_check_dod.py::test_distinct_structured_run_ids_pass`

### Requirement: Definition-of-done evidence uses distinct run outputs
Two different runs among the acceptance runs and the parent confirmation MUST NOT point at the same raw-output file, however its path is spelled. Two structured orchestration results used by two different runs MUST NOT share a raw-output SHA-256 either: a result carries its run's id and times, so two runs never produce the same bytes. Each failure MUST read `the same raw output cannot serve as two runs: <first location> and <second location>`.

The digest rule applies only to structured orchestration results. A deterministic command prints the same bytes on every run, so two runs with non-structured output MAY have byte-identical output as long as they are different files; this keeps the behavior that existed before this change. One run cited by two acceptance criteria counts once, as defined for run ids.

Each raw-output file MUST be read once per run citation, and a missing, mismatched or credential-bearing artifact MUST be reported once.

#### Scenario: Reused output is rejected
- **WHEN** two acceptance runs or an acceptance run and parent confirmation point to the same raw artifact
- **THEN** the evidence fails with the duplicate-output message naming both run locations
- **TEST** `tests/test_check_dod.py::test_same_raw_output_cannot_serve_as_two_runs`
- **TEST** `tests/test_check_dod.py::test_plain_text_confirmation_cannot_reuse_an_acceptance_artifact`
- **TEST** `tests/test_check_dod.py::test_structured_confirmation_cannot_point_at_the_fresh_run_artifact`
- **TEST** `tests/test_check_dod.py::test_one_plain_text_file_under_two_path_spellings_is_still_reuse`

#### Scenario: Identical plain-text bytes in two files pass
- **WHEN** the first run, the fresh run and the confirmation of a non-structured command are three files with the same bytes
- **THEN** the evidence passes
- **TEST** `tests/test_check_dod.py::test_plain_text_runs_with_identical_bytes_in_two_files_pass`

#### Scenario: Identical structured results in two files fail
- **WHEN** two runs record byte-identical structured results in two files
- **THEN** the evidence fails with the duplicate-output message
- **TEST** `tests/test_check_dod.py::test_structured_results_with_identical_bytes_in_two_files_fail`

#### Scenario: Two criteria cite the same plain-text runs
- **WHEN** a second acceptance criterion cites the same first and fresh runs as the first criterion
- **THEN** the evidence passes; the same file recorded at two different times still fails
- **TEST** `tests/test_check_dod.py::test_two_criteria_may_cite_the_same_plain_text_runs`
- **TEST** `tests/test_check_dod.py::test_one_artifact_recorded_as_runs_at_two_times_is_still_reuse`

#### Scenario: A fresh run copied from the first run is reuse
- **WHEN** the fresh run of a criterion is an exact copy of its first run
- **THEN** the evidence fails with the duplicate-output message
- **TEST** `tests/test_check_dod.py::test_fresh_run_copied_from_first_run_is_reuse_not_one_run_cited_twice`

#### Scenario: A missing artifact is reported once
- **WHEN** an acceptance run or the confirmation points at a file that does not exist
- **THEN** the evidence fails with exactly one "is missing" error for it
- **TEST** `tests/test_check_dod.py::test_missing_artifact_is_reported_once`

#### Scenario: Distinct outputs pass
- **WHEN** every acceptance and confirmation run has a distinct raw artifact
- **THEN** the evidence remains valid
- **TEST** `tests/test_check_dod.py::test_distinct_raw_outputs_are_valid`