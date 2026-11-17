# Tasks

Owned files: `verdict/provider_bootstrap.py` (`BootstrapDiagnostic.describe` and the
private `_redact_free_text` helper only), `verdict/api.py` (the `lifespan` diagnostic
loop only), `tests/test_diagnostic_credential_name_redaction.py`,
`tests/test_bootstrap_diagnostics_carry_no_credential_values.py`, one added test in
`tests/test_check_dod.py`, one pinned separator in
`tests/test_provider_bootstrap_adoption.py`, `docs/CONFIGURATION.md`, `CHANGELOG.md`,
and this change. `verdict/security.py` is not changed.

## 1. Root cause
- [x] 1.1 Found the matching pattern. On `efaacf38`, only the last entry of
  `verdict.security._SECRET_PATTERNS` (index 4, the `api_key|token|password|secret`
  label pattern) matches the reported line. Its one match is
  `OMNIROUTE_API_KEY: provider`, prefix `OMNIROUTE_API_KEY: `, and `redact_text` turns
  `provider` into `[redacted]`. The four `CREDENTIAL_SHAPES` patterns and the URL
  userinfo rule do not match the line.
- [x] 1.2 Found the other emitter of the same format: the `verdict serve` startup loop
  in `verdict/api.py` builds the line by hand. Its `runtime_sources_health_only` line
  is rewritten to `field=OMNIROUTE_MANAGEMENT_TOKEN: [redacted] sources: ...`. Probed
  the other credential-name renderings through `redact_text`. None of these changed:
  harness `token_env: <NAME> (set: ...)`, JSON `api_key_env`/`token_env`, the doctor
  `OMNIROUTE_MANAGEMENT_TOKEN rejected: HTTP ...` issue, the
  `Enter value for <NAME>: ` prompt, and the `<NAME>  SET` status row.

## 2. Secret shapes pinned before any change
- [x] 2.1 `tests/test_diagnostic_credential_name_redaction.py::test_realistic_secret_shapes_stay_redacted`
  has 11 fake shapes with their exact redacted output:
  `OMNIROUTE_API_KEY: sk-...`, `OMNIROUTE_API_KEY=abc123...`,
  `api_key: <random token>`, double- and single-quoted values,
  `Authorization: Bearer`, bare `Bearer`, `NAME:` and `api_key:` followed by a 20-letter
  lowercase word, and `field=OMNIROUTE_API_KEY: <word or sk- key> provider ...`.
  Committed alone as `c1ab5d49` on unchanged code: 11 passed. After the fix: 11
  passed.

## 3. Red tests
- [x] 3.1 Red first, with the final tests on `efaacf38` product code:
  `tests/test_diagnostic_credential_name_redaction.py`, `tests/test_check_dod.py` and
  `tests/test_provider_bootstrap_adoption.py` gave 5 failed, 79 passed. The failures:
  `test_credential_env_missing_note_renders_without_a_name_colon_label` (the line was
  `... field=OMNIROUTE_API_KEY: provider ...`),
  `test_every_bootstrap_note_line_survives_the_redactor` (redacted to
  `field=OMNIROUTE_API_KEY: [redacted] 'omniroute' ...`),
  `test_route_stderr_note_survives_the_redactor`,
  `test_serve_startup_lines_survive_the_redactor` (redacted to
  `field=OMNIROUTE_MANAGEMENT_TOKEN: [redacted] sources: ...`), and
  `tests/test_check_dod.py::test_verdicts_own_missing_credential_note_is_not_a_credential`
  (`raw output docs/proof/dod/confirm.txt holds an unredacted credential`).

## 4. Emitter fix
- [x] 4.1 `BootstrapDiagnostic.describe()` puts ` - ` after the field label and the
  optional source, where it had `: `. The `verdict serve` loop now renders each entry
  through `BootstrapDiagnostic(...).describe()` with no source, so the serve line
  keeps its old content apart from the separator. Green: the same three files give 84
  passed.
