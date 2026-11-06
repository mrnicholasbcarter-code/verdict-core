# Tasks

Owned files: `verdict/subagent_models.py`, `verdict/worker_runtime.py`,
`verdict/credentials_store.py` (`get_credential_source` only),
`tests/test_runtime_management_token.py`, three added tests in
`tests/test_credentials_store.py`, one added test and two added assertions in
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
  (the doctor `--json` Credentials row for `OMNIROUTE_MANAGEMENT_TOKEN` after `cli.main`
  ran `load_into_env`; text mode is task 3.4). Before the change: 2 failed, 23 passed across
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
- [x] 3.2 `verdict credentials list` end to end:
  `tests/test_cli_inprocess.py::test_cli_credentials_list_names_store_copies_and_exports_without_values`
  runs `verdict credentials list --json`, then `verdict credentials list` (text), each
  through `cli.main`. In both, the store-only `OMNIROUTE_MANAGEMENT_TOKEN`, which
  `cli.main` copies in, lists as `store` (JSON) and `STORE` (text). `GEMINI_API_KEY`,
  exported with a value equal to its stored one, lists as `env` and `ENV`. Neither value
  appears in either output. This test covers `credentials list` only, not doctor. Red
  against `efaacf38` behaviour: with `verdict/credentials_store.py` temporarily restored
  from `efaacf38` (`git show efaacf38:verdict/credentials_store.py`), this test and the
  3.1 test failed: 2 failed, both `assert 'env' == 'store'`. With the branch's file
  restored: 2 passed.

## 3b. Round-2 review fixes (cross-family review of `6129ae29`)
- [x] 3.3 The record does not keep a stale copy across loads (a review finding that
  does not hold: `load_into_env` clears the record at the start of each call).
  `tests/test_credentials_store.py::test_get_credential_source_drops_a_copy_the_next_load_does_not_repeat`
  makes two real `load_into_env()` calls with no manual reset of the record between
  them. Load 1 copies `TEST_KEY`, which lists as `store`. The process then exports the
  same string itself, so load 2 does not copy the name. After load 2 the record no
  longer holds `TEST_KEY`, and `get_credential_source` returns `env`. It passed on its
  first run (1 passed), because it pins the existing clearing from #959. There is no
  red phase. Mutation check: with the `_STORE_INJECTED.clear()` line in
  `load_into_env` temporarily replaced by `pass`, it failed
  (`assert 'TEST_KEY' not in {'TEST_KEY': '<digest>'}`, 1 failed). With the line
  restored: 1 passed.
- [x] 3.4 Doctor text mode. The doctor end-to-end test
  (`test_cli_doctor_rejected_store_management_token_fails_both_modes_without_the_value`)
  now also asserts the text Credentials row for `OMNIROUTE_MANAGEMENT_TOKEN` from
  `cli.cmd_doctor()` reads `STORE`, exactly once, and that the token value is absent.
  It passed on the branch (1 passed). Red: with `verdict/credentials_store.py` restored
  from `efaacf38` and the earlier `--json` row assertion temporarily disabled, so the
  run reaches the text check, it failed with `assert ['ENV'] == ['STORE']`. Both were
  then restored. With `efaacf38`'s file and both assertions enabled, the test stops at
  the `--json` assertion (`assert 'env' == 'store'`).
- Coverage by output mode:
  - `verdict credentials list` in `--json` and text:
    `test_cli_credentials_list_names_store_copies_and_exports_without_values`.
  - `verdict doctor` in `--json` (`cli.main`) and text (`cli.cmd_doctor()`):
    `test_cli_doctor_rejected_store_management_token_fails_both_modes_without_the_value`.
  - Both tests also assert that the value is absent from every output they check.

## 4. Verification (offline, fixtures only)
- [x] 4.1 Related suites: every test file that imports either module
  (`git grep -l "subagent_models\|worker_runtime" tests/`, 13 files including the new
  one), `test_serve_management_token`, `test_doctor_management_token`,
  `test_credentials_store`, `test_credentials_masking`, `test_cli_inprocess`, and the
  `get_credential_source` callers' suites `test_actions_json_golden`,
  `test_actions_entrypoint_parity`, `test_actions_f2_parity`, `test_actions_parity`
  and `test_actions_dispatch_b`: 23 files, 570 passed (after the round-2 fixes).
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
