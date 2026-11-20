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
- [ ] (post-merge) 1.14 Push through vgate; CI exact-head checks green; squash-merge to main

## 2. Live checks

- [ ] (post-merge) 2.1 Run `verdict orchestrate` on a real story; confirm tier-1 route is selected for tier-1 required nodes instead of always going to tier-0
- [ ] (post-merge) 2.2 Run `verdict doctor` and confirm it stays green
