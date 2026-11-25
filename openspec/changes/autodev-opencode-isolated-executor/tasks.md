# Tasks

Build plan for OC2a (#1301). The build waits on #1299 (OC1) being merged. Every spec scenario
has one test below, named `test_` plus the scenario title in snake case; a scenario with no
test here is a defect in this plan.

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
  test; no sleeps. Where a test compares "the complete tree", it uses the definition of
  byte-identical in the spec delta and compares directory sets too.
- Only tasks 1.1 and 5.1 import OC1 code. Tasks 2 to 4 depend on the stub contract only.

## 1. Preconditions

- [ ] 1.1 Confirm #1299 is merged to main. Re-read what the design took from it [U]: the
      `OpenCodeExecutor.run(prompt, *, route_id, cwd, timeout_seconds)` signature and that it
      never raises, the `harness`, `env=` and binary-resolver symbols, `PINNED_VERSION`, the
      context file and its byproduct handling, the serve base-URL setting and the credential
      source. Record each difference in design.md under "Drift after #1299" and adjust tasks 3
      and 5 before coding. Re-read #1313's state. Files: design.md. Verify:
      `git log origin/main --oneline -- verdict/orchestration/executor_opencode.py` lists the
      OC1 merge, and `$TESTENV uv run --frozen python -c "from verdict.orchestration.executor_opencode import OpenCodeExecutor, PINNED_VERSION"` exits 0.

## 2. The attempt tree (no OpenCode needed)

- [ ] 2.1 Seed (D2, D3). `seed_attempt_tree(repo, parent_dir, owned_files)`: `--no-local` clone,
      remove `origin`, overlay from `git status --porcelain=v1 -z --untracked-files=all`
      (a status with `R` or `C` consumes the following source record: copy the destination's
      working-tree bytes, remove the source when the user's tree lacks it), take M0, refuse by
      name for no HEAD, a submodule, a nested repository, an unsafe component or a limit.
      Files: `verdict/autodev_opencode.py`, `tests/test_autodev_opencode.py`. Verify:
      `$TESTENV uv run --frozen python -m pytest -q -n 2 tests/test_autodev_opencode.py -k seed`
      with `test_clone_shares_nothing_with_the_repository`, `test_dirty_state_is_carried_over`,
      `test_staged_rename` (`git mv`; the clone has `new.py` and no `old.py`),
      `test_staged_rename_whose_destination_was_then_modified` (`git mv` then edit; the clone
      has the edited bytes and no `old.py`), `test_owned_files_hold_working_tree_bytes` (an
      `eol` attribute), `test_seed_refused_before_any_worker_starts` (one case per refusal, no
      directory left). The rename tests and the dirty-state test also assert the user's
      repository is byte-identical after seeding, and a `-z` record for a path containing a
      space parses as one path.
- [ ] 2.2 Manifest and delta (D4). `attempt_delta(tree, m0, repo, unit)` builds M1 with a
      descriptor-relative walk (root opened `O_DIRECTORY`; `scandir` on each directory
      descriptor; children opened `O_DIRECTORY | O_NOFOLLOW` relative to their parent; files
      `O_NOFOLLOW | O_NONBLOCK` then `fstat`), applies the byproduct rule through
      `git check-ignore --no-index -z --stdin` in the user's repository, then ownership on leaf
      entries, the regular-file rule and the empty-delta rule, and reads each delta file once.
      It never runs git in the tree. Files: `verdict/autodev_opencode.py`,
      `tests/test_autodev_opencode.py`. Verify: `$TESTENV uv run --frozen python -m pytest -q -n 2 tests/test_autodev_opencode.py -k delta`
      with `test_planted_git_configuration_is_inert`, `test_git_state_does_not_change_the_result`
      (edit, `git add`, `git commit`, revert), `test_a_new_owned_file_in_a_new_directory`,
      `test_directory_and_file_swapped`, `test_special_files_in_the_attempt_tree` (a FIFO would
      hang without `O_NONBLOCK`), `test_a_regular_file_replaced_by_a_fifo_during_validation` and
      `test_ancestor_directory_swapped_during_validation` (both through a `hooks` seam between
      the metadata read and the open), `test_a_tracked_file_with_the_byproduct_name`,
      `test_ignored_byproduct` (a new path in a user-ignored directory is dropped),
      `test_a_worker_cannot_hide_a_path_by_editing_gitignore`; plus
      `test_ignored_path_present_at_seed_is_not_a_byproduct`, `test_owned_delete_is_in_the_delta`,
      `test_mode_only_change_is_in_the_delta`, `test_symlink_in_an_owned_path_is_rejected`,
      `test_delta_hash_mismatch_is_error`, `test_file_over_the_limit_is_a_named_refusal` and
      `test_tree_over_the_limit_is_a_named_refusal`.
- [ ] 2.3 Transfer (D6). `transfer(delta, user_repo, m0, hooks=None)`: the read-only phase
      (each path resolved from a root descriptor with `O_DIRECTORY | O_NOFOLLOW`; the user's file
      opened relative to its parent with `O_NOFOLLOW | O_NONBLOCK` and checked by `fstat` before it
      is read; kind, mode and SHA-256, or absence, compared with M0; limits), then the write phase (`mkdir` by descriptor, journalled; comparison and
      device and inode check repeated before each write; one `replace_file(parent_fd, name,
      data, mode)` helper that creates a unique `.verdict-transfer-*` file with
      `O_CREAT | O_EXCL | O_NOFOLLOW`, writes, sets the mode and renames, and unlinks the
      temporary file by the same descriptor on any failure before the rename succeeds, naming
      an unlink failure without replacing the original error), rolling back through restore
      on any failure after the first write. It refuses where `os.supports_dir_fd` lacks `open`,
      `stat`, `mkdir`, `rename` or `unlink`. Files: `verdict/autodev_opencode.py`,
      `tests/test_autodev_opencode.py`. Verify: `$TESTENV uv run --frozen python -m pytest -q -n 2 tests/test_autodev_opencode.py -k "transfer or kill"`
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
      **Crash tests.** `test_kill_after_the_temporary_write`,
      `test_kill_after_the_first_of_two_renames` and `test_kill_after_the_last_rename` run the
      transfer in a child process (`subprocess` with `sys.executable`; a helper beside the test
      file) over a repository whose owned files are tracked and unmodified. A `hooks` entry
      prints a ready line and blocks at the named phase; the test reads it, sends `SIGKILL`,
      then asserts: each owned path equals its prior or its new bytes (never torn), at most one
      `.verdict-transfer-*` file exists, `git status --porcelain` lists nothing but owned paths
      and that file, and `git checkout -- <owned paths>` plus removing the created files and
      empty directories leaves the repository byte-identical to before the transfer.
- [ ] 2.4 Restore (D7). `restore(journal)` undoes the current attempt's transfer in reverse with task 2.3's
      traversal, re-check and `replace_file` helper, checking kind, executable bit and SHA-256
      against what the transfer wrote; it leaves alone and reports any path that differs, removes
      created directories when empty, and is idempotent. Files: `verdict/autodev_opencode.py`,
      `tests/test_autodev_opencode.py`. Verify:
      `$TESTENV uv run --frozen python -m pytest -q -n 2 tests/test_autodev_opencode.py -k restore`
      with `test_created_files_and_directories_are_removed`,
      `test_user_edit_after_the_transfer_is_kept`,
      `test_user_mode_change_after_the_transfer_is_kept`,
      `test_parent_swapped_between_transfer_and_restore`, `test_restore_twice`,
      `test_restore_without_a_transfer`, `test_journal_is_emptied_at_the_start_of_each_attempt`, `test_restore_write_failure_leaves_no_temporary_file`,
      and `test_restore_of_a_modified_file_and_of_a_deleted_file`.

## 3. The executor

- [ ] 3.1 Prompt (D10). `build_opencode_prompt(unit)` names the objective, owned files,
      verification command and context pack, and the rules (edit only owned files, run the
      command, no commit or push). Files: `verdict/autodev_opencode.py`,
      `tests/test_autodev_opencode.py`. Verify: `$TESTENV uv run --frozen python -m pytest -q -n 2 tests/test_autodev_opencode.py -k prompt`
      with `test_prompt_carries_the_context_pack_unchanged` and
      `test_prompt_never_contains_the_credential` (a credential value placed in the environment).
- [ ] 3.2 `OpenCodeUnitExecutor.execute_unit` and the `PatchAttempt` mapping (D8). Seed, run the
      injected OpenCode executor with `route_id=config.model` and `cwd=<tree>`, take the delta,
      read it once, remove the tree, transfer (the last fallible step), and map to `applied`,
      `rejected` or `error` with the real reason; the tree is also removed in a `finally`.
      `config.model` is the route, sampling fields `None`, `response_headers` `{}`,
      `resolved_model` `None`, `isolated` true, `restore()` delegates to task 2.4. Files:
      `verdict/autodev_opencode.py`, `tests/test_autodev_opencode.py`. Verify:
      `$TESTENV uv run --frozen python -m pytest -q -n 2 tests/test_autodev_opencode.py -k execute`
      with `test_a_valid_owned_repair_is_applied` (usage `reported=False` when tokens are null),
      `test_worker_failed` (timeout, stall, serve refusal text, non-zero exit: nothing
      transferred although the tree holds edits), `test_worker_makes_no_change`,
      `test_attempt_tree_removed_on_every_exit_path` (applied, rejected, error, exception,
      cancellation), `test_tree_is_removed_before_the_transfer`, `test_running_event_loop_returns_error`,
      `test_no_served_identity_is_claimed` and `test_credential_never_appears_in_a_record` (a stub
      terminal whose error text is already redacted while the credential sits in the
      environment; reason, outcome, attempt record and receipt payload contain the redacted text
      and never the credential).
- [ ] 3.3 Owned-file precondition (D5). `OpenCodeUnitExecutor.preflight(unit)` reads
      `git status --porcelain=v1 -z --untracked-files=all` and `git ls-files -z -- <owned>` in
      the user's repository and returns a reason naming the path and the remedy when an owned path
      is listed by status (modified, staged, deleted, renamed, untracked) or exists untracked
      (ignored), or when either command fails.
      Files: `verdict/autodev_opencode.py`, `tests/test_autodev_opencode.py`. Verify:
      `$TESTENV uv run --frozen python -m pytest -q -n 2 tests/test_autodev_opencode.py -k preflight`
      with `test_tracked_and_unmodified_owned_file`, `test_modified_owned_file_refused`,
      `test_staged_or_deleted_owned_file_refused`, `test_untracked_or_ignored_owned_file_refused`,
      `test_absent_owned_file_accepted`, `test_unowned_dirty_file_does_not_refuse` and
      `test_git_read_fails_closed` (a runner whose `git status` exits non-zero must not read as
      clean). Each refusal case asserts the repository is byte-identical and no attempt
      directory exists.
- [ ] 3.4 Isolation matrix against a hostile stub worker. Each `rejected` variant compares the
      user's repository before and after with the spec's byte-identical definition. Files:
      `tests/test_autodev_opencode.py`. Verify: `$TESTENV uv run --frozen python -m pytest -q -n 2 tests/test_autodev_opencode.py -k matrix`
      with `test_out_of_bounds_edits_are_rejected` (parametrised: a plain edit, a staged edit, a
      worker `git commit`, an unowned new file, a symlink swapped into an owned path, a worker
      `git push origin` which has no target, a planted `core.fsmonitor` and hook). Two more run
      end to end through `execute_unit` with a stub that also writes `.verdict-context.md` into
      `cwd`: `test_context_file_created_by_the_executor` (the owned repair is `applied` and the
      file is absent from the user's repository) and `test_no_context_file_at_all` (the same
      repair with no such file). One more test records the limit ADR-053 Decision 4 accepts:
      `test_absolute_path_write_is_an_accepted_risk` is `xfail(strict=True)` on "the user's
      repository is unchanged", so closing the limit turns it red until the mark is removed.

## 4. `run_autodev` wiring

- [ ] 4.1 Protocol, gateway check and the restore rule (D7, D8). Add the `UnitExecutor`
      protocol and the optional `isolated`, `restore` and `preflight` capabilities; type the
      `executor` parameter and `_run_unit_once`; skip only the base-URL equality for an
      `isolated` executor and keep the model equality; in `_run_unit_once` wrap everything after
      `execute_unit` is called so that, for an executor with `restore`, any exit other than a
      verified success calls `restore()` once (a false `_verify`; an exception from
      `execute_unit`, `_touched_since` or `_verify`; `KeyboardInterrupt`), appends the result to
      the reason for a false result, and for an exception re-raises the original after catching
      and printing any failure of `restore()` to standard error. Files:
      `verdict/autodev_run.py`, `tests/test_autodev_opencode_wiring.py`. Verify:
      `$TESTENV uv run --frozen python -m pytest -q -n 2 tests/test_autodev_opencode_wiring.py -k "restore or gateway"`
      and `$TESTENV uv run --frozen python -m pytest -q -n 2 tests/test_autodev_wiring.py`
      (existing `PatchExecutor` behaviour unchanged), with
      `test_verification_fails_after_an_applied_attempt` (byte-identical, a pre-existing dirty
      unowned file untouched), `test_verification_command_writes_to_the_repository`,
      `test_verified_unit_is_not_restored`, `test_earlier_verified_unit_is_not_undone` (unit A
      verified, then unit B rejected, erroring or raising on the same executor, A's files stay),
      `test_user_edit_after_the_transfer_is_kept` (the same scenario through `_run_unit_once`: the
      reason names the file), `test_verification_raises` (a runner raising
      `PermissionError` in `_verify`; the `PermissionError` propagates), `test_attribution_raises`,
      `test_interrupt_after_the_transfer` (`KeyboardInterrupt`), `test_executor_raises_after_its_transfer`,
      `test_restore_fails_while_an_exception_propagates` (restore raises `RuntimeError`; the
      propagated exception is the original and standard error names the restore failure),
      `test_restore_capability_alone_grants_nothing` (has `restore()`, not `isolated`, still fails
      the gateway equality check and is not subject to the retry refusal), and `test_patch_executor_is_never_restored`.
- [ ] 4.2 Refuse retries and handle null sampling (D8). With an `isolated` executor and non-empty
      `retry_routes` or a `retry_executor_factory`, raise `AutodevError` before the planner call.
      Make `_executor_lineage` and the attempt-record write accept `None` sampling values,
      omitting the keys if the record schema requires numbers. Files: `verdict/autodev_run.py`,
      `tests/test_autodev_opencode_wiring.py`. Verify: `$TESTENV uv run --frozen python -m pytest -q -n 2 tests/test_autodev_opencode_wiring.py -k "retry or lineage"`
      with `test_retry_routes_with_the_isolated_executor` (and the factory variant, each before
      any decomposer call), `test_failed_unit_is_not_retried` (`PatchExecutor` patched to raise is
      never constructed) and `test_attempt_record_for_an_isolated_unit_is_written` (temp
      `VERDICT_HOME`).
- [ ] 4.3 The precondition hook (D5). `_run_unit_once` calls `executor.preflight(unit)` first
      when the executor has it, before `_try_mechanical`, and acts on the reason only if the
      mechanical tier declines the unit: the unit fails with the reason, `execute_unit` is not
      called, and the outcome has tier `refused`: `run_autodev` writes no attempt record for it
      (it does so for tier `model`), `unreported_units` omits it, and `AutodevReport.summary`
      counts model units by tier `model`. Files: `verdict/autodev_run.py`, `tests/test_autodev_opencode_wiring.py`. Verify:
      `$TESTENV uv run --frozen python -m pytest -q -n 2 tests/test_autodev_opencode_wiring.py -k preflight`
      with `test_refusal_starts_no_worker_and_writes_nothing` (also asserts no attempt record, that
      `unreported_units` omits the unit and that the summary's model count excludes it), `test_mechanical_edit_does_not_refuse`
      (a fake runner makes `ruff --fix` edit the owned file and the check still fail),
      `test_mechanical_tier_fixes_a_modified_owned_file` and `test_default_path_unaffected`
      (`PatchExecutor` has no `preflight` and runs on a modified owned file).

## 5. CLI

- [ ] 5.1 Readiness check and the credential seam (D9, D10). Functions in
      `verdict/autodev_opencode.py`, called by task 5.2 before `prepare_live_autodev` and
      `prepare_live_autodev_selected`, check the opencode binary (OC1's resolver and version
      probe; extract one shared helper if the doctor's probe is not callable) and serve
      (`GET /health` is 200 with `"engine": "verdict"`). A failure prints one message and exits 1
      through `_report_autodev_failure` with a new label argument (default and existing callers
      unchanged). A version other than the pinned one is named and does not fail. One function
      builds the OpenCode executor for the CLI. Files: `verdict/autodev_opencode.py`,
      `verdict/cli.py` (the label argument only), `tests/test_autodev_opencode_wiring.py`. Verify:
      `$TESTENV uv run --frozen python -m pytest -q -n 2 tests/test_autodev_opencode_wiring.py -k readiness`
      with `test_opencode_binary_missing` (missing, or `--version` failing),
      `test_serve_not_answering` (refused, timeout, non-200, a non-Verdict body),
      `test_one_message_in_json_mode` and `test_version_differs_from_the_pinned_version`. The
      failure tests assert exit status 1, exactly one error (one JSON object with `--json`) whose
      label names the readiness check, and that `prepare_live_autodev`,
      `prepare_live_autodev_selected`, the decomposer, `run_autodev` and `PatchExecutor`, each
      patched to raise, are never called.
- [ ] 5.2 The flag. Add `--worker-harness` (choices: `opencode`; default none) to the autodev
      parser, pass it through dispatch to `cmd_autodev`, build the isolated executor with task
      5.1's builder, pass `retry_routes=()`, leave the selection report unchanged. Files:
      `verdict/commands/parsers_routing.py`, `verdict/commands/dispatch.py`, `verdict/cli.py`,
      `tests/test_autodev_opencode_wiring.py`. Verify: `$TESTENV uv run --frozen python -m pytest -q -n 2 tests/test_autodev_opencode_wiring.py -k cli`
      and `$TESTENV uv run --frozen python -m pytest -q -n 2 tests/test_autodev_wiring.py`
      with `test_flag_absent` (`PatchExecutor`, no readiness check, no clone),
      `test_flag_with_dry_run` (nothing runs), `test_flag_passes_the_isolated_executor_and_no_retry_routes`
      and `test_help_says_retries_and_lineage_are_not_yet_supported`.

## 6. Docs

- [ ] 6.1 Document the flag in `docs/CLI_REFERENCE.md`: opt-in and experimental, the one-message
      fail-closed behaviour, what is isolated (the spec's byte-identical definition), what is not
      (an absolute-path write, the mechanical tier, ignored files), that an owned file with
      uncommitted changes is refused, what a crash leaves and how git undoes it, that retries and
      lineage are not yet supported, and where the attempt tree lives. Describe only what runs.
      Files: `docs/CLI_REFERENCE.md`. Verify: `uv run --frozen python scripts/check_doc_links.py`.

## 7. Gates and review

- [ ] 7.1 Run ruff check and format, mypy strict, the lint gates and the reachability ratchet,
      with every new symbol reachable from `cmd_autodev`. Check the diff against
      `.claude/pipeline-lessons.md`. Get the cross-family review of the local commit and fix every
      valid finding. Files: none. Verify:
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
