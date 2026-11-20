# Tasks — ONE capacity classifier (S1)

- [x] Add `capacity_of()` to `verdict/capacity.py` as single public entry point
- [x] Wire `capacity_of` as production caller in `verdict/intelligence.py`
- [x] Fix `gateway_free` double-prefix bug in `verdict/intelligence.py`
- [x] Remove `:free` name check from `free_status()` in `verdict/free_route_harvest.py`
- [x] Remove `:free` name check from `pick_best_live()` in `verdict/free_route_harvest.py`
- [x] Remove name-based free rank from `omniroute_catalog.rank()`
- [x] Remove `:free` suffix check from `verdict/autodev_run.py`
- [x] Add `FreeTierModel.route_id` property (normalised, no double-prefix)
- [x] Remove dead `_is_positively_free_identity` and `_looks_free_by_name` from `verdict/free_tier_admit.py`
- [x] Update `_choose_sort` and `admit_free_tier_active` to use `row.route_id`
- [x] Add forbidden-symbol entries in `reachability-forbidden.json`
- [x] Write failing-first tests in `tests/test_one_capacity_classifier.py`
- [x] Update `tests/test_free_route_harvest.py` for name-removed behaviour
- [ ] (post-merge) Confirm `verdict doctor` is green on operator's real setup
