# Tasks

Owned files: `verdict/provider_bootstrap.py` (`BootstrapDiagnostic.describe` only),
`verdict/api.py` (the `lifespan` diagnostic loop only),
`tests/test_diagnostic_credential_name_redaction.py`, one added test in
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

## 5. Verification (offline, fixtures only)
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

Not a ticked task: a gate log captured before this change keeps the old line and
still fails `scripts/check_dod.py`. It has to be captured again from a build with this
change. Live verification on the operator host and DoD acceptance from merged main are
not claimed here.
