# Tasks

## 1. Implementation

Verification: focused tests pass (`uv run python -m pytest -q tests/test_orch_executors.py tests/test_acceptance.py tests/test_setup_host.py tests/test_doctor_host_prerequisites.py`), ruff, mypy --strict. Test: unit tests.

- [x] 1.1 Registry default and `--executor` default become `direct-gateway`; `_worker_executor` falls back to the registry default
- [x] 1.2 `build_selector` default visibility follows the registry default; mixed maps with a Prime node and `--sync-visibility` keep Prime visibility
- [x] 1.3 `harness-prime` acceptance check is skipped without Prime and never counted as passed
- [x] 1.4 Setup plans no Prime registry and doctor reports Prime rows `not-installed` without the Prime binary
- [x] 1.5 docs/CLI_REFERENCE.md and docs/CONFIGURATION.md describe direct-gateway as the default

## 2. Tests

Verification: the named tests fail on main and pass here. Test: unit tests.

- [x] 2.1 `tests/test_orch_executors.py`: default builds direct-gateway; `prime` and `prime-headless` build Prime; `--executor-map` mixes
- [x] 2.2 `tests/test_certification_executor_visibility.py`: live route missing from the Prime registry is visible by default, refused under Prime
- [x] 2.3 `tests/test_acceptance.py`: Prime absent, installed but not enabled, installed and enabled
- [x] 2.4 `tests/test_setup_host.py`, `tests/test_doctor_host_prerequisites.py`, `tests/test_cli_inprocess.py`: no Prime planned; `not-installed` rows are not issues

## 3. Verification

Verification: operator runs the command on installed main; raw output names the executor. Test: post-merge live proof.

- [ ] (post-merge) Run `verdict orchestrate` without `--executor` on installed main and confirm the receipt names `direct-gateway`
