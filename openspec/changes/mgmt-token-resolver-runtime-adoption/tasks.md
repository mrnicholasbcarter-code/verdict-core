# Tasks

Owned files: `verdict/subagent_models.py`, `verdict/worker_runtime.py`,
`verdict/credentials_store.py` (`get_credential_source` only),
`tests/test_runtime_management_token.py`, two added tests in
`tests/test_credentials_store.py`, one added test and one added assertion in
`tests/test_cli_inprocess.py`, one table row in `docs/CONFIGURATION.md`, and this change.

Every pytest run below used an isolated home and the shared venv:
`H=$(mktemp -d); env -u OMNIROUTE_API_KEY -u VERDICT_OMNIROUTE_API_KEY -u OMNIROUTE_MANAGEMENT_TOKEN HOME=$H VERDICT_HOME=$H/state VERDICT_RECEIPTS_DB=:memory: VERDICT_LIVE_SMOKE=0 PYTHONPATH=$PWD OPENSPEC_BIN=<pinned openspec 1.13.2> .venv/bin/python -m pytest -q -p no:cacheprovider <tests>`.

## 1. Subagent selection and the worker runtime use the resolver
- [x] 1.1 Tests first: `tests/test_runtime_management_token.py`, 6 tests, each run
  against both seams (`SubagentModelSelector.from_environment` and
  `worker_runtime.cli_run`), so 12 cases. A recorder replaces the OmniRoute transport
  and captures its keyword arguments; no transport is built and no socket is opened.
  Before the change: 6 failed, 6 passed. The failures were store-only
  (`assert None == 'store-management-sentinel'`), blank-falls-back-to-store
  (`assert '   ' == 'store-management-sentinel'`) and blank-without-store
  (`assert '   ' is None`), each for both seams. Exported-wins, no-token and
  insecure-store passed, because they pin today's behaviour.
- [x] 1.2 Both seams call `credentials_store.resolve_management_token()`. These were the
  only direct `OMNIROUTE_MANAGEMENT_TOKEN` reads left under `verdict/`
  (`git grep -n OMNIROUTE_MANAGEMENT_TOKEN -- verdict`). After: 12 passed.

## 2. Credential listings report recorded provenance
- [x] 2.1 Tests first:
  `tests/test_credentials_store.py::test_get_credential_source_names_a_value_load_into_env_copied_as_the_store`
  and one assertion added to
  `tests/test_cli_inprocess.py::test_cli_doctor_rejected_store_management_token_fails_both_modes_without_the_value`
  (the doctor Credentials row for `OMNIROUTE_MANAGEMENT_TOKEN` after `cli.main` ran
  `load_into_env`). Before the change: 2 failed, 23 passed across
  `tests/test_credentials_store.py` and that test, both with
  `assert 'env' == 'store'`.
- [x] 2.2 `get_credential_source` returns `store` when `injected_from_store` says the
  current environment value equals the value the latest load copied in. After: 25
  passed.

## 3. Review fixes (cross-family review of `c0ff835a`)
- [x] 3.1 Narrowed provenance claim. The requirement, the proposal and the
  `get_credential_source` docstring now say `store` means "the current value equals the
  value `load_into_env` copied from the store in its latest load". They no longer say
  "the process has not changed it". The limitation is stated: a value changed away and
  set back to the exact copied string in the same process reads as `store`. No mutation
  tracking is added.
  `tests/test_credentials_store.py::test_limitation_value_changed_away_and_back_in_process_lists_as_store`
  pins that case. It passed on its first run on the branch (1 passed), because it
  documents existing behaviour. There is no red phase. Against the `efaacf38`
  `verdict/credentials_store.py` it fails (`assert 'env' == 'store'`), only because that
  version never reports `store` for an environment value.
- [x] 3.2 Listing path end to end:
  `tests/test_cli_inprocess.py::test_cli_credentials_list_names_store_copies_and_exports_without_values`
  runs `verdict credentials list --json` and text mode through `cli.main`. The store-only
  `OMNIROUTE_MANAGEMENT_TOKEN`, which `cli.main` copies in, lists as `store`/`STORE`.
  `GEMINI_API_KEY`, exported with a value equal to its stored one, lists as
  `env`/`ENV`. Neither value appears in either output. Red against `efaacf38`
  behaviour: with `verdict/credentials_store.py` temporarily restored from `efaacf38`
  (`git show efaacf38:verdict/credentials_store.py`), this test and the 3.1 test failed:
  2 failed, both `assert 'env' == 'store'`. With the branch's file restored: 2 passed.

## 4. Verification (offline, fixtures only)
- [x] 4.1 Related suites: every test file that imports either module
  (`git grep -l "subagent_models\|worker_runtime" tests/`, 13 files including the new
  one), `test_serve_management_token`, `test_doctor_management_token`,
  `test_credentials_store`, `test_credentials_masking`, `test_cli_inprocess`, and the
  `get_credential_source` callers' suites `test_actions_json_golden`,
  `test_actions_entrypoint_parity`, `test_actions_f2_parity`, `test_actions_parity`
  and `test_actions_dispatch_b`: 23 files, 569 passed.
- [x] 4.2 `ruff check .` and `ruff format --check .` pass.
  `mypy verdict --strict` reports no issues in 326 source files.
  `bandit -r verdict -ll -q` exits 0 with no issues.
  `scripts/check_reachability.py --ratchet-base origin/main` reports 0 new findings and
  0 new orphan modules. `scripts/check_doc_links.py` passes (447 files).
  `git diff --check` is clean.
- [x] 4.3 `openspec validate mgmt-token-resolver-runtime-adoption --strict` with the
  pinned OpenSpec 1.13.2 CLI: valid.

Post-merge acceptance is not a ticked task. From installed merged main, on the operator
host with the token only in the credential store, a supervisor-driven worker run should
read `connections`, and `verdict doctor` should list `OMNIROUTE_MANAGEMENT_TOKEN` as
`store`. This list records local, offline verification only. It is not a merged-main,
installed-host acceptance or Done claim.
