# Tasks

Build plan for OC2a (#1301). #1299 (OC1) has merged; task 1.1 records the drift since this plan was
written. Every spec scenario has one test below, named `test_` plus the scenario title in snake case; a
scenario with no test here is a defect in this plan.

Conventions for every task:

- Until #1306 merges, run tests and any command that writes state with
  `TESTENV='env HOME=/tmp/oc2a-home VERDICT_HOME=/tmp/oc2a-home/.verdict'`.
- Test first: show each test failing before its code exists (and each guard's test failing with
  the guard removed), then passing. Put the output in the PR.
- Tests that call `run_autodev` pass `mechanical=False` and `store=ReceiptStore(":memory:")`
  (the mechanical tier runs ruff in the user's tree and the default ledger writes
  `repo/.verdict/receipts.db`), except the tests that exercise the mechanical tier, which use a
  fake `runner`.
- No test calls a live model, a probe or the real `opencode`: the OpenCode executor is a stub
  with `async run(...)` or a fake binary. Faults and races are injected by monkeypatching the
  one `os` call, or through a `hooks` mapping (phase name to callable) on the function under
  test; no sleeps. An interrupt test patches that one call so that it performs the real operation
  and then raises `KeyboardInterrupt`. Where a test compares "the complete tree", it uses the
  definition of byte-identical in the spec delta ("before the attempt" is the moment `execute_unit`
  is called) and compares directory sets too.
- Each task's tests are one class in the test file, named on its Verify line (`file::Class`). A
  Verify command never uses `-k`: it names a file or a class, so it runs exactly the tests the task
  names. Task 7.1 runs the complete files.
- Only tasks 1.1, 3.2 and 5.1 import OC1 code. Tasks 2 to 4 otherwise depend on the stub contract only;
  task 3.2 pins that stub's parameters to the real `OpenCodeExecutor.run`.
- Tests of the readiness check and the CLI pin `VERDICT_OPENCODE_BIN`, `VERDICT_SERVE_URL` and the
  serve-token and key providers to fakes: this host has opencode installed and `verdict serve` running,
  and a test that reaches either would pass for the wrong reason.

## 1. Preconditions

- [x] 1.1 Confirm #1299 is merged to main. Re-read what the design took from it [U]: the
      `OpenCodeExecutor.run(prompt, *, route_id, cwd, timeout_seconds)` signature and that it
      never raises, the `harness`, `env=` and binary-resolver symbols, `PINNED_VERSION`, the
      context file and its byproduct handling, the serve base-URL setting and the credential
      source. Record each difference in design.md under "Drift after #1299" and adjust tasks 3
      and 5 before coding. Re-read #1313's state. Files: design.md. Verify:
      `git log origin/main --oneline -- verdict/orchestration/executor_opencode.py` lists the
      OC1 merge, and `$TESTENV uv run --frozen python -c "from verdict.orchestration.executor_opencode import OpenCodeExecutor, PINNED_VERSION"` exits 0.
      Evidence: the log lists `b0010c10` (OC1) and `44be580c` (#1339), the import exits 0 and prints
      `2.3.2`; design.md "Drift after #1299" records #1336, #1338 and #1339 and the adjustments to
      tasks 3 and 5 are made in this commit.
- [x] 1.2 Carried review finding 1, ignored owned new files (Codex on #1319). Spec scenarios
      "Ignored owned new file", "Ignored and plain owned new files", "Ignored absent owned file
      refused" and "Ignored and plain absent owned files refused". Both remedies the finding offers are
      built: `preflight` refuses an owned absent path that an ignore rule matches (the policy: `git
      status` and so `changed_files` would not show it), and `attempt_delta` never drops an owned path as
      a byproduct (the fail-safe). Ticked when the tests named in tasks 2.2 and 3.3 pass and each fails
      with its guard removed. Files: none of its own. Verify:
      `$TESTENV uv run --frozen python -m pytest -q -n 2 "tests/test_autodev_opencode.py::TestDelta::test_ignored_owned_new_file" "tests/test_autodev_opencode.py::TestDelta::test_ignored_and_plain_owned_new_files" "tests/test_autodev_opencode.py::TestPreflight::test_ignored_absent_owned_file_refused" "tests/test_autodev_opencode.py::TestPreflight::test_ignored_and_plain_absent_owned_files_refused"`.
      Evidence: `TestDelta::test_ignored_owned_new_file`, `TestDelta::test_ignored_and_plain_owned_new_files`, `TestPreflight::test_ignored_absent_owned_file_refused` and `TestPreflight::test_ignored_and_plain_absent_owned_files_refused` pass; with the owned-path precedence removed (`delta-owned-precedence-off`) or the ignored-absent refusal removed (`pre-absent-ignored-off`) they fail.
- [x] 1.3 Carried review finding 2, created-directory identity on rollback (Codex on #1319). Spec scenario
      "Created directory replaced" and the identity rule in the restore requirement. The transfer records
      device and inode of each directory it creates; restore checks them before removing it and treats a
      replacement as not restored. Ticked when the test named in task 2.4 passes and fails with the
      identity check removed. Files: none of its own. Verify:
      `$TESTENV uv run --frozen python -m pytest -q -n 2 "tests/test_autodev_opencode.py::TestRestore::test_created_directory_replaced"`.

      Evidence: `TestRestore::test_created_directory_replaced` passes; with the identity comparison removed it fails (mutation `restore-dir-identity-off`).
## 2. The attempt tree (no OpenCode needed)

- [x] 2.1 Seed (D2, D3). `seed_attempt_tree(repo, parent_dir, owned_files)`: `--no-local` clone,
      remove `origin`, overlay from `git status --porcelain=v1 -z --untracked-files=all`
      (a status with `R` or `C` consumes the following source record: copy the destination's
      working-tree bytes, remove the source when the user's tree lacks it), take M0, refuse by
      name for no HEAD, a submodule, a nested repository, an unsafe component or a limit.
      Files: `verdict/autodev_opencode.py`, `tests/test_autodev_opencode.py`. Verify:
      `$TESTENV uv run --frozen python -m pytest -q -n 2 tests/test_autodev_opencode.py::TestSeed`
      with `test_clone_shares_nothing_with_the_repository`, `test_dirty_state_is_carried_over`,
      `test_staged_rename` (`git mv`; the clone has `new.py` and no `old.py`),
      `test_staged_rename_whose_destination_was_then_modified` (`git mv` then edit; the clone
      has the edited bytes and no `old.py`), `test_owned_files_hold_working_tree_bytes` (an
      `eol` attribute), `test_seed_refused_before_any_worker_starts` (one case per refusal, no
      directory left). The rename tests and the dirty-state test also assert the user's
      repository is byte-identical after seeding, and a `-z` record for a path containing a
      space parses as one path.
      Evidence: `TestSeed` green; each guard (clone sharing, remote, rename source, owned bytes from the user's tree, deleted files, nested repository, submodule, no HEAD, `.git` component, seed limit) was removed in turn and its named test failed.
- [x] 2.2 Manifest and delta (D4). `attempt_delta(tree, m0, repo, unit)` builds M1 with a
      descriptor-relative walk (root opened `O_DIRECTORY`; `scandir` on each directory
      descriptor; children opened `O_DIRECTORY | O_NOFOLLOW` relative to their parent; files
      `O_NOFOLLOW | O_NONBLOCK` then `fstat`), applies the byproduct rule (new, unowned, ignored paths are dropped) through
      `git check-ignore --no-index -z --stdin` in the user's repository, then ownership on leaf
      entries, the regular-file rule and the empty-delta rule, and reads each delta file once.
      It never runs git in the tree. Files: `verdict/autodev_opencode.py`,
      `tests/test_autodev_opencode.py`. Verify: `$TESTENV uv run --frozen python -m pytest -q -n 2 tests/test_autodev_opencode.py::TestDelta`
      with `test_planted_git_configuration_is_inert`, `test_git_state_does_not_change_the_result`
      (edit, `git add`, `git commit`, revert), `test_a_new_owned_file_in_a_new_directory`,
      `test_directory_and_file_swapped`, `test_special_files_in_the_attempt_tree` (a FIFO would
      hang without `O_NONBLOCK`), `test_a_regular_file_replaced_by_a_fifo_during_validation` and
      `test_ancestor_directory_swapped_during_validation` (both through a `hooks` seam between
      the metadata read and the open), `test_ignored_byproduct` (a new path in a user-ignored directory is dropped),
      `test_ignored_owned_new_file` and `test_ignored_and_plain_owned_new_files` (task 1.2: an owned
      path is never dropped as a byproduct, alone or beside a plain owned file),
      `test_a_worker_cannot_hide_a_path_by_editing_gitignore`; plus
      `test_ignored_path_present_at_seed_is_not_a_byproduct`, `test_owned_delete_is_in_the_delta`,
      `test_mode_only_change_is_in_the_delta`, `test_symlink_in_an_owned_path_is_rejected`,
      `test_delta_hash_mismatch_is_error`, `test_file_over_the_limit_is_a_named_refusal` and
      `test_tree_over_the_limit_is_a_named_refusal`.
      Evidence: `TestDelta` green; removing the `.git` skip, the ownership check, the ignore filter, the owned-path precedence, the worker-tree ignore read, the special-file rule, the empty rule, `O_NONBLOCK`, `O_NOFOLLOW`, the hash recheck or the file limit each failed its named test.
- [x] 2.3 Transfer (D6). `transfer(delta, user_repo, m0, journal, hooks=None)`: the read-only phase
      (each path resolved from a root descriptor with `O_DIRECTORY | O_NOFOLLOW`; the user's file
      opened relative to its parent with `O_NOFOLLOW | O_NONBLOCK` and checked by `fstat` before it
      is read; kind, mode and SHA-256, or absence, compared with M0; limits), then the write phase (each
      mutation (`mkdir` by descriptor, rename over a target, unlink) is appended to `journal`, with the
      path's prior and intended state, before the call that makes it, and a created directory's device
      and inode are recorded in its entry as soon as `mkdir` has returned; comparison and
      device and inode check repeated before each write; one `replace_file(parent_fd, name,
      data, mode)` helper that creates a unique `.verdict-transfer-*` file with
      `O_CREAT | O_EXCL | O_NOFOLLOW`, writes, sets the mode and renames, and unlinks the
      temporary file by the same descriptor on any failure before the rename succeeds, naming
      an unlink failure without replacing the original error), rolling back through restore
      on any failure after the first write. It refuses where `os.supports_dir_fd` lacks `open`,
      `stat`, `mkdir`, `rename` or `unlink`. Files: `verdict/autodev_opencode.py`,
      `tests/test_autodev_opencode.py`. Verify: `$TESTENV uv run --frozen python -m pytest -q -n 2 tests/test_autodev_opencode.py::TestTransfer tests/test_autodev_opencode.py::TestCrash`
      with `test_owned_file_changed_after_the_seed`, `test_file_appeared_after_the_seed`,
      `test_non_regular_file_in_the_user_s_tree`, `test_edit_between_the_checks_and_the_first_write`
      (through a `hooks` seam), `test_only_the_mode_changed`,
      `test_path_that_escapes_the_root_is_refused` (a crafted `..` or absolute delta path),
      `test_user_file_over_the_limit_is_refused`,
      `test_symlinked_parent`, `test_parent_swapped_for_a_symlink_during_the_transfer`,
      `test_parent_directory_renamed_away_during_the_transfer`,
      `test_a_refusal_on_a_later_path_leaves_no_directory_behind`,
      `test_late_conflict_after_earlier_paths_were_written`,
      `test_rollback_blocked_by_a_later_user_edit`, `test_failure_part_way`,
      `test_temporary_file_removed_after_a_failed_write` (parametrised over the write, the mode
      change and the rename raising `OSError(ENOSPC)`, each for an existing owned file and for a
      new owned file in a new directory; compares the complete tree and the directory set before
      and after), `test_temporary_file_removal_fails_too`, and the crash tests below.
      **Crash tests** (class `TestCrash`). `test_kill_after_the_temporary_write`,
      `test_kill_after_the_first_of_two_renames` and `test_kill_after_the_last_rename` run the
      transfer in a child process (`subprocess` with `sys.executable`; a helper beside the test
      file) over a repository whose owned files are tracked and unmodified. A `hooks` entry
      prints a ready line and blocks at the named phase; the test reads it, sends `SIGKILL`,
      then asserts: each owned path equals its prior or its new bytes (never torn), at most one
      `.verdict-transfer-*` file exists, `git status --porcelain` lists nothing but owned paths
      and that file, and `git checkout -- <owned paths>` plus removing the created files and
      empty directories leaves the repository byte-identical to its state when the unit started.
      Evidence: `TestTransfer` and `TestCrash` green (the crash tests SIGKILL a child process at the named phase); removing phase one, the pre-write recheck, the mode comparison, the non-regular rule, the unsafe-path rule, the held-directory check, no-follow plus the held check, the rollback, the size limit, the temporary-file cleanup or its naming each failed its named test.
- [x] 2.4 Restore (D7). `restore(journal)` undoes the current attempt's transfer in reverse with task 2.3's
      traversal, re-check and `replace_file` helper. For each entry it compares kind, executable bit
      and SHA-256 with the intended state (undo it), with the prior state (the mutation was never
      made: change nothing, report nothing) and otherwise leaves the path alone and reports it; it removes a created directory only when it is empty and its device and inode still equal the recorded
      ones (an entry whose identity was never recorded, see design D7, is removed only when it is an empty
      real directory), and is idempotent. Files: `verdict/autodev_opencode.py`,
      `tests/test_autodev_opencode.py`. Verify:
      `$TESTENV uv run --frozen python -m pytest -q -n 2 tests/test_autodev_opencode.py::TestRestore`
      with `test_created_files_and_directories_are_removed`,
      `test_user_edit_after_the_transfer_is_kept`,
      `test_user_mode_change_after_the_transfer_is_kept`,
            `test_created_directory_replaced` (task 1.3: the original directory is renamed away, an empty real
      directory takes its name, then the original is removed, so a reused inode number cannot hide the
      swap; restore leaves the replacement and names it),
      `test_parent_swapped_between_transfer_and_restore`, `test_restore_twice`,
      `test_restore_without_a_transfer`, `test_restore_write_failure_leaves_no_temporary_file`,
      `test_restore_of_a_modified_file_and_of_a_deleted_file` and the write-ahead tests
      `test_interrupt_right_after_a_rename`, `test_interrupt_right_after_a_deletion` and
      `test_interrupt_right_after_a_directory_creation` (each patches `rename`, `unlink` or `mkdir` to
      perform the real call and then raise `KeyboardInterrupt`, runs `transfer` with a `journal`,
      calls `restore(journal)`, and asserts the tree is byte-identical to before and that the
      exception that left the transfer is the `KeyboardInterrupt`), plus
      `test_interrupt_before_the_mutation` (the patched call raises without performing the
      operation: restore changes and reports nothing). The first three fail if the journal append
      is moved after the mutation; the fourth fails if restore reports a path that still holds its
      prior state.

      Evidence: `TestRestore` green; moving the journal append after the mutation (rename, deletion), removing the directory identity check, restoring over a user edit, reporting an unmade mutation, not removing directories, not emptying the journal and leaving a temporary file each failed their named test. `test_journal_is_emptied_at_the_start_of_each_attempt` moved to task 3.2 (it needs the executor).
## 3. The executor

- [x] 3.1 Prompt (D10). `build_opencode_prompt(unit)` names the objective, owned files,
      verification command and context pack, and the rules (edit only owned files, run the
      command, no commit or push). Files: `verdict/autodev_opencode.py`,
      `tests/test_autodev_opencode.py`. Verify: `$TESTENV uv run --frozen python -m pytest -q -n 2 tests/test_autodev_opencode.py::TestPrompt`
      with `test_prompt_carries_the_context_pack_unchanged` and
      `test_prompt_never_contains_the_credential` (a credential value placed in the environment).
      Evidence: `TestPrompt` green; dropping the context from the prompt, or putting the credential in it, fails its named test.
- [x] 3.2 `OpenCodeUnitExecutor.execute_unit` and the `PatchAttempt` mapping (D8). Seed, run the
      injected OpenCode executor with `route_id=config.model` and `cwd=<tree>`, take the delta,
      read it once, remove the tree, transfer (the last fallible step), and map to `applied`,
      `rejected` or `error` with the real reason; the tree is also removed in a `finally`.
      `config.model` is the route, sampling fields `None`, `response_headers` `{}`,
      `resolved_model` `None`, `isolated` true, `restore()` delegates to task 2.4. `begin_run(run_id)`
      stores the autodev run id; every OpenCode attempt passes it as `run_id` and the unit's `unit_id` as
      `node_id` (an executor without a run id returns `error` before any tree is made), and the attempt
      timeout is `ATTEMPT_TIMEOUT_SECONDS` (600; the constructor refuses below 120 or above 1,740).
      Files:
      `verdict/autodev_opencode.py`, `tests/test_autodev_opencode.py`. Verify:
      `$TESTENV uv run --frozen python -m pytest -q -n 2 tests/test_autodev_opencode.py::TestExecute`
      with `test_a_valid_owned_repair_is_applied` (usage `reported=False` when tokens are null),
      `test_worker_failed` (timeout, stall, serve refusal text, non-zero exit: nothing
      transferred although the tree holds edits), `test_worker_makes_no_change`,
      `test_attempt_tree_removed_on_every_exit_path` (applied, rejected, error, exception,
      cancellation), `test_tree_is_removed_before_the_transfer`, `test_running_event_loop_returns_error`,
            `test_journal_is_emptied_at_the_start_of_each_attempt` (an earlier unit's journal is gone before the
      next unit's first action), `test_no_run_id`, `test_attempt_timeout_bounds`,
      `test_stub_matches_the_real_run_signature` (the stub's parameters equal
      `inspect.signature(OpenCodeExecutor.run)` and the stub records `run_id` and `node_id`),
      `test_no_served_identity_is_claimed` and `test_credential_never_appears_in_a_record` (a stub
      terminal whose error text is already redacted while the credential sits in the
      environment; reason, outcome, attempt record and receipt payload contain the redacted text
      and never the credential).
      Evidence: `TestExecute` green (against the group 2 module the new tests all fail: 53 failed); removing the run id, the timeout bounds, the tree removal before the transfer, the journal clear, the not-ok guard, the loop check, the run-id check, the `finally` or the reported-usage rule fails its named test.
- [x] 3.3 Owned-file precondition (D5). `OpenCodeUnitExecutor.preflight(unit)` reads
      `git status --porcelain=v1 -z --untracked-files=all`, `git ls-files -v -z -- <owned>`,
      `git check-attr -z filter ident -- <owned>` and `git check-ignore -z --stdin` in the user's repository
      (read-only: `GIT_OPTIONAL_LOCKS=0`, literal pathspecs) and returns a reason
      naming the path and the remedy when an owned path is listed by status (modified, staged,
      deleted, renamed, untracked), exists untracked (ignored), is tracked with a tag other than
      `H` (assume-unchanged `h`, skip-worktree `S` or `s`), or is tracked with a `filter` or `ident`
      value other than `unspecified` or `unset`, or is absent and matched by an ignore rule (`git
      check-ignore -z --stdin`: exit 0 ignored, 1 not, else a failed read); or when any of the four
      commands fails. An absent, unreported, not-ignored path is accepted whatever attributes match. Files: `verdict/autodev_opencode.py`,
      `tests/test_autodev_opencode.py`. Verify:
      `$TESTENV uv run --frozen python -m pytest -q -n 2 tests/test_autodev_opencode.py::TestPreflight`
      with `test_tracked_and_unmodified_owned_file`, `test_modified_owned_file_refused`,
      `test_staged_or_deleted_owned_file_refused`, `test_untracked_or_ignored_owned_file_refused`,
      `test_assume_unchanged_owned_file_refused` and `test_skip_worktree_owned_file_refused` (an
      edit that leaves `git status` empty; the test first asserts `git status --porcelain` is empty,
      so it cannot pass for the wrong reason),
      `test_owned_file_with_a_filter_or_ident_attribute_refused` (parametrised: a clean filter
      that drops a line, and `ident`; a same-size edit; the same empty-status assertion first),
      `test_absent_owned_file_accepted` (once with a `filter` attribute matching its name),
      `test_ignored_absent_owned_file_refused` and `test_ignored_and_plain_absent_owned_files_refused`
      (task 1.2; the mixed case creates nothing),
      `test_unowned_dirty_file_does_not_refuse` and `test_git_read_fails_closed` (parametrised over
      the four reads: a runner whose command exits non-zero, or for `check-ignore` with a status other
      than 0 and 1, must not read as clean). Each refusal
      case asserts the repository is byte-identical and no attempt directory exists.
      Evidence: `TestPreflight` green; removing the status, tag, untracked, attribute, ignored-absent or fail-closed checks, or checking attributes of an absent file, fails its named test.
- [x] 3.4 Isolation matrix against a hostile stub worker. Each `rejected` variant compares the
      user's repository before and after with the spec's byte-identical definition. Files:
      `tests/test_autodev_opencode.py`. Verify: `$TESTENV uv run --frozen python -m pytest -q -n 2 tests/test_autodev_opencode.py::TestMatrix`
      with `test_out_of_bounds_edits_are_rejected` (parametrised: a plain edit, a staged edit, a
      worker `git commit`, an unowned new file, a symlink swapped into an owned path, a worker
      `git push origin` which has no target, a planted `core.fsmonitor` and hook). One more runs
      end to end through `execute_unit` (the executor writes nothing into the attempt tree since
      #1336, so no context file is planted): `test_no_context_file_at_all` (the owned repair is
      `applied`, `changed_files` lists exactly the owned file and nothing else reaches the user's
      repository). One more test records the limit ADR-053 Decision 4 accepts:
      `test_absolute_path_write_is_an_accepted_risk` is `xfail(strict=True)` on "the user's
      repository is unchanged", so closing the limit turns it red until the mark is removed.

      Evidence: `TestMatrix` green (seven hostile variants, `test_no_context_file_at_all`, and the strict xfail); running git in the attempt tree, or walking `.git`, fails the planted-configuration tests.
## 4. `run_autodev` wiring

- [x] 4.1 Protocol, gateway check and the restore rule (D7, D8). Add the `UnitExecutor`
      protocol and the optional `isolated`, `restore` and `preflight` capabilities; type the
      `executor` parameter and `_run_unit_once`; skip only the base-URL equality for an
      `isolated` executor and keep the model equality; in `_run_unit_once` wrap `execute_unit` and
      everything after it so that, for an executor with `restore`, any exit other than a verified
      success calls `restore()` once (a false `_verify`; an exception or `KeyboardInterrupt` from
      `execute_unit`, `_touched_since` or `_verify`), appends the result to
      the reason for a false result, and for an exception re-raises the original after catching
      and printing any failure of `restore()` to standard error. Files:
      `verdict/autodev_run.py`, `tests/test_autodev_opencode_wiring.py`. Verify:
      `$TESTENV uv run --frozen python -m pytest -q -n 2 tests/test_autodev_opencode_wiring.py::TestRestoreWiring`
      and `$TESTENV uv run --frozen python -m pytest -q -n 2 tests/test_autodev_wiring.py`
      (existing `PatchExecutor` behaviour unchanged), with
      `test_verification_fails_after_an_applied_attempt` (byte-identical, a pre-existing dirty
      unowned file untouched), `test_verification_command_writes_to_the_repository`,
      `test_verified_unit_is_not_restored`, `test_earlier_verified_unit_is_not_undone` (unit A
      verified, then unit B rejected, erroring or raising on the same executor, A's files stay),
      `test_user_edit_after_the_transfer_is_kept` (the same scenario through `_run_unit_once`: the
      reason names the file), `test_verification_raises` (a runner raising
      `PermissionError` in `_verify`; the `PermissionError` propagates), `test_attribution_raises`,
      `test_interrupt_after_the_transfer` (`KeyboardInterrupt`), `test_executor_raises_after_its_transfer` (an `Exception` after the transfer: one `restore()`, an
      unverified `executor error:` outcome, not re-raised),
      `test_interrupt_inside_the_executor` (the real `OpenCodeUnitExecutor` over a stub OpenCode
      executor, its `rename` patched to perform the call and raise `KeyboardInterrupt`, which
      propagates out of `_run_unit_once` unchanged after one `restore()`),
      `test_restore_fails_while_an_exception_propagates` (restore raises `RuntimeError`; the
      propagated exception is the original and standard error names the restore failure),
      `test_restore_capability_alone_grants_nothing` (has `restore()`, not `isolated`, still fails
      the gateway equality check and is not subject to the retry refusal), and `test_patch_executor_is_never_restored`.
      Evidence: `TestRestoreWiring` green (against the unwired `autodev_run.py` the wiring file fails 23 of 25); the existing `tests/test_autodev_wiring.py` and the other six autodev test files pass unmodified (222 tests); removing the restore on a false result, on an exception, the verified-unit guard, the swallow of a restore failure, or the isolated skip of the gateway check, or letting a `restore` method imply `isolated`, fails its named test.
- [x] 4.1b Hand the run id to the executor (D8). `run_autodev` calls `executor.begin_run(attempt_run_id)`
      once, before the first unit, when the executor has it. Files: `verdict/autodev_run.py`,
      `tests/test_autodev_opencode_wiring.py`. Verify:
      `$TESTENV uv run --frozen python -m pytest -q -n 2 tests/test_autodev_opencode_wiring.py::TestRunId`
      with `test_run_id_and_unit_id_reach_the_opencode_executor` (the stub OpenCode executor receives the
      receipt's `attempt_run_id` and the unit's id) and `test_patch_executor_gets_no_run_id`.
      Evidence: `TestRunId` green; removing the `begin_run` call fails `test_run_id_and_unit_id_reach_the_opencode_executor`.
- [x] 4.2 Refuse retries and handle null sampling (D8). With an `isolated` executor and non-empty
      `retry_routes` or a `retry_executor_factory`, raise `AutodevError` before the planner call.
      Make `_executor_lineage` and the attempt-record write accept `None` sampling values,
      omitting the keys if the record schema requires numbers. Files: `verdict/autodev_run.py`,
      `tests/test_autodev_opencode_wiring.py`. Verify: `$TESTENV uv run --frozen python -m pytest -q -n 2 tests/test_autodev_opencode_wiring.py::TestRetries`
      with `test_retry_routes_with_the_isolated_executor` (and the factory variant, each before
      any decomposer call), `test_failed_unit_is_not_retried` (`PatchExecutor` patched to raise is
      never constructed) and `test_attempt_record_for_an_isolated_unit_is_written` (temp
      `VERDICT_HOME`).
      Evidence: `TestRetries` green; removing the retry refusal fails `test_retry_routes_with_the_isolated_executor`; recording unsent settings as null values fails the attempt-record test.
- [x] 4.3 The precondition hook (D5). `_run_unit_once` calls `executor.preflight(unit)` first
      when the executor has it, before `_try_mechanical`, and acts on the reason only if the
      mechanical tier declines the unit: the unit fails with the reason, `execute_unit` is not
      called, and the outcome has tier `refused` with `changed_files` from `_touched_since` (as
      for the other model-tier exits, so a mechanical edit is attributed to the unit); `run_autodev`
      writes no attempt record for it (it does so for tier `model`), `unreported_units` omits it,
      and `AutodevReport.summary` and the CLI's model-unit row count model units by tier `model`
      (`git grep -n '"mechanical"'` finds no other consumer that assumes two tiers). The refusal itself writes
      nothing; the mechanical tier's edit stays. Files: `verdict/autodev_run.py`,
      `tests/test_autodev_opencode_wiring.py`. Verify:
      `$TESTENV uv run --frozen python -m pytest -q -n 2 tests/test_autodev_opencode_wiring.py::TestPreflightHook`
      with `test_refusal_starts_no_worker_and_records_no_attempt` (also asserts no attempt record, that
      `unreported_units` omits the unit and that the summary's model count excludes it),
      `test_mechanical_edit_does_not_refuse` (a clean owned file; a fake runner makes `ruff --fix`
      edit it and the check still fail; the worker starts),
      `test_refused_unit_keeps_a_mechanical_edit` (an owned file with an uncommitted change; the
      fake runner's `ruff --fix` edits it and the check still fails; asserts the refusal reason, no
      worker, no attempt tree, the file holds the user's bytes plus the edit, `changed_files` lists
      it and nothing else changed),
      `test_mechanical_tier_fixes_a_modified_owned_file` and `test_default_path_unaffected`
      (`PatchExecutor` has no `preflight` and runs on a modified owned file).

      Evidence: `TestPreflightHook` green; asking preflight after the mechanical tier, dropping the refusal, counting a refused unit as a model unit, dropping the mechanical edit from `changed_files`, counting it in the summary, or letting a raising preflight pass fails its named test.
## 5. CLI

- [x] 5.1 Readiness check and the credential seam (D9, D10). Functions in
      `verdict/autodev_opencode.py`, called by task 5.2 before `prepare_live_autodev` and
      `prepare_live_autodev_selected`, check the opencode binary (OC1's resolver and version
      probe; extract one shared helper if the doctor's probe is not callable) and serve
      (`GET /health` is 200 with `"engine": "verdict"`) and the credentials the OpenCode executor signs
      with (the serve token resolves and the worker signing key is readable; read-only, no value in the
      message), each through an injectable provider. They run for the flag with `--allow-live` and
      without `--dry-run`, above the `execution_path_decision is None` branch. A failure prints one message and exits 1
      through `_report_autodev_failure` with a new label argument (default and existing callers
      unchanged). A version other than the pinned one is named and does not fail. One function
      builds the OpenCode executor for the CLI. Files: `verdict/autodev_opencode.py`,
      `verdict/cli.py` (the label argument only), `tests/test_autodev_opencode_wiring.py`. Verify:
      `$TESTENV uv run --frozen python -m pytest -q -n 2 tests/test_autodev_opencode_wiring.py::TestReadiness`
      with `test_opencode_binary_missing` (missing, or `--version` failing),
            `test_serve_not_answering` (refused, timeout, non-200, a non-Verdict body),
      `test_credentials_missing` (no serve token; no key file),
      `test_readiness_runs_even_when_a_decision_is_supplied`,
      `test_one_message_in_json_mode` and `test_version_differs_from_the_pinned_version`. The
      failure tests assert exit status 1, exactly one error (one JSON object with `--json`) whose
      label names the readiness check, and that `prepare_live_autodev`,
      `prepare_live_autodev_selected`, the decomposer, `run_autodev` and `PatchExecutor`, each
      patched to raise, are never called.
      Evidence: `TestReadiness` green (against the tree without the CLI code the readiness and flag classes fail 19 of 19); the readiness checks run against a loopback fake serve, a fake `opencode` script and a temporary home. Skipping the binary, version-exit, serve-engine or serve-status, token or key check, dropping the version note, or failing on a different version fails its named test.
- [x] 5.2 The flag. Add `--worker-harness` (choices: `opencode`; default none) to the autodev
      parser, pass it through dispatch to `cmd_autodev`, build the isolated executor with task
      5.1's builder, pass `retry_routes=()`, leave the selection report unchanged. Files:
      `verdict/commands/parsers_routing.py`, `verdict/commands/dispatch.py`, `verdict/cli.py`,
      `tests/test_autodev_opencode_wiring.py`. Verify: `$TESTENV uv run --frozen python -m pytest -q -n 2 tests/test_autodev_opencode_wiring.py::TestFlag`
      and `$TESTENV uv run --frozen python -m pytest -q -n 2 tests/test_autodev_wiring.py`
      with `test_flag_absent` (`PatchExecutor`, no readiness check, no clone),
      `test_flag_with_dry_run` (nothing runs), `test_flag_passes_the_isolated_executor_and_no_retry_routes`
      and `test_help_says_retries_and_lineage_are_not_yet_supported`.

      Evidence: `TestFlag` green, and `TestEndToEnd` drives the real `OpenCodeExecutor` with a fake worker under the autodev run id (the signed run id and node id equal the receipt's `attempt_run_id` and the unit id, and the worker's directory holds nothing the executor wrote). Gating readiness on a missing decision or running it under `--dry-run`, passing the retry routes, not building the executor, a hard-coded failure label, a dropped dispatch argument or an unsaid help line fails its named test; the existing autodev tests pass unmodified.
## 6. Docs

- [ ] 6.1 Document the flag in `docs/guides/harnesses.md` (a subsection of "OpenCode as a worker
      harness", where `--executor opencode` is documented; `docs/CLI_REFERENCE.md` lists no autodev
      flags) and add the autodev use of `VERDICT_SERVE_URL` to `docs/CONFIGURATION.md`: opt-in and
      experimental, the one-message fail-closed behaviour (binary, serve, credentials), what is isolated
      (the spec's byte-identical definition), what is not (an absolute-path write, the mechanical tier,
      the verification command, ignored files), that an owned file with uncommitted changes, or one hidden
      from `git status` (assume-unchanged, skip-worktree, a `filter` or `ident` attribute) or ignored, is
      refused while an edit the mechanical tier already made stays, what a crash during the executor's
      writes leaves and how git undoes it (line endings as git writes them), that retries and lineage are
      not yet supported, and where the attempt tree lives. Describe only what runs.
      Files: `docs/guides/harnesses.md`, `docs/CONFIGURATION.md`. Verify:
      `uv run --frozen python scripts/check_doc_links.py`.

## 7. Gates and review

- [ ] 7.1 Run ruff check and format, mypy strict, the lint gates and the reachability ratchet,
      with every new symbol reachable from `cmd_autodev`. Check the diff against
      `.claude/pipeline-lessons.md`. Get the cross-family review of the local commit and fix every
      valid finding. Run the complete test files, not a selection:
      `$TESTENV uv run --frozen python -m pytest -q -n 2 tests/test_autodev_opencode.py tests/test_autodev_opencode_wiring.py tests/test_autodev_wiring.py tests/test_autodev_context.py tests/test_autodev_operational_loop.py tests/test_autodev_operational_routing.py tests/test_autodev_outcome_attempts.py tests/test_autodev_verification.py tests/test_cli_present_autodev.py`
      (every class named above is in them, and the existing autodev tests pass unmodified:
      `git diff --stat origin/main -- tests` lists only the two new test files). Files: none. Verify:
      `uv run --frozen ruff check . && uv run --frozen ruff format --check .`,
      `uv run --frozen mypy verdict --strict`,
      `uv run --frozen python scripts/check_reachability.py --ratchet-base origin/main`,
      `uv run --frozen python scripts/check_lint_gates.py` and `git diff --check` all exit 0.
- [ ] 7.2 Validate this change with both validators and tick the tasks that have evidence.
      Files: `openspec/changes/autodev-opencode-isolated-executor/`. Verify:
      `uv run --frozen python verdict/openspec_vendor/validate_openspec_change.py openspec/changes/autodev-opencode-isolated-executor`
      and `npx -y @fission-ai/openspec@1.13.2 validate autodev-opencode-isolated-executor --strict`.

## 8. After merge

- [ ] (post-merge) 8.1 On installed main, run `VERDICT_OPENCODE_BIN=/nonexistent verdict autodev
      --allow-live --worker-harness opencode --objective "x" --repo <scratch repository>`.
      Verify: it exits 1 with the single readiness message naming the binary, before any probe
      or planner call, and `git -C <scratch repository> status --porcelain` is empty. The live
      unit run on OpenCode through serve is OC2b's check (#1314), not this one.
