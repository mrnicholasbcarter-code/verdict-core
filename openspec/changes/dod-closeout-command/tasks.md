# Tasks

Build plan for story 1 of #1308: the closeout command. Every scenario in the spec deltas has one test,
named `test_` plus the scenario title in snake case; the names are listed below, and a scenario with no
name here is a defect in this plan. Decision references (D1 to D10) are to design.md.

Conventions for every task:

- Until #1306 merges (PR #1311 was open on 2026-11-25), run tests and any command that writes state with
  `TESTENV='env HOME=/tmp/dod1-home VERDICT_HOME=/tmp/dod1-home/.verdict'`.
- Test first: show each test failing before its code exists, and each guard's test failing with the guard
  removed, then passing. Put the output in the PR.
- No live or paid call and no real `gh` or `git push` in a test: every spawn goes through one function
  (task 6.1) that tests replace with a fake, except the tests that name a real child. The fake installed main
  is a temporary git repository with a stub `.venv/bin/python` that prints canned output per argument list; the
  clock, `gh` and `check_dod` path are parameters. The shared fixture is `tests/helpers/closeout_fixture.py`.
- A Verify command names a file or a class, never `-k`, so it runs exactly the tests the task names.
- Modules are `verdict/closeout/{allowlist,declaration,identity,execute,ledger,cycle,draft,cli}.py`.

## 1. Preconditions

- [ ] 1.1 Re-read #1311 [U]. If it is merged, drop `TESTENV`. Read `tests/helpers/home_guard.py` and
      `tests/helpers/home_snapshot.py`; record the real names of the guard's stand-in for the real home
      and of the snapshot and diff helpers in design.md D10, and adjust tasks 3.2 and 10.2. Files:
      design.md. Verify: `gh pr view 1311 --json state` and `git log origin/main --oneline --
      tests/helpers/home_guard.py`.
- [ ] 1.2 Fix the initial allowlist. Read `cmd_doctor` and the `eligibility` handler: confirm that
      `doctor --json` and `eligibility --no-refresh` with `--json --no-pager --verified --frontier --reasoning`
      write nothing, that each flag exists, and what exit code means green (doctor exits 1 while an issue
      remains). Drop any flag that fails. Record handler names in design.md D5. Files: design.md. Verify:
      `$TESTENV uv run --frozen python -m verdict doctor --help` and the same for `eligibility`, and the
      handler lines cited in design.md.

## 2. `check_dod.py --draft` (D9; repository-tooling delta)

- [ ] 2.1 Draft mode. Schema validated with `parent_confirmation` not required (so no early return), the
      confirmation block and `_openspec` skipped, a present confirmation and the `not_applicable` checks
      kept, output `DRAFT OK (pending: parent_confirmation, openspec archive)` or `DRAFT FAILED`, no `DONE`.
      Files: `scripts/check_dod.py`, `tests/test_check_dod.py`. Verify:
      `$TESTENV uv run --frozen python -m pytest -q -n 2 tests/test_check_dod.py::TestDraftMode` with
      `test_draft_without_a_confirmation_passes_when_everything_else_holds`, `test_draft_never_prints_done`,
      `test_draft_with_a_missing_confirmation_does_not_return_early`,
      `test_draft_still_rejects_a_wrong_digest`, `test_draft_still_rejects_an_ancestry_violation`,
      `test_draft_still_rejects_a_fresh_run_inside_the_window`,
      `test_draft_still_rejects_an_unredacted_output`,
      `test_draft_still_checks_a_confirmation_that_is_present`. The existing tests of the file keep passing
      unchanged.
- [ ] 2.2 Roots and shapes. `--evidence-root` resolves raw outputs (paths still `docs/proof/dod/...`), git
      stays in `--root`; refuse `--draft` with `--all`, without `--evidence-root`, or with a root inside
      `--root`; refuse `--evidence-root` without `--draft`. Files: `scripts/check_dod.py`,
      `tests/test_check_dod.py`. Verify: same command with `::TestDraftRoots`:
      `test_draft_reads_outputs_from_the_evidence_root_and_git_from_the_repository_root`,
      `test_draft_is_refused_in_the_wrong_shapes`, `test_strict_mode_is_unchanged`. Also run
      `$TESTENV uv run --frozen python scripts/check_dod.py --all` and attach the unchanged result.

