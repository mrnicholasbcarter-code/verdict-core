# Tasks

Owned files: `verdict/api.py`, `verdict/doctor_diagnostics.py`,
`verdict/doctor_presentation.py`, `tests/test_serve_management_token.py`,
`tests/test_doctor_management_token.py`, one added test in
`tests/test_cli_inprocess.py`, and this change.

## 1. Serve uses the resolver
- [x] 1.1 Tests first: `tests/test_serve_management_token.py` (6 tests). Before the change,
  4 failed: store-only, blank-env, health-only-without-token and store-token startup
  diagnostic. After: 6 passed.
- [x] 1.2 `_build_availability_cache` and `server_bootstrap_diagnostics` call
  `credentials_store.resolve_management_token()`. These were the only
  `OMNIROUTE_MANAGEMENT_TOKEN` reads in `verdict/api.py`.

## 2. Doctor validates the token
- [x] 2.1 Tests first: `tests/test_doctor_management_token.py`. The first 20 tests all
  failed before `_doctor_management_token` existed. The 21st,
  `test_store_value_copied_into_the_environment_is_still_the_credential_store`, was
  added after the end-to-end test in 2.4 showed that the CLI entry's `load_into_env`
  made a stored token look exported. After the fix: 21 passed. They cover absent, valid
  (environment, store, and a store value copied into the environment), 401 and 403
  with source and repair, environment override wording, no-retry warnings for
  URLError, timeout, 404, 429 and 500, a non-JSON body or 203, no gateway, header
  injection, and no token value in the doctor JSON.
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

## 3. Verification (offline, fixtures only)
- [x] 3.1 Focused and related suites: `tests/test_doctor*.py`, `tests/test_api*.py`,
  the two new files, `test_cli_inprocess`, `test_omniroute_catalog_size`,
  `test_gateway_key_resolution`, `test_eligibility_gate`,
  `test_provider_bootstrap_adoption`, `test_http_safety_static`,
  `test_credentials_store`, `test_identify_cli`, `test_gateway_lifecycle_surfaces`,
  `test_resilience_connections_eligibility`, `test_actions_*` (json golden, boundary,
  parity, f2 parity, entrypoint parity), `test_home_action_rendering`,
  `test_visual_system`, `test_proxy` and `test_gateway_kind_config`: 643 passed and 2
  failed. The 2 failures are in `tests/test_doctor_service_code_path.py` (prime-sync
  working directory). They fail the same way on base `8710cfce` in a clean worktree,
  and this change does not cause them.
- [x] 3.2 `ruff check` and `ruff format --check` pass on the changed files.
  `mypy verdict --strict` reports no issues in 326 source files.
  `scripts/check_reachability.py --ratchet-base origin/main` reports 0 new findings.
  `scripts/check_doc_links.py` passes.
- [ ] 3.3 Live check on the operator host from merged main: `verdict doctor` shows the
  Management token row, and the acceptance serve check reads connections without an
  EnvironmentFile. Not done in this slice; it stays with BOD-219.
