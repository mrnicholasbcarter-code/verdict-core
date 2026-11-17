# Tasks

Owned files: `scripts/check_dod.py`, `tests/test_check_dod.py`, `AGENTS.md`; this change.

## 1. Spec
Tests/verification: run both pinned OpenSpec validators and require exit 0.
- [x] 1.1 Proposal, design and the `repository-tooling` spec delta are written. Verification: both pinned validators exit 0.
- [ ] (post-merge) Validate with both repository and pinned OpenSpec validators on merged main. Verification: command exit codes and output are recorded.

## 2. Tests first
- [x] 2.1 Add failing and passing tests for outcome and exit-code agreement. Verification: focused pytest shows red before implementation and green after.
- [x] 2.2 Add failing and passing tests for the five-minute result window. Verification: focused pytest shows red before implementation and green after.
- [x] 2.3 Add failing and passing tests for PASS review status. Verification: focused pytest shows red before implementation and green after.
- [x] 2.4 Add failing and passing tests for duplicate raw-output digests. Verification: focused pytest shows red before implementation and green after.
- [x] 2.5 Add a non-JSON raw-output regression test. Verification: focused pytest passes before and after implementation.
- [x] 2.6 Add command-keyed `verdict orchestrate --json` structured-stdout tests, including empty, noisy, valid, and non-orchestration commands. Verification: focused pytest passes.
- [x] 2.7 Add structured parent-confirmation tests and the three hand-built key-shape regression fixture. Verification: focused pytest passes.
- [x] 2.8 Add broad wrapper command-matcher coverage. Verification: focused pytest passes for env and timeout wrappers.

## 3. Implementation
- [x] 3.1 Parse structured orchestration results, require them for `verdict orchestrate --json`, and enforce run truth. Verification: focused pytest and strict mypy pass.
- [x] 3.2 Reject duplicate raw outputs across distinct evidence runs. Verification: focused pytest passes with the exact required error.
- [x] 3.3 Validate structured parent confirmations without an exit-code field. Verification: focused pytest passes for BLOCKED and out-of-window confirmations.
- [x] 3.4 Require command-keyed result JSON for `verdict orchestrate --json`. Verification: focused pytest passes for hand-built summaries, empty output, leading noise, and real-shaped output.
- [x] 3.5 Fail closed on unusable structured timestamps and inverted intervals. Verification: focused pytest passes for wrong types, missing timezones, and reversed bounds.
- [x] 3.6 Reject repeated structured run_id values across evidence runs. Verification: focused pytest passes for repeated and distinct ids.

## 4. Docs
Tests/verification: inspect the exact AGENTS.md sentence and run the repository documentation checks.
- [x] 4.1 Document orchestration run-truth validation in AGENTS.md. Verification: doc-link and diff checks pass.

## 5. Break-test fixes
Tests/verification: each test is written first and fails before its fix; focused pytest passes after.
- [x] 5.1 Match every orchestrate JSON command form: `--json` prefixes, `python -m verdict.cli`, shell punctuation, and a whitespace fallback when the command cannot be parsed. Verification: `test_orchestrate_command_matcher_forms` and `test_hand_built_summary_fails_under_every_orchestrate_json_form` pass.
- [x] 5.2 Never raise on raw output: oversized integers, deep nesting, time overflow, a `run_id` that is not a non-empty string, text stdout cannot encode. Verification: one test per cause passes, and unparseable output of another command stays plain text.
- [x] 5.3 Let the acceptance item's expected exit code govern the parent confirmation and the structured-stdout requirement. Verification: honest blocked stories pass for expected exit codes 1 and 2; expected exit code 0 is unchanged.
- [x] 5.4 Count one run cited by two acceptance criteria once, and name both run locations in the reuse messages. Verification: the two-criteria tests pass for plain text and structured results; the reuse tests still fail.
- [x] 5.5 Apply digest uniqueness only to structured results and restore the shared test fixture to its `origin/main` form. Verification: the unmodified `origin/main` test file passes against this checker.
- [x] 5.6 Add tests for both directions of each run-truth rule. Verification: each of the seven surviving mutations fails at least one test.
- [x] 5.7 Read each run artifact once. Verification: `test_missing_artifact_is_reported_once` and `test_each_run_citation_reads_its_artifact_once` pass.
- [x] 5.8 Update the spec delta, design and proposal to match. Verification: both pinned OpenSpec validators exit 0.

## 6. Verification
Tests/verification: execute every acceptance command from the worktree root and record each exit code.
- [x] 6.1 Focused and repository gates pass on the current branch. Verification: the focused tests, ruff, format, mypy, DoD, doc links, reachability, diff, and both OpenSpec validators exit 0.
- [ ] (post-merge) Run the full definition-of-done and repository gates on merged main. Verification: all acceptance commands exit 0 and raw summaries are recorded.