## 3. The allowlist (D5)

- [ ] 3.1 Matcher and table. `match(argv)` returns the entry or raises a named refusal; exact tokens, required
      and optional flags, no abbreviation, no `=value`, no value flag, no positional; a deny set by name for
      `orchestrate`, `autodev`, `bench`, `setup`, `serve`, `acceptance`, `--fix`, `--allow-live*`. Files:
      `verdict/closeout/allowlist.py`, `tests/test_closeout_allowlist.py`. Verify:
      `$TESTENV uv run --frozen python -m pytest -q -n 2 tests/test_closeout_allowlist.py::TestMatcher` with
      `test_unlisted_subcommand_is_refused`, `test_unlisted_or_abbreviated_flag_is_refused`,
      `test_missing_required_flag_is_refused`, `test_live_and_mutating_commands_are_refused`. Each test also
      asserts the refusal names the token.
- [ ] 3.2 Read-only proof per entry. Parametrised over the table: run `python -m verdict <entry>` as a real
      child with `HOME` and `VERDICT_HOME` in a temporary directory, snapshot before and after (task 1.1's
      helper). Files: `tests/test_closeout_allowlist.py`. Verify: same file `::TestEntriesReadOnly` with
      `test_each_allowlist_entry_leaves_a_temporary_home_unchanged`; show it failing against a temporary
      entry that writes a file.

## 4. Declarations (D1)

- [ ] 4.1 Schema and loader. `proof/closeout.schema.json` (`additionalProperties: false` at every level,
      `freshness_window_hours` minimum 1, `timeout_seconds` maximum 600, `live` const false, `kind` const
      `product`); the loader adds the checks a schema cannot: story equals file stem, `argv` matched by the
      allowlist, no host path or redaction change in `expect` or marker, and one bad story never stops another.
      Files: `proof/closeout.schema.json`, `verdict/closeout/declaration.py`,
      `tests/test_closeout_declaration.py`. Verify: `$TESTENV uv run --frozen python -m pytest -q -n 2
      tests/test_closeout_declaration.py::TestDeclaration` with `test_valid_declaration_is_accepted`,
      `test_window_below_one_hour_is_refused`, `test_unknown_key_is_refused`,
      `test_argv_given_as_a_string_is_refused`,
      `test_story_name_must_be_a_plain_name_equal_to_the_file_name`, `test_live_declaration_is_refused`,
      `test_host_path_or_secret_in_the_expectation_is_refused`,
      `test_one_bad_declaration_stops_only_its_own_story`. Each refusal test asserts the field is named.

## 5. Installed main and the merge commit (D2, D3)

- [ ] 5.1 Identity. Root from the running package, the acceptance unit's `WorkingDirectory=` read from the unit
      directory (inverse of `_escape_path`), full `HEAD`, clean tree, all git as `--no-optional-locks`. Files:
      `verdict/closeout/identity.py`, `tests/test_closeout_identity.py`. Verify: `$TESTENV uv run --frozen
      python -m pytest -q -n 2 tests/test_closeout_identity.py::TestInstalledMain` with
      `test_development_checkout_is_refused`,
      `test_unit_file_missing_or_without_a_working_directory_is_refused`,
      `test_dirty_installed_main_is_refused`, `test_unrecorded_build_is_refused`,
      `test_identity_checks_leave_the_installed_index_untouched` (stale stat time; compare index bytes; fails
      with plain `git status`).
- [ ] 5.2 Merge commit. `lookup_merge(pr, spawn)` with a validated integer, the answer validated by shape the
      way a real loader would, then the local existence and ancestor tests. Files:
      `verdict/closeout/identity.py`, `tests/test_closeout_identity.py`. Verify: same file
      `::TestMergeCommit` with `test_merged_ancestor_proceeds`, `test_unmerged_pull_request_is_refused`,
      `test_merge_commit_absent_from_installed_main_waits`,
      `test_merge_commit_that_is_not_an_ancestor_is_refused`, `test_malformed_lookup_result_is_refused`
      (one case each: not JSON, missing key, null, wrong type, non-hex `oid`, timeout; none spawns a story
      command).

## 6. Spawn, scrub and judge (D4, D6)

