# Tasks: ranking admitted routes by verified success

## 1. Score

- [x] 1.1 `rank_success` scores a route for a node kind from verified real-work rows with `score_task_history`, falls back from `route+kind` to `route`, and is neutral below the minimum sample; aliases of one model share a record. Verify: `tests/test_orch_rank_by_success.py` (fallback, neutral, alias and window tests).
- [x] 1.2 Only controller-verified rows of kind `real_task` count. Verify: `tests/test_orch_rank_by_success.py::test_only_verified_real_work_counts`.

## 2. Ranking

- [x] 2.1 `TaskRequirements.for_node` carries the node kind, and the ladder puts the success rank second in `_rank_key` (after capacity class, before slack), without changing planner or reviewer selections that carry no kind. Verify: `tests/test_orch_rank_by_success.py` ordering tests, run through `for_node`; `tests/test_explain_selection.py::test_rank_key_built_from_rank_components`.
- [x] 2.2 The success rank never changes membership or a rejection reason (with the same ledger and health evidence, on and off) and never crosses a capacity class. Verify: `tests/test_orch_rank_by_success.py::test_the_score_never_changes_membership_or_a_rejection_reason` and `::test_many_verified_passes_rank_above_a_cold_route_never_above_a_cheaper_class`.
- [x] 2.3 The success rank adds one ledger read per selection, not one per route; an unreadable ledger fails open once. Verify: `tests/test_orch_rank_by_success.py::test_one_read_per_selection_not_one_per_route` and `::test_an_unreadable_ledger_fails_open_with_one_read`.

## 3. Receipt and docs

- [x] 3.1 Each candidate's `rank_components.success` records score, n, key, node kind, source, window and minimum sample. Verify: `tests/test_orch_rank_by_success.py::test_the_receipt_records_the_settings_and_the_source`; `tests/test_explain_selection.py` key list.
- [x] 3.2 `docs/CONFIGURATION.md` describes the rank and its settings. Verify: `scripts/check_doc_links.py`.

## 4. Live check

- [ ] (post-merge) 4.1 On installed main, the receipt of a real `verdict orchestrate` run shows `rank_components.success` with a score, n and key on its selection events. Verify: the run's `events.jsonl`.
