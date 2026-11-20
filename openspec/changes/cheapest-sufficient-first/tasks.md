# Tasks

## 1. Implementation

- [x] 1.1 Move `c["slack"]` to position 2 in `_rank_key` (after `capacity_order`)
- [x] 1.2 Update `_rank_key` type annotation and docstring; update `_probe_order` slice from `[:3]` to `[:4]` to keep probe ordering consistent with new rank key
- [x] 1.3 Add `"ordering_basis": "cheapest_sufficient_first"` to `_rank_components`
- [x] 1.4 Move `"slack"` and add `"ordering_basis"` before `"rating_basis"` in `_rank_components_for_display`
- [x] 1.5 Update `_rank_components` and `_rank_components_for_display` docstrings
- [x] 1.6 Update `test_cheaper_sufficient_beats_unnecessary_premium` to expect haiku for low-risk task
- [x] 1.7 Update `test_different_work_units_get_different_models_from_same_inventory` assertions and comment
- [x] 1.8 Add `TestCheapestSufficientFirst` class with three failing-first tests
- [x] 1.9 Update `test_selection_identical_for_representative_fixture` ordered key list
- [x] 1.10 Update `test_rank_key_built_from_rank_components` docstring and tuple
- [x] 1.11 Update `docs/CONFIGURATION.md` advisory-ranks tie-break description
- [x] 1.12 Run `tests/test_orch_eligibility.py`, `tests/test_explain_selection.py`, `tests/test_free_first_order.py` — all pass
- [x] 1.13 `ruff check`, `ruff format --check`, `mypy verdict --strict` — all pass

## 2. Role split (operator decision 2026-11-20)

- [x] 2.1 Add `role_ordering: str = "cheapest_sufficient_first"` field to `TaskRequirements` in `verdict/orchestration/contracts.py`
- [x] 2.2 Make `_rank_key` role-aware: `cheapest_sufficient_first` puts slack at position 2; `strongest_first` puts rating before slack. Use `threading.local` flag set in `_assess_all`.
- [x] 2.3 Make `ordering_basis` in `_rank_components` dynamic (reflects active ordering, not hardcoded).
- [x] 2.4 Add `role_ordering="strongest_first"` to planner `TaskRequirements` in `verdict/orchestration/run.py`
- [x] 2.5 Add `role_ordering="strongest_first"` to reviewer `TaskRequirements` in `verdict/orchestration/review.py`
- [x] 2.6 Add `role_ordering="strongest_first"` to `PLANNER_REQUIREMENTS` in `verdict/role_selection.py`
- [x] 2.6a Add `role_ordering="strongest_first"` to `_live_planner_models` `TaskRequirements` in `verdict/autodev_run.py` to keep it consistent with `PLANNER_REQUIREMENTS`
- [x] 2.7 Add `TestRoleOrdering` class with one test per ordering side to `tests/test_orch_eligibility.py`
- [x] 2.8 Update OpenSpec proposal, spec and tasks to state the role split
- [ ] (post-merge) 2.9 Push through vgate; CI exact-head checks green; squash-merge to main

## 3. Live checks

To verify: run `verdict orchestrate` on a real story from merged main; inspect the selection log and `verdict doctor` output.

- [ ] (post-merge) 3.1 Run `verdict orchestrate` on a real story; confirm tier-1 route is selected for tier-1 required worker nodes instead of always going to tier-0
- [ ] (post-merge) 3.2 Confirm planner/reviewer selection still picks the highest-rated route
- [ ] (post-merge) 3.3 Run `verdict doctor` and confirm it stays green
