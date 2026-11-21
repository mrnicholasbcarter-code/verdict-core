# Tasks

## 1. Registry carry-forward and rotation

- [x] 1.1 Add `_CARRY_SKIP_REASONS` constant and `_UTC` timezone alias to
      `verdict/discovery/registry.py`
- [x] 1.2 Add `carried: int = 0` field to `DiscoveryRegistry` with `to_dict`
      and `from_dict` support
- [x] 1.3 Add `_load_registry_raw()` function that loads the registry without
      the file-level TTL gate (for carry-forward)
- [x] 1.4 Add `_checked_at_dt()` helper that parses `entry.checked_at` to a
      tz-aware datetime for sort comparisons (never-checked → datetime.min)
- [x] 1.5 Add `previous: DiscoveryRegistry | None = None` parameter to
      `run_refresh()`; build `previous_by_key` lookup dict
- [x] 1.6 Implement rotation: sort routes by `_checked_at_dt(previous_by_key[r.key])`
      when `previous_by_key` is non-empty; never-checked routes (key absent in
      previous) sort to front
- [x] 1.7 Implement carry-forward in the skipped-entries loop: when
      `skip_reason in _CARRY_SKIP_REASONS` and the route has an entry in
      `previous_by_key`, use the previous entry verbatim; increment
      `carried_count`
- [x] 1.8 Pass `carried=carried_count` to `DiscoveryRegistry` constructor

## 2. Refresh action changes

- [x] 2.1 Import `_load_registry_raw` in `verdict/discovery/refresh.py`
- [x] 2.2 Load previous registry via `_load_registry_raw(env)` before calling
      `run_refresh` (skip when `dry_run=True`)
- [x] 2.3 Pass `previous=previous` to `run_refresh()`
- [x] 2.4 Add `"carried": registry.carried` to the `counts` dict in
      `_registry_to_summary()`
- [x] 2.5 Add `carried` to the static `counts` dict in the `catalog_unavailable`
      path
- [x] 2.6 Show `carried N` in `render_refresh_plain` when `carried > 0`

## 3. Tests

- [x] 3.1 Add `CarryForwardRotationTest` class to
      `tests/test_discovery_registry.py` with four tests:
      (a) unprobed entry keeps `checked_at` and `carried == 1`,
      (b) route reclassified away from FREE is dropped,
      (c) probe order: never-checked first, then oldest,
      (d) two budget-limited runs together cover more routes than one

## 4. Quality gates

- [x] 4.1 All offline tests pass; `uv run ruff check . && uv run ruff format
      --check .` clean; `uv run mypy verdict --strict` clean
- [x] 4.2 Reachability clean: `check_reachability.py --ratchet-base origin/main`
      reports 0 NEW orphan symbols or forbidden imports
- [x] 4.3 All lint gates pass: `scripts/check_lint_gates.py`
- [ ] (post-merge) After next scheduled `--free-only` refresh, verify
      `verdict discovery refresh` output shows `carried N` with N > 0 on a
      budget-limited run
- [ ] (post-merge) After two successive `--free-only` refreshes, verify that
      the second run probes routes that were `wall_budget_exhausted` in the
      first run (rotation working)
