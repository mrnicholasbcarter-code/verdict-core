# Tasks

- [x] 1. Add `verdict/orchestration/harness.py` with `ExecutionTarget`,
  `HarnessCapability`, `CapabilityLevel`, `HarnessCapabilities`,
  `HarnessAdapter`, `HarnessRegistry`, `DEFAULT_REGISTRY`, and the
  `PrimeHeadlessAdapter` / `DirectGatewayAdapter` production adapters.
- [x] 2. Wire `DEFAULT_REGISTRY` into `cli.py` (`--executor` choices,
  `_build_single_executor`, `_parse_executor_map`,
  `_selector_visibility_executor`), `eligibility_report.py`
  (`build_selector` visibility delegation), `run.py` (receipt labels via
  `label_for`) and `executors.py` (drop `MixedExecutor._BACKEND_KINDS`).
- [x] 3. Tests: `tests/test_harness_registry.py` (registry resolution,
  alias, capability rejection, executor parity, fake non-Prime harness
  driving a bounded node) and `tests/test_harness_identity_independent.py`
  (AST architecture test: harness ≠ provider ≠ model).
- [x] 4. Preserve mixed `--executor-map` visibility semantics; regression
  test in `tests/test_certification_executor_visibility.py`.
- [x] 5. Local gate: `ci_local.sh` push-set PASS on the merged head;
  vgate pre-push PASS; PR checks green on exact head.
- [ ] (post-merge) Live acceptance on installed main: the hourly
  `verdict-acceptance.timer` run stays green and a bounded Prime work unit
  through `verdict orchestrate` produces a receipt with
  `harness="prime-headless"` and unchanged model identity.