- [x] 4.2 Updated the golden. With the fix and the old golden,
  `test_resolve_default_providers_announces_the_builtin_set` failed (1 failed, 31
  passed). It now pins `[configuration] field=providers source=default - `, which is
  the new format of the message.
- [x] 4.3 `docs/CONFIGURATION.md` ("Secrets in rendered output") documents the line
  format and why the field is never followed by `:` or `=`. `CHANGELOG.md` records the
  format change.

## 5. Verification of the first round at `369c4dd0` (offline, fixtures only)
- [x] 5.1 27 test files: the 6 that `git grep -l "redact_text\|verdict.security" tests/`
  lists, every file that mentions `provider_bootstrap`, `BootstrapDiagnostic`,
  `BootstrapError`, `server_bootstrap`, `prime_supervisor`, `credential_env_missing` or
  `precedence_conflict`, and `tests/test_check_dod.py`. Added to those:
  `tests/test_cli_inprocess.py`, `tests/test_api_dispatch_permits.py` and
  `tests/test_api_relay_context_length.py`. Result: 828 passed.
- [x] 5.2 `ruff check .`, `ruff format --check .`, `mypy verdict --strict` (no issues
  in 326 source files), `bandit -r verdict -ll -q` (exit 0),
  `scripts/check_doc_links.py`, `scripts/check_reachability.py --ratchet-base origin/main`
  (0 NEW), `scripts/verify_proof_matrix.py` and `git diff --check` pass.

## 6. Review fixes (cross-family review of `369c4dd0`)
- [x] 6.1 Fail-closed regression. Under the old `field=NAME: detail` layout the label
  pattern also caught a secret at the start of a detail, by accident. With ` - ` that
  went away. Red first, with the final tests on `369c4dd0` product code: across
  `tests/test_diagnostic_credential_name_redaction.py`,
  `tests/test_bootstrap_diagnostics_carry_no_credential_values.py`,
  `tests/test_check_dod.py`, `tests/test_provider_bootstrap_adoption.py` and
  `tests/test_provider_bootstrap.py`, 12 failed and 168 passed. Failing:
  `test_describe_redacts_secret_shapes_in_field_detail_and_remediation` (the `sk-` key,
  the `Bearer` token and `token=<random>` were printed),
  `test_describe_keeps_the_documented_userinfo_marker` (the raw userinfo password was
  printed), and all 10 cases of
  `test_every_emitter_renders_names_safely_and_redacts_free_text` (each printed
  `sk-fake0000notreal0000example`).
- [x] 6.2 `describe()` now passes the field, detail and remediation through
  `redact_text` one part at a time. `_redact_free_text` shields the documented
  `***:***@` userinfo marker first, because `redact_text` would rewrite it to
  `[redacted]@`. The first attempt without the shield failed
  `tests/test_provider_bootstrap_adoption.py::test_cli_route_gate_never_echoes_url_userinfo`
  (1 failed, 178 passed), and the shield fixed it. The false positive did not come
  back: the redactor sees each part alone, with no `NAME:` label in front of it, and
  `test_credential_env_missing_note_renders_without_a_name_colon_label` still pins the
  exact line. Green: the same five files give 180 passed.
- [x] 6.3 Limitation, said plainly: a long random lowercase word, or any bare random
  string with no label and no known prefix, still gets through `describe()`.
  `test_limitation_an_unlabelled_value_with_no_known_shape_is_not_caught` pins that.
  It passed on first run because it documents existing behaviour.
