# Tasks

## 1. Served model

- [x] 1.1 A provider-prefixed served-model header admits the route: `tests/test_autodev_wiring.py::test_prepare_live_autodev_selected_admits_provider_prefixed_served_model`
- [x] 1.2 Another served model is dropped as `identity`: `tests/test_autodev_wiring.py::test_prepare_live_autodev_selected_drops_other_served_model_as_identity`
- [x] 1.3 A response without a served-model header is dropped as `identity`: `tests/test_autodev_wiring.py::test_prepare_live_autodev_selected_drops_missing_served_model_as_identity`
- [x] 1.4 The pinned probe accepts a provider-prefixed header and refuses another model before planning: `tests/test_autodev_wiring.py::test_prepare_live_autodev_pinned_probe_accepts_both_served_model_forms`

## 2. Ladder depth

- [x] 2.1 Lower-ranked routes are probed after higher-ranked probes fail: `tests/test_autodev_wiring.py::test_prepare_live_autodev_selected_backfills_after_probe_failures`
- [x] 2.2 The walk stops at three admitted routes: `tests/test_autodev_wiring.py::test_prepare_live_autodev_selected_stops_at_top_n_admitted_routes`
- [x] 2.3 A model family is probed once, whether its probe passed or failed: `tests/test_autodev_wiring.py::test_prepare_live_autodev_selected_probes_each_family_once`
- [x] 2.4 The walk stops at eight probes and names every probed route: `tests/test_autodev_wiring.py::test_prepare_live_autodev_selected_stops_at_the_probe_budget`

## 3. Ladder requests

- [x] 3.1 The executor ladder request excludes the `claude` and `codex` pools: `tests/test_autodev_wiring.py::test_prepare_live_autodev_selected_excludes_reserved_pools_in_the_executor_request`
- [x] 3.2 The planner ladder request excludes the `claude` and `codex` pools: `tests/test_autodev_wiring.py::test_live_planner_models_excludes_reserved_pools_in_the_planner_request`

## 4. Spec

- [x] 4.1 `openspec validate autodev-executor-ladder --strict`

- [ ] (post-merge) On installed main, one unpinned `verdict autodev --allow-live --json` run on a scratch repository lists at least two `executor_selection.candidates` from different model families.
- [ ] (post-merge) After that run, no health-cache entry in the `claude` or `codex` pool shares a `checked_at` stamp with an entry either of the run's two selection sweeps wrote.