- [ ] 6.1 The one spawn function. Kind plus argument list, `shell=False`, new session, closed stdin, timeout,
      process-group kill on every exit, the environment rule, interpreter resolved inside the installed root.
      Files: `verdict/closeout/execute.py`, `tests/test_closeout_execute.py`. Verify: `$TESTENV uv run
      --frozen python -m pytest -q -n 2 tests/test_closeout_execute.py::TestSpawn` with
      `test_allowlisted_command_runs_from_installed_main`,
      `test_interpreter_outside_installed_main_is_refused`,
      `test_child_environment_carries_no_consent_switch`, `test_timeout_kills_the_process_group` (real stub
      script that forks a sleeping child; assert no survivor).
- [ ] 6.2 Scrub and judge. `redact_text`, `$HOME` to `~`, `/home/<name>` to `/operator`, 1 MiB cap, then
      `expect`, marker and exit code on the scrubbed text. Files: `verdict/closeout/execute.py`,
      `tests/test_closeout_execute.py`. Verify: same file `::TestScrub` with `test_echoed_secret_is_not_stored`
      (the secret is searched for in the output file, the ledger, the report and the captured log),
      `test_home_path_is_rewritten`, `test_stored_output_is_stable_under_redaction`,
      `test_expected_text_is_judged_after_scrubbing`, `test_oversized_output_fails_the_step`.

## 7. The ledger and the write helper (D7, D10)

- [ ] 7.1 Chain. Append, replay, verify chain and output digests, write output before entry, `flock`.
      Files: `verdict/closeout/ledger.py`, `tests/test_closeout_ledger.py`. Verify: `$TESTENV uv run --frozen
      python -m pytest -q -n 2 tests/test_closeout_ledger.py::TestLedger` with
      `test_edited_exit_code_is_refused`, `test_edited_output_file_is_refused`,
      `test_removed_or_reordered_middle_entry_is_refused`; each names the first bad `seq`.
- [ ] 7.2 Write helper. Join onto the closeout home, refuse absolute, `..` and symlink escape; every module
      writes through it and nowhere else. Files: `verdict/closeout/ledger.py`,
      `tests/test_closeout_ledger.py`. Verify: same file `::TestWriteHelper` with
      `test_escaping_path_is_refused` (absolute, `..`, symlinked story directory, symlinked `runs/`).

## 8. The cycle and the state machine (D8)

- [ ] 8.1 One cycle. Steps in order (`on_main` once, items, `doctor`), every step runs, records from what
      executed. Files: `verdict/closeout/cycle.py`, `tests/test_closeout_cycle.py`. Verify: `$TESTENV uv run
      --frozen python -m pytest -q -n 2 tests/test_closeout_cycle.py::TestCycle` with
      `test_installed_sha_is_recorded_not_the_merge_sha`,
      `test_first_run_records_the_installed_sha_time_exit_code_and_output_digest`,
      `test_failing_command_keeps_its_output_and_yields_no_draft`, `test_spawn_failure_records_no_run`,
      `test_corrupt_ledger_stops_only_its_story`.
- [ ] 8.2 Window. Derived from the ledger by replay; injected clock; no option sets it. Files:
      `verdict/closeout/cycle.py`, `tests/test_closeout_cycle.py`. Verify: same file `::TestWindow` with
      `test_run_before_the_window_spawns_nothing` (59 minutes against 1 hour; the fake spawn counts zero),
      `test_run_after_the_window_records_a_fresh_run`, `test_window_counts_from_the_passing_first_run`,
      `test_changed_declaration_voids_the_first_run`.
- [ ] 8.3 Retries and reset. At most three failed cycles in all, `needs_person`, `reset` as a ledger entry.
      Files: `verdict/closeout/cycle.py`, `tests/test_closeout_cycle.py`. Verify: same file `::TestRetries`
      with `test_failed_cycle_is_retried_on_the_next_invocation`,
      `test_third_failed_cycle_leaves_the_story_for_a_person` (a fourth invocation spawns nothing),
      `test_reset_resumes_a_story_and_keeps_its_history`, and `::TestNoClockOption` with
      `test_no_option_changes_the_clock_or_the_window`.

## 9. The draft (D9)