- [x] 6.4 Structural guarantee: `tests/test_bootstrap_diagnostics_carry_no_credential_values.py`.
  It sets distinct, shapeless fake values. Each non-URL name in `CREDENTIALS` and
  `GATEWAY_KEY_ENV_NAMES`, plus the config-declared `FAKE_ENV_API_KEY`, gets one value
  in the exported environment and another in the credential store (passed, and on disk
  for the entry points). `FAKE_STORE_ONLY_API_KEY` is in the store only.
  `FAKE_UNSET_API_KEY` deliberately has no value, so `credential_env_missing` fires.
  URL names carry a fake userinfo password. The gateway key is also on the start
  command line, in the key file and in Prime's `auth.json`. This wording was narrowed
  in 8.3; at `d971ff26` it said every `api_key_env` name got a value. It asserts it reached
  27 diagnostic codes: all 16 `configuration` and all 9 `gateway_health` codes listed
  in `docs/CONFIGURATION.md`, plus the serve-only `runtime_sources_health_only` and
  `default_upstream_base_url`. It reaches them through the resolver,
  `verify_gateway_reachable`, the lifecycle
  (`ensure_gateway_ready`, `inspect_gateway`, `require_gateway_ready`), `verdict route`,
  the library gate, `verdict serve` startup and the Prime supervisor, and it checks
  every rendering. 10 passed on `369c4dd0` product code and after the fix: no existing
  diagnostic carries a value. Mutation check: with `redact_url` removed from the
  `precedence_conflict` provider-URL detail, the `resolver-valid-config` case failed on
  `fakeurlpw02qzxkvbnwmtrp`. Reverted.
- [x] 6.5 Downstream emitters through their seams:
  `test_every_emitter_renders_names_safely_and_redacts_free_text` covers
  `describe_bootstrap_failure`, `BootstrapError`, `verdict route` (refusal and notes,
  through `verdict.cli._build_route_gate`), the library gate notes
  (`verdict.gate.resolve_default_providers`), `verdict serve` startup (refusal and
  notes, through `TestClient(api.app)`), the serve precedence-conflict lines
  (`api._build_intelligence`), and the Prime supervisor's
  `_build_intelligence_service_from_config` and `_live_admission_gateway`. The resolver
  is patched at its module, which every emitter imports at call time. At `d971ff26`
  these went through private builders. 8.1 moves them to public entries.
- [x] 6.6 Isolation: every new test that resolves credentials runs under a temporary
  `HOME`, `VERDICT_HOME` and `XDG_CONFIG_HOME`, through an autouse fixture in
  `tests/test_diagnostic_credential_name_redaction.py`, the `fakes` fixture in the
  structural file and `monkeypatch` in the `tests/test_check_dod.py` test. Each also
  passes `credential_store_env` explicitly or writes its own store.

## 7. Verification at `d971ff26` (offline, fixtures only)
- [x] 7.1 28 test files (the 27 of 5.1 plus the new structural file), with
  `tests/test_cli_inprocess.py`, `tests/test_api_dispatch_permits.py` and
  `tests/test_api_relay_context_length.py`: 852 passed. Full suite with `-n 6`: 11063
  passed and 1 failed. The failure was
  `tests/test_orch_runtime.py::test_slow_selector_reaches_full_concurrency_fast`
  (`took 1.69s`, limit 1.5s under parallel load). Run alone, it passed 3 times out of 3,
  and it does not touch these modules.
- [x] 7.2 `ruff check .`, `ruff format --check .`, `mypy verdict --strict` (no issues in
  326 source files), `bandit -r verdict -ll -q` (exit 0) pass.

