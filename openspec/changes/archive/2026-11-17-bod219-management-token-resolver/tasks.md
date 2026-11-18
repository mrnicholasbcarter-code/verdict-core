# Tasks

Owned files: `verdict/api.py`, `verdict/doctor_diagnostics.py`,
`verdict/doctor_presentation.py`, `verdict/credentials_store.py` (provenance only;
the coordinator authorized it in the review), `tests/test_serve_management_token.py`,
`tests/test_doctor_management_token.py`, one added test each in
`tests/test_cli_inprocess.py` and `tests/test_credentials_store.py`, and this change.

## 1. Serve uses the resolver
- [x] 1.1 Tests first: `tests/test_serve_management_token.py` (6 tests). Before the change,
  4 failed: store-only, blank-env, health-only-without-token and store-token startup
  diagnostic. After: 6 passed.
- [x] 1.2 `_build_availability_cache` and `server_bootstrap_diagnostics` call
  `credentials_store.resolve_management_token()`. These were the only
  `OMNIROUTE_MANAGEMENT_TOKEN` reads in `verdict/api.py`.

## 2. Doctor validates the token
- [x] 2.1 Tests first: `tests/test_doctor_management_token.py`. The first 20 tests all
  failed before `_doctor_management_token` existed. Now 23 pass. They cover absent,
  valid (environment, store, and a store value copied into the environment), 401 and
  403 with source and repair, environment override wording, no-retry warnings for
  URLError, timeout, 404, 429 and 500, a non-JSON body or 203, no gateway, header
  injection, and no token value in text or JSON.
- [x] 2.2 `_doctor_management_token` makes one `open_no_redirect` GET of
  `/api/resilience/connections` with a 5 s timeout and a 16 MiB read bound, and the
  collector calls it after the gateway-key check.
- [x] 2.3 `repair_command` maps a rejected or forbidden `OMNIROUTE_MANAGEMENT_TOKEN`
  finding to `verdict credentials set OMNIROUTE_MANAGEMENT_TOKEN --stdin`, checked in
  `test_rejected_or_forbidden_token_is_a_failure_with_source_and_repair`.
- [x] 2.4 End to end:
  `tests/test_cli_inprocess.py::test_cli_doctor_rejected_store_management_token_fails_both_modes_without_the_value`
  (1 passed). `verdict doctor --json` and the text mode both exit 1 for a stored token
  OmniRoute rejects, the text shows the exact repair, and neither output contains the
  token.

## 3. Review fixes (cross-family review of `4f39cb51`)
- [x] 3.1 Recorded provenance. Red first:
  `test_exported_token_equal_to_the_stored_value_is_the_environment` and
  `tests/test_credentials_store.py::test_management_token_source_records_real_provenance`
  failed: 2 failed, 42 passed across `tests/test_doctor_management_token.py` and
  `tests/test_credentials_store.py`. `load_into_env` now records each injected name
  with a value digest, and `resolve_management_token_source` names the source from
  that record. Doctor uses it, and the value-equality heuristic is removed. After: 50
  passed across those two files and `tests/test_serve_management_token.py`.
- [x] 3.2 No network in the header-injection test. The test now uses `no_gateway_network`,
  which every test in `tests/test_doctor_management_token.py` uses through an autouse
  fixture, and the token is exported. The old version wrote the CR/LF token to the
  store. The store's line format dropped the injected header, so the real opener made
  a real connection. Under the guard it raised `LiveNetworkAttempted` for
  `('127.0.0.1', 20128)`. The exported CR/LF token is now refused by the real opener
  with `ValueError` before any socket is opened.
- [x] 3.3 Text no-leak: `test_token_value_never_appears_in_doctor_text_or_json` covers
  the absent, ok, warning, unreachable, 401 and 403 outcomes. Each runs through
  `run_action("doctor")` (JSON) and `cli.cmd_doctor()` (TerminalUI and
  `doctor_presentation` repair hints). 6 cases pass. These assertions found no leak,
  so there was no red phase.
- [x] 3.4 Round-2 review. `load_into_env` now clears the record at the start of each
  call, so the record covers the latest load only and a name that is not re-injected is
  dropped. Red first:
  `tests/test_credentials_store.py::test_repeated_load_into_env_records_only_the_latest_load`
  failed (1 failed, 51 passed across `tests/test_doctor_management_token.py`,
  `tests/test_credentials_store.py` and `tests/test_serve_management_token.py`). After:
  52 passed (doctor 23, credentials store 23, serve 6). The narrowed meaning is
  documented: `credential_store` means this process copied the value from the store in
  its latest load and the value is unchanged. An identical value exported later in the
  same process cannot be distinguished and is reported as `credential_store`.
  `test_limitation_identical_value_reexported_in_process_reads_as_credential_store`
  pins that behaviour. It passed on first run because it documents existing behaviour.

## 4. Verification (offline, fixtures only)
- [x] 4.1 Related suites: `tests/test_doctor*.py`, `tests/test_api*.py`, the new
  files, `test_cli_inprocess`, `test_omniroute_catalog_size`,
  `test_gateway_key_resolution`, `test_eligibility_gate`,
  `test_provider_bootstrap_adoption`, `test_http_safety_static`,
  `test_credentials_store`, `test_identify_cli`, `test_omniroute_evidence`,
  `test_gateway_lifecycle_surfaces`, `test_resilience_connections_eligibility`,
  `test_actions_*` (json golden, boundary, parity, f2 parity, entrypoint parity),
  `test_home_action_rendering`, `test_visual_system`, `test_proxy` and
  `test_gateway_kind_config`. With `tests/test_doctor_service_code_path.py` deselected:
  697 passed. That file is reported separately because this agent cannot set
  `PYTHONPATH`: its subprocess then resolves the shared venv's site-packages copy of
  `verdict` instead of the worktree, and 2 of its 8 tests fail in that invocation.
  This change does not touch that code path.
- [x] 4.2 `ruff check` and `ruff format --check` pass on the changed files.
  `mypy verdict --strict` reports no issues in 326 source files.
  `scripts/check_reachability.py --ratchet-base origin/main` reports 0 new findings.
  `scripts/check_doc_links.py` passes.

Post-merge parent acceptance is not a ticked task: from installed merged main, the
parent runs `verdict doctor` on the operator host and expects the Management token row,
then confirms the hourly acceptance serve check reads connections without the
`management-token.conf` EnvironmentFile drop-in. That evidence is recorded on BOD-219.
This task list records local, offline verification only; it is not a merged-main,
installed-host acceptance or Done claim.
