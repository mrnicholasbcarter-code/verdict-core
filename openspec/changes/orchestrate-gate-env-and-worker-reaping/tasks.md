# Tasks

## 1. Repo gates run in the project's environment

Tests are in `tests/test_orch_verify_command.py`.

- [x] 1.1 A uv project that declares extras gets `uv run --frozen --all-extras mypy`: `test_the_mypy_gate_runs_with_the_projects_extras`
- [x] 1.2 The ruff gates resolve as before: `test_the_ruff_gates_resolve_as_before_when_the_project_has_extras`
- [x] 1.3 No extras, declared conflicts and an unreadable `pyproject.toml` keep the command and never raise: `test_the_mypy_command_is_unchanged_when_there_are_no_usable_extras`, `test_a_worktree_without_a_pyproject_keeps_the_uv_command`
- [x] 1.4 Worktree environments: `test_a_worktree_venv_still_wins_when_the_project_has_extras`, `test_a_venv_not_made_by_uv_is_used_as_it_is`, `test_a_venv_uv_made_without_the_extras_is_completed_not_used_as_it_is`, `test_a_uv_made_venv_is_used_when_uv_is_not_on_path`, `test_a_uv_made_venv_is_used_by_ruff_and_by_projects_without_extras`
- [x] 1.5 With the real uv, a typed module whose stubs only an extra installs passes through the resolved gate and fails through today's command: `test_the_resolved_mypy_gate_passes_in_a_real_uv_project_whose_stubs_are_in_an_extra`
- [x] 1.6 The node, base and integration worktrees all run the gate with the extras, and the node gate that fails like its base is left to integration: `test_node_base_and_integration_gates_all_run_with_the_projects_extras`, `test_the_same_fault_at_node_level_is_still_left_to_integration`

## 2. An environment fault at integration is an environment error

Tests are in `tests/test_orch_verify_command.py`.

- [x] 2.1 The unresolved third-party imports are read from the mypy output (packages, mypy's error total, whether the kept output is whole); first-party ones, other gates, passing gates and other mypy errors give none: `test_unresolved_third_party_imports_are_read_from_the_mypy_output`, `test_a_short_output_that_lists_every_error_is_complete`, `test_a_missing_first_party_module_is_never_an_environment_error`, `test_a_src_layout_first_party_module_is_never_an_environment_error`, `test_a_module_in_a_configured_mypy_path_is_first_party`, `test_a_module_in_the_mypypath_environment_variable_is_first_party`, `test_ordinary_type_errors_have_no_unresolved_imports`, `test_only_the_mypy_gate_is_read_for_unresolved_imports`, `test_a_passing_gate_has_no_unresolved_imports`
- [x] 2.2 At integration the failure is `environment_error` / `BLOCK`, not judged, only with the same unresolved packages on the run's untouched base (one run, worktree and branch removed), and a real failure in another gate wins: `test_an_environment_fault_at_integration_is_an_environment_error`, `test_a_real_gate_failure_at_integration_stays_a_verification_failure`, `test_a_real_failure_in_another_gate_wins_over_an_environment_fault`
- [x] 2.3 An import only the candidate cannot resolve is not the environment (review finding 1 of #1364): the base evidence rule, a typo, a typo beside the base's fault, a cut output with more errors than the base, and a base that cannot be checked all stay `verification_failed` with the reason recorded: `test_the_same_unresolved_imports_on_the_untouched_base_are_the_environment`, `test_an_import_that_the_base_resolves_is_not_the_environment`, `test_a_base_without_the_fault_is_no_evidence`, `test_a_cut_output_needs_no_more_errors_than_the_base`, `test_the_environment_reason_names_at_most_three_packages`, `test_a_missing_import_the_implementation_introduced_is_a_verification_failure`, `test_an_environment_fault_without_evidence_from_the_base_is_a_verification_failure`
- [x] 2.4 A gate that cannot start is an environment error at integration, and a real failure in another gate wins in either order (review finding 2 of #1364): `test_a_gate_executable_that_cannot_be_found_is_an_environment_error_at_integration`, `test_uv_missing_from_the_path_is_an_environment_error_at_integration`, `test_a_gate_command_that_could_not_start_is_an_environment_error_at_integration`, `test_a_real_failure_wins_over_a_gate_that_cannot_run_in_either_order`

## 3. Worker processes end with their attempt

Tests are in `tests/test_orch_worker_reaping.py` (real subprocesses, bounded waits, PIDs the fake workers recorded).

- [x] 3.1 Success: a child in a session of its own, and an orphan of a tool command, end with the attempt, and the attempt directory is gone: `test_success_leaves_no_process_of_a_session_of_its_own`, `test_success_leaves_no_orphan_of_a_tool_command`
- [x] 3.2 Failure, timeout, a leader that keeps running after its final event, and cancellation: `test_failure_leaves_no_process_behind`, `test_timeout_leaves_no_process_behind`, `test_a_leader_that_keeps_running_after_its_final_event_is_stopped`, `test_cancellation_leaves_no_process_behind`
- [x] 3.3 A second cancellation during cleanup, in both phases: `test_cancelling_again_while_the_attempt_is_ended_still_ends_it`
- [x] 3.4 Only the attempt's own processes end, and a signal checks the marker on the process it holds: `test_only_the_attempts_own_processes_are_ended`, `test_two_attempts_end_only_their_own_processes`, `test_a_process_that_does_not_carry_the_marker_is_never_signalled`
- [x] 3.5 An orchestrator process sent SIGTERM or SIGINT while a worker runs leaves no worker behind and exits 143 or 130: `test_an_orchestrator_that_is_signalled_leaves_no_worker_behind`
- [x] 3.6 Handlers are restored, none is installed off the main thread, and a signal the embedding loop handles is left to it: `test_the_signal_handlers_are_the_runs_while_it_lasts_and_the_callers_after`, `test_a_run_in_a_worker_thread_installs_no_handler_and_still_runs`, `test_a_signal_the_embedding_loop_already_handles_is_left_to_it`

## 4. Spec

Test and verify: `uv run python verdict/openspec_vendor/validate_openspec_change.py openspec/changes/orchestrate-gate-env-and-worker-reaping`.

- [x] 4.1 The change validates strictly (vendored validator and `check_lint_gates.py`).
- [ ] (post-merge) 4.2 On installed main, a self-build run of the remaining tasks of #1269 (`verdict orchestrate`, no `--executor` flag) passes its integration repo gates and reaches review, and afterwards no process of its attempts is running (check the PIDs the run's workers had, and `pgrep -f "opencode run --standalone"` is empty).