- [ ] 9.1 Builder and report. Assemble the JSON from verified entries only, copy outputs byte for byte,
      write `report.md`, run `check_dod.py --draft --root <installed> --evidence-root <draft>` of installed
      main, move a rejected candidate to `rejected/`, append the `draft` entry. Files:
      `verdict/closeout/draft.py`, `tests/test_closeout_draft.py`. Verify: `$TESTENV uv run --frozen python
      -m pytest -q -n 2 tests/test_closeout_draft.py::TestDraft` with
      `test_complete_story_yields_a_draft_that_passes_the_draft_check`,
      `test_draft_carries_no_confirmation`,
      `test_strict_check_fails_on_the_draft_for_confirmation_and_archive_only` (runs the plain
      `check_dod.py` on the draft, with and without a stub confirmation),
      `test_too_early_fresh_run_fails_the_draft_check` (hand-built draft),
      `test_no_draft_for_a_run_that_did_not_happen` (first cycle only; corrupt ledger; `spawn_error`),
      `test_failed_runs_are_listed_in_the_report_and_not_in_the_evidence`.

## 10. The command and its boundary (D8, D10)

- [ ] 10.1 CLI. `verdict closeout run|status|reset`, parser and dispatch, exit codes 0 to 3, lock, status
      with no spawn and no write. Files: `verdict/closeout/cli.py`, `verdict/commands/parsers_closeout.py`,
      `verdict/commands/dispatch.py`, `verdict/cli.py`, `tests/test_closeout_cli.py`. Verify: `$TESTENV uv run
      --frozen python -m pytest -q -n 2 tests/test_closeout_cli.py::TestCli` with
      `test_status_spawns_nothing_and_writes_nothing`, `test_second_invocation_exits_busy`.
- [ ] 10.2 Boundary proofs. Files: `tests/test_closeout_cli.py`. Verify: same file `::TestBoundary` with
      `test_a_full_cycle_writes_only_under_the_closeout_home` (both trees diffed, index identical),
      `test_only_the_five_process_kinds_are_spawned` (every recorded call classified; none is a GitHub write
      or a git write verb), `test_real_home_is_refused_by_the_test_guard` (task 1.1's stand-in). Show each
      failing when the code writes outside the home or spawns a sixth kind.
- [ ] 10.3 Pilot declaration. Add `proof/closeout/<story>.yaml` for one story already merged whose acceptance
      command is on the allowlist and that has no evidence file; the controller names the story in the PR. If
      none qualifies, stop and report it as blocked rather than inventing one. This is the source of the
      post-merge checks; it is not the backfill. Files: `proof/closeout/<story>.yaml`. Verify:
      `$TESTENV uv run --frozen python -m verdict closeout status --json` lists it as loaded, and
      `scripts/check_dod.py --all` is unchanged.

## 11. Docs and gates

- [ ] 11.1 Docs. `docs/guides/dod-closeout.md` (what runs, what it writes, exit codes, the allowlist, what
      is story 2 and 3, how a person confirms), linked from the AGENTS.md "Definition of done" section;
      describe only what the code does. Files: `docs/guides/dod-closeout.md`, `AGENTS.md`. Verify:
      `uv run --frozen python scripts/check_doc_links.py`.
- [ ] 11.2 Gates, run once on the final commit. Files: none. Verify: `uv run --frozen ruff check . && uv run
      --frozen ruff format --check .`, `uv run --frozen mypy verdict --strict`, `uv run --frozen python
      scripts/check_reachability.py --ratchet-base origin/main` (every new module has a production caller
      through the CLI), `uv run --frozen python scripts/check_lint_gates.py`, `git diff --check`, and the
      focused test files above with `-n 2`.

## 12. Live checks on merged main

Each task below is its own verification: the output it names is the proof, recorded in the PR.

- [ ] (post-merge) 12.1 After installed main is reinstalled at or past the merge (by hand until story 2),
      from installed main run `verdict closeout status`: the pilot is listed `not_started`. Record the raw
      output in the PR.
- [ ] (post-merge) 12.2 Run `verdict closeout run` from installed main: the identity check passes (exit not 2),
      the ledger holds `first` entries whose `sha` equals `git -C <installed main> rev-parse HEAD`,
      `grep -r /home/` over the story's directory finds nothing, and a second `run` inside the window spawns
      nothing.
- [ ] (post-merge) 12.3 After one window, `verdict closeout run` records the fresh run and a draft;
      `check_dod.py --draft` exits 0 on it, and the plain `check_dod.py` fails only on the missing
      confirmation and the archive. Record the output; a person or separate agent then confirms, and the
      story's own DoD evidence follows.
