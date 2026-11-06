# Tasks

Owned files: `verdict/ocr_release.py`, `verdict/serve_service.py`,
`verdict/setup_host.py` (new); `verdict/capability_bootstrap.py`, `verdict/cli.py`
(setup wiring only), `verdict/harness_prime.py`, `verdict/doctor_diagnostics.py`,
`verdict/doctor_presentation.py`, `scripts/check_dev_readiness.py`,
`reachability-baseline.json`; `tests/test_ocr_release.py`,
`tests/test_serve_service.py`, `tests/test_setup_host.py`,
`tests/test_doctor_host_prerequisites.py`, the host stub and one test in
`tests/test_cli_inprocess.py`, the host stub in `tests/test_doctor_shared_memory.py`;
three docs; this change.

## 1. One ocr helper
- [x] 1.1 Tests first: `tests/test_ocr_release.py` (11 tests) failed at collection
  before `verdict/ocr_release.py` existed. After: 11 passed; with
  `tests/test_check_dev_readiness.py` 50 passed (its 39 tests unchanged).
- [x] 1.2 `ocr_release` reads the pin from `certify-rehearsal.yml`, checks the
  digest before running a binary, and installs only verified bytes;
  `scripts/check_dev_readiness.py` imports `ocr_pin`, `inspect_ocr`,
  `OCR_ASSET_URL` and `REHEARSAL_WORKFLOW` from it.

## 2. Serve unit installer
- [x] 2.1 Tests first: `tests/test_serve_service.py` (12 tests) failed at collection
  before `verdict/serve_service.py` existed; after: 12 passed. Two more tests
  (loose env-file mode is rewritten 0600; `install_problems` names keys, never
  values) failed first (2 failed, 12 passed), then 14 passed.
- [x] 2.2 Render, dry-run, rollback copies, `enable --now` and `try-restart` on
  change; token reuse order environment, credential store, env file; generation
  with `secrets.token_urlsafe` stored in the credential store; fail closed on a
  token the env file cannot carry.

## 3. Setup plans and applies host prerequisites
- [x] 3.1 Tests first: `tests/test_setup_host.py` (16 tests) failed at collection
  before `verdict/setup_host.py` and the `harness_prime` registry helpers existed
  (the helpers were stashed while the tests were written). After: 16 passed.
- [x] 3.2 `capability_bootstrap.run_bootstrap` takes `host_planner`/`host_runner`;
  host actions join all-scope plans, pass the existing consent gate, are re-planned
  after drift, and roll back as manual undo. `verdict setup --recommended` and
  `--apply` pass them; the classic `--dry-run`/`--plan` path does not.
- [x] 3.3 Tests authorize host ids only (`--allow`), so no catalog install action
  runs in a test.
- [x] 3.4 Existing setup, bootstrap, harness and CLI suites: 262 passed
  (`test_capability_bootstrap`, `test_bootstrap_presentation`, `test_setup_plan`,
  `test_cli_terminal_presentation`, `test_setup_preservation_ui`,
  `test_credentials_masking`, `test_harness_prime`, `test_harness_prime_sync_models`,
  `test_cli_inprocess`, `test_terminal_ui`, `test_metadata_service_state`,
  `test_identify_service`, `test_prove_at_rest_service_state`,
  `test_harness_bootstrap_surfaces`).

## 4. Doctor rows with exact repairs
- [x] 4.1 Tests first: `tests/test_doctor_host_prerequisites.py` (14 tests): 14 failed
  before `setup_host.doctor_rows`, the Acceptance timer row, the timer repair
  wording and the explicit-repair branch of `repair_command`; after: 14 passed.
- [x] 4.2 End to end:
  `tests/test_cli_inprocess.py::test_cli_doctor_failed_host_prerequisite_names_its_repair_both_modes`
  errored with `doctor_diagnostics.py` stashed (no `host_prerequisite_rows`), then
  passed: `--json` and text mode exit 1 and text prints the exact `Repair:` command.
- [x] 4.3 Full-collector tests stub `host_prerequisite_rows` beside their existing
  systemd stubs. `tests/test_doctor*.py`, `test_cli_inprocess`, `test_actions_*`
  parity and boundary, `test_identify_service`, `test_home_setup_warm_cache`,
  `test_http_safety_static`, `test_visual_system`: 449 passed.

## 5. Docs, reachability and verification (offline, fixtures only)
- [x] 5.1 `docs/CLI_REFERENCE.md` (setup and doctor), `docs/guides/setup-preview.md`
  and `docs/guides/harnesses.md` describe what runs; `scripts/check_doc_links.py`
  passes.
- [x] 5.2 `scripts/check_reachability.py --ratchet-base 3293cb3c`: 0 new findings.
  The test-only `serve_service.unit_state` was removed; the stale
  `harness_prime.enable` entry was dropped and five `harness_prime` entries got
  triage reasons because this branch changed the module.
- [x] 5.3 `ruff check .`, `ruff format --check .`, `mypy verdict --strict` (329
  source files) and `git diff --check` pass.

Post-merge parent acceptance is not a ticked task. From installed merged main the
parent runs `verdict setup --recommended` and `verdict setup --apply --yes` on the
operator host, then `verdict doctor`, and expects the Host prerequisites rows and
the Acceptance timer row to be OK with `ocr` matching the pin, `verdict-serve.service`
active with `/health` answering, and `~/.prime/agent/models.json` listing models.
That evidence is recorded on BOD-219. This list records local, offline
verification only; it is not a merged-main, installed-host acceptance or Done claim.