## 8. Round-2 review fixes (cross-family review of `d971ff26`, test seams only)
- [x] 8.1 Public entries. In both test files, `verdict route` now runs through
  `cli.main` with `--skip-setup route hello --allow-offline --terse`. `run_action` is
  replaced by a sentinel so the run stops after the bootstrap emitters and before any
  routing decision. A refusal exits 1. The library gate goes through the public `Gate`
  constructor, with a refusal and with notes. `verdict serve` goes through
  `TestClient(api.app)`. Its notes case also makes serve print a `precedence_conflict`
  for a config `primary_model` that holds an `sk-` key, so the private
  `api._build_intelligence` call and the `api._serve_field_resolution` patch are gone.
  The Prime supervisor goes through `main --select-only` with the real production
  factory. Two things are faked there because they are not emitters: `run_attempt`,
  which must never launch, and `_resolve_subagent_report_routes`, a live OmniRoute
  lookup. Kept private, with the reason in its docstring:
  `_live_admission_gateway`. It runs only inside the live-admission loader that
  controller selection calls after a production bundle exists, so no public entry
  reaches it offline, and it is the one emitter that raises
  `AdmissionUnavailableError`. Red on `369c4dd0` product code, in a throwaway
  detached worktree: 12 failed and 17 passed in
  `tests/test_diagnostic_credential_name_redaction.py`. All 10 emitter cases failed on
  `sk-fake0000notreal0000example`. On this head: 29 passed.
- [x] 8.2 Network isolation. `tests/test_diagnostic_credential_name_redaction.py` has an
  autouse fixture that requests `no_gateway_network`. It covers both serve tests, the
  route and gate cases and the supervisor. The structural file's `fakes` fixture
  already requested it.
- [x] 8.3 Structural claim. The test docstring, the spec and `docs/CONFIGURATION.md`
  now say exactly where each fake value goes, and which names stay unset on purpose
  and why: `FAKE_UNSET_API_KEY` everywhere, plus per-path unsets for the gateway key,
  the management token, `OMNIROUTE_BASE_URL` and the serve URL names. Both
  parametrized tests also check that every credential name that reaches a rendered
  line has a fake value in some source, or is `FAKE_UNSET_API_KEY`.
  `test_the_name_check_sees_every_config_declared_name` shows the check is not
  vacuous. `test_fake_values_go_exactly_where_the_docstring_says` pins the layout.
  `cli.main` copies the on-disk store into `os.environ` outside `monkeypatch`, so the
  route helper records each stored name as unset first. After the structural file
  ran, no fake value was left in `os.environ` (checked with a session-finish hook:
  `LEAKED_ENV: []`). Result: 11 passed.
- [x] 8.4 One finding, not changed: the supervisor's report is JSON. Inside JSON,
  `token=[redacted]` followed by the closing quote is not a fixed point of
  `redact_text`, because the label pattern takes the quote with it. The supervisor case
  therefore runs the leak check on the raw stdout and the line checks on the decoded
  controller error. This is a property of the shared redactor on JSON, not of these
  emitters, and real diagnostics carry no labelled values (6.4).

## 9. Verification of the final head (offline, fixtures only)
- [x] 9.1 The 28 test files of 7.1 with `tests/test_cli_inprocess.py`,
  `tests/test_api_dispatch_permits.py` and `tests/test_api_relay_context_length.py`:
  853 passed. Full suite with `-n 6`: 11063 passed and 2 failed. Both failures are
  wall-clock limits under parallel load:
  `tests/test_demo_trace.py::test_scripted_workers_overlap_when_duration_is_positive`
  (4.61s, limit 3.0s) and
  `tests/test_orch_runtime.py::test_slow_selector_reaches_full_concurrency_fast`
  (1.58s, limit 1.5s). Run alone, both passed 3 times out of 3. Neither file uses
  `provider_bootstrap`, `redact_text` or `credentials_store`. This round changed tests
  only; product code is as at `d971ff26`.
- [x] 9.2 `ruff check .`, `ruff format --check .`, `mypy verdict --strict` (no issues in
  326 source files), `bandit -r verdict -ll -q` (exit 0), the pinned OpenSpec CLI
  `validate --strict`, `scripts/check_doc_links.py`,
  `scripts/check_reachability.py --ratchet-base origin/main` (0 NEW) and
  `git diff --check` pass.

Not a ticked task: a gate log captured before this change keeps the old line and
still fails `scripts/check_dod.py`. It has to be captured again from a build with this
change. Live verification on the operator host and DoD acceptance from merged main are
not claimed here.
