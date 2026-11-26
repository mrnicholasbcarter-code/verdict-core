# Tasks

## 1. Integrate and review nodes are merge-only

- [x] 1.1 `WorkNode` refuses `owned_files` on an `integrate` or `review` node with an error that says
      to move the work to an implement node (`verdict/orchestration/contracts.py`); `WorkGraph.from_dict`
      is strict by default and a reader of a recorded run opts out with
      `allow_merge_only_owned_files=True`. Tests in `tests/test_orch_planner.py`:
      `test_merge_only_node_with_owned_files_is_rejected_with_an_actionable_reason`,
      `test_merge_only_node_without_owned_files_is_accepted`,
      `test_a_persisted_graph_with_an_integrate_node_that_owns_files_still_loads`; the retained proof
      run `docs/proof/dogfood-bod-225-live-2026-09-29` (an integrate node that owns files) keeps
      rebuilding its receipt in
      `tests/test_trace_evidence_events.py::test_existing_proof_run_receipt_still_valid`.
- [x] 1.2 The planning prompt states the rule (`verdict/orchestration/planner.py`). Test:
      `test_planning_prompt_states_that_integrate_nodes_are_merge_only`.
- [x] 1.3 The planner's repair call receives the error text, a repair that keeps the rule broken fails
      planning closed before any worker runs, and a file-less integrate node is accepted as before.
      Tests: `test_parse_plan_rejects_an_integrate_node_that_owns_files`,
      `test_parse_plan_accepts_a_file_less_integrate_node`,
      `test_frontier_planner_repair_call_receives_the_merge_only_reason`,
      `test_frontier_planner_fails_closed_when_the_repair_keeps_the_rule_broken`.

- [x] 1.4 A fresh graph is held to the rule and a recorded one still loads: `verdict orchestrate --graph`
      uses the strict loader (`verdict/orchestration/cli.py`) and the readers of a recorded run (run
      resume in `run.py`, `controls.py`, `receipt.py`) pass the option. Tests in
      `tests/test_orch_graph_loader.py`: `test_a_fresh_graph_is_strict_by_default`,
      `test_a_recorded_graph_loads_unchanged_with_the_explicit_option`,
      `test_orchestrate_refuses_a_fresh_graph_whose_integrate_node_owns_files` (exit 2, no run directory),
      `test_run_controls_read_a_recorded_graph_that_has_an_integrate_node_with_files`,
      `test_resume_reads_a_recorded_graph_that_has_an_integrate_node_with_files`; the receipt reader by
      `tests/test_trace_evidence_events.py::test_existing_proof_run_receipt_still_valid`.

## 2. A verification command that cannot collect is an environment error

- [x] 2.1 `classify_verify_env_error` takes the node's `owned_files` and classifies pytest exit 2 on
      collection errors in unowned files (`verdict/orchestration/verification.py`). Tests in
      `tests/test_orch_verify_command.py`:
      `test_collection_errors_in_unowned_files_are_an_environment_error` (the exact tail of run
      `20261126T020427Z`), `test_the_exact_run_tail_is_unchanged_without_the_node_owned_files`,
      `test_collection_errors_are_an_environment_error_for_a_node_that_owns_nothing`,
      `test_a_collection_error_in_an_owned_file_stays_a_verification_failure`,
      `test_a_collection_error_header_in_an_owned_file_stays_a_verification_failure`,
      `test_errors_with_a_reason_suffix_are_parsed_as_paths`,
      `test_other_pytest_outcomes_stay_verification_failures` (exit 4, exit 1, exit 5, no marker, no
      readable paths), `test_collection_output_from_a_command_that_is_not_pytest_is_not_reclassified`,
      `test_the_collection_rule_covers_the_usual_pytest_invocations`,
      `test_existing_environment_rules_are_unchanged_when_owned_files_are_given`,
      `test_the_reason_names_at_most_three_paths_and_counts_the_rest`,
      `test_an_erroring_path_that_cannot_be_read_keeps_the_failure_a_verification_failure`,
      `test_a_cut_error_list_is_not_trusted_when_the_runner_capped_the_output`,
      `test_a_full_error_list_in_a_capped_output_still_decides`,
      `test_a_non_repo_relative_erroring_path_is_inconclusive_not_unowned` (absolute, `../` and `a/../b`
      paths, summary and header forms),
      `test_one_non_repo_relative_path_makes_the_whole_error_list_inconclusive`,
      `test_a_dot_slash_path_is_still_matched_to_the_owned_file`.
- [x] 2.2 The runtime passes the node's `owned_files` (`verdict/orchestration/runtime.py`). Tests in
      `tests/test_orch_runtime.py`: `test_collection_errors_in_unowned_files_block_after_one_attempt`
      (BLOCKED, one attempt, no reassignment, no cooldown, no escalation count, no ledger or health
      entry) and `test_a_collection_error_in_an_owned_file_is_still_corrected_and_rerouted`.

## 3. Checks

- [x] 3.1 `uv run --frozen python -m pytest -q -n 2 tests/test_orch_planner.py
      tests/test_orch_graph_loader.py tests/test_orch_verify_command.py tests/test_orch_runtime.py
      tests/test_orch_recovery.py tests/test_admission.py` passes; `tests/test_admission.py` is
      unchanged. The one existing test that read a recorded graph with an integrate node that owns files
      (`test_a_persisted_graph_with_an_integrate_node_that_owns_files_still_loads`) and the
      `df263` graph test in `tests/test_orch_dogfood3_fixes.py` now pass the explicit option.
- [x] 3.2 `ruff check`, `ruff format --check`, `mypy verdict --strict`, `openspec validate --strict`
      and `scripts/check_lint_gates.py` pass.

- [x] 3.3 Test-first, and the exception to it. Every new test fails on `main` (`9bca4982`) except six
      compatibility controls, which pass there by design because they pin behaviour the change must
      not alter. Each was shown able to fail by mutating the fix:
      `test_merge_only_node_without_owned_files_is_accepted[integrate|review]` and
      `test_parse_plan_accepts_a_file_less_integrate_node` fail when the rule rejects every
      integrate or review node, with or without files;
      `test_a_collection_error_in_an_owned_file_is_still_corrected_and_rerouted` fails when the
      classifier skips the ownership check;
      `test_run_controls_read_a_recorded_graph_that_has_an_integrate_node_with_files` and
      `test_resume_reads_a_recorded_graph_that_has_an_integrate_node_with_files` fail when the
      reader drops `allow_merge_only_owned_files=True`. The three-argument call in
      `test_the_exact_run_tail_is_unchanged_without_the_node_owned_files` is compatibility coverage
      too, but that test also passes `owned_files=None` explicitly, which `main` rejects with a
      `TypeError`, so the test as a whole fails on `main`.

## 4. Live check

- [ ] (post-merge) 4.1 From installed merged main, a self-build run of the remaining work of
      [#1269](https://github.com/mrnicholasbcarter-code/verdict-core/issues/1269) (`verdict
      orchestrate --openspec-change verdict-deliver`) does not give an integrate node authoring work
      (a plan that does is repaired, or fails before any worker runs), and a node whose verification
      command stops on collection errors in files it does not own ends BLOCKED once as
      `environment_error`, with no reassignment, cooldown or escalation count. Record the run directory
      and the raw `verify` and `failure` events.
