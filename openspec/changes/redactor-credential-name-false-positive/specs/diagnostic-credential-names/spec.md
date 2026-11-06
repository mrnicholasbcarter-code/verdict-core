## ADDED Requirements

### Requirement: A credential name in a diagnostic line is never followed by a colon or an equals sign
`BootstrapDiagnostic.describe()` MUST render
`<code> [<class>] field=<field>[ source=<source>] - <detail>; remediation: <remediation>`.
The field label and the optional source MUST be followed by ` - `, never by `:` or `=`.
Every bootstrap diagnostic line Verdict prints MUST be rendered by `describe()`. This
covers the `verdict route` and library gate stderr notes, the `verdict serve` startup
lines, `describe_bootstrap_failure`, `BootstrapError` messages and the Prime
supervisor's launch detail. The `verdict serve` startup line keeps its `verdict serve: `
prefix and carries no source. A line that names a credential and holds no credential
value MUST be returned unchanged by `verdict.security.redact_text`, so
`scripts/check_dod.py` MUST accept a raw output that holds it.

#### Scenario: Missing gateway key note
- **WHEN** the gateway provider declares `api_key_env: OMNIROUTE_API_KEY` and the name is
  unset in the environment and in the credential store
- **THEN** the CLI prints exactly
  `verdict: credential_env_missing [configuration] field=OMNIROUTE_API_KEY - provider 'omniroute' declares api_key_env OMNIROUTE_API_KEY but it is unset in the environment and in the credential store; remediation: export OMNIROUTE_API_KEY, or run 'verdict credentials set OMNIROUTE_API_KEY'`
- **AND** `redact_text` returns that line unchanged
- **TEST** `tests/test_diagnostic_credential_name_redaction.py::test_credential_env_missing_note_renders_without_a_name_colon_label`,
  `::test_every_bootstrap_note_line_survives_the_redactor`,
  `::test_route_stderr_note_survives_the_redactor`

#### Scenario: Serve startup lines
- **WHEN** `verdict serve` starts with a gateway URL, no gateway key and no management
  token
- **THEN** it prints a `[configuration]` line for `field=OMNIROUTE_API_KEY` and one for
  `field=OMNIROUTE_MANAGEMENT_TOKEN`, each starting with `verdict serve: `, and
  `redact_text` returns every `[configuration]` line unchanged
- **TEST** `tests/test_diagnostic_credential_name_redaction.py::test_serve_startup_lines_survive_the_redactor`

#### Scenario: DoD evidence holding the note
- **WHEN** a DoD raw-output artifact holds the bootstrap notes, including
  `credential_env_missing`, and nothing else that matches a secret
- **THEN** `scripts/check_dod.py` reports no error for it
- **TEST** `tests/test_check_dod.py::test_verdicts_own_missing_credential_note_is_not_a_credential`

#### Scenario: Pinned separator for a defaulted field
- **WHEN** the CLI falls back to the built-in provider set
- **THEN** stderr holds `[configuration] field=providers source=default - `
- **TEST** `tests/test_provider_bootstrap_adoption.py::test_resolve_default_providers_announces_the_builtin_set`

#### Scenario: Every emitter, through its public entry
- **WHEN** a refusal or a note with `field=OMNIROUTE_API_KEY` reaches
  `describe_bootstrap_failure`, a `BootstrapError` message, `verdict route` through
  `cli.main` (refusal, and notes up to the routing decision), the library `Gate`
  constructor (refusal and notes), `verdict serve` through `TestClient(api.app)`
  (refusal, and notes together with a `precedence_conflict` line), or the Prime
  supervisor's `main --select-only` with the real production factory
- **AND** the supervisor's `_live_admission_gateway`, the one emitter left on its
  private seam because it runs only inside controller selection after a production
  bundle exists, so no public entry reaches it offline
- **THEN** each output holds `field=OMNIROUTE_API_KEY - `, never `field=OMNIROUTE_API_KEY:`,
  and `redact_text` returns it unchanged. For the supervisor's JSON report the line
  checks run on the decoded controller error and the leak check on the raw stdout.
- **AND** every one of these tests runs under `no_gateway_network`
- **TEST** `tests/test_diagnostic_credential_name_redaction.py::test_every_emitter_renders_names_safely_and_redacts_free_text`

### Requirement: A diagnostic line redacts secret shapes in its free text
`BootstrapDiagnostic.describe()` MUST pass the field, the detail and the remediation
through `verdict.security.redact_text` one part at a time before formatting, so an `sk-`
key, a `Bearer` token or a labelled value such as `token=<value>` in any of them is
printed as `[redacted]`. URL userinfo MUST keep the documented `***:***@` form, whether
it was already redacted or not. Redacting each part on its own MUST NOT bring back the
false positive: the `credential_env_missing` line stays exactly as in the scenario
above. Limitation: a value with no label and no known prefix is not caught by this
step. The next requirement is the guarantee for such values.

#### Scenario: Secret shapes in field, detail and remediation
- **WHEN** a diagnostic has `field=providers.sk-...`, a detail holding
  `Authorization: Bearer <token>` and an `sk-` key, and a remediation holding
  `token=<random token>`
- **THEN** `describe()` returns exactly
  `gateway_unreachable [gateway_health] field=providers.[redacted] source=environment - gateway answered Authorization: Bearer [redacted] for key [redacted]; remediation: retry with token=[redacted]`
- **AND** every emitter above prints none of the three values
- **TEST** `tests/test_diagnostic_credential_name_redaction.py::test_describe_redacts_secret_shapes_in_field_detail_and_remediation`,
  `::test_every_emitter_renders_names_safely_and_redacts_free_text`

#### Scenario: Userinfo marker
- **WHEN** a detail holds one URL already rendered as `http://***:***@host` and one with
  raw `user:password@` userinfo
- **THEN** `describe()` shows both as `***:***@`, never `[redacted]@`, and the password is
  gone
- **TEST** `tests/test_diagnostic_credential_name_redaction.py::test_describe_keeps_the_documented_userinfo_marker`,
  `tests/test_provider_bootstrap_adoption.py::test_cli_route_gate_never_echoes_url_userinfo`

#### Scenario: Unlabelled value with no known shape (limitation)
- **WHEN** a detail holds a bare long lowercase word or a bare mixed-case random string
- **THEN** `describe()` prints it unchanged. This is a documented limitation, not a
  guarantee.
- **TEST** `tests/test_diagnostic_credential_name_redaction.py::test_limitation_an_unlabelled_value_with_no_known_shape_is_not_caught`

### Requirement: No bootstrap diagnostic carries a credential value
No `BootstrapDiagnostic` MUST be built from a credential value. The fake values are
distinct and placed as follows. Each non-URL name in
`verdict.credentials_registry.CREDENTIALS` and `GATEWAY_KEY_ENV_NAMES`, plus the
config-declared `FAKE_ENV_API_KEY`, gets one value in the exported environment and a
different one in the credential store. The config-declared `FAKE_STORE_ONLY_API_KEY`
gets a value in the credential store only. The config-declared `FAKE_UNSET_API_KEY`
deliberately gets no value, so `credential_env_missing` fires; it has no value to
leak. Each URL-valued registry name gets a URL with a fake userinfo password in the
exported environment. The gateway key also goes on the gateway start command line,
and in the `verdict route` case into the owner-only key file and Prime's `auth.json`.
Some paths unset names on purpose to reach a missing-input diagnostic. With these
values, no fake value MUST appear in any rendering of any diagnostic, and every
credential name that reaches a rendered line MUST have a fake value in some source or
be `FAKE_UNSET_API_KEY`. Renderings are
`describe()`, `to_dict()`, `BootstrapError` messages and `to_dict()`,
`describe_bootstrap_failure`, `BootstrapResult.to_dict()`, gateway lifecycle outcomes,
and the stderr and exception text of `verdict route`, the library gate,
`verdict serve` and the Prime supervisor. The fake values carry no secret shape, so
redaction cannot hide a leak.

#### Scenario: Resolver paths
- **WHEN** the resolver reports `credential_env_missing`, `default_primary_model`,
  `precedence_conflict` (provider URL and shadowed malformed environment values),
  `config_file_missing`, `no_provider_configuration`, `default_provider_fallback`,
  `default_providers_forbidden`, `config_file_unparsable`, `config_file_unreadable`
  (when not running as root), `config_file_not_mapping`, `config_providers_malformed`,
  `provider_entry_malformed`, `provider_base_url_missing`, `provider_base_url_invalid`
  (config and environment), `gateway_start_command_malformed` (string and type) or
  `gateway_ready_timeout_invalid`, with fake values in the config text, the start
  command, URL userinfo, the environment and the store
- **THEN** no fake value appears in any rendering
- **TEST** `tests/test_bootstrap_diagnostics_carry_no_credential_values.py::test_no_resolver_or_lifecycle_diagnostic_carries_a_credential_value`

#### Scenario: Gateway health and lifecycle paths
- **WHEN** `verify_gateway_reachable` reports `gateway_unreachable` (unhealthy answer or a
  raising probe) or `gateway_required_but_absent`, or the lifecycle reports
  `gateway_start_failed`, `gateway_process_exited`, `gateway_ready_timeout`,
  `gateway_unhealthy`, `gateway_auth_failed`, `gateway_remote_not_managed` or
  `gateway_start_command_missing`, through `ensure_gateway_ready`, `inspect_gateway` or
  `require_gateway_ready`, with the gateway key on the start command line
- **THEN** no fake value appears in any rendering
- **TEST** `tests/test_bootstrap_diagnostics_carry_no_credential_values.py::test_no_resolver_or_lifecycle_diagnostic_carries_a_credential_value`

#### Scenario: Public entry points
- **WHEN** `verdict route` (through `cli.main`) and the library `Gate` constructor run
  with the gateway key only in the key file and Prime's `auth.json`, with a valid and
  then a malformed config; `verdict serve` starts through `TestClient(api.app)` without
  the gateway key and the management token, and its startup report is built again
  with no gateway or upstream URL (`default_upstream_base_url`); and the Prime
  supervisor's `main --select-only` and its live-admission gateway refuse for missing
  providers
- **THEN** their stderr, stdout, exception text and `server_bootstrap_diagnostics()`
  report name the expected codes and hold no fake value
- **AND** the values `cli.main` copies from the on-disk store into the process
  environment are removed when the test ends
- **TEST** `tests/test_bootstrap_diagnostics_carry_no_credential_values.py::test_no_public_entry_point_prints_a_credential_value`,
  `::test_fake_values_go_exactly_where_the_docstring_says`,
  `::test_the_name_check_sees_every_config_declared_name`

### Requirement: Secrets written in the label syntax stay redacted
`verdict.security.redact_text` MUST keep redacting a credential value written after a
credential name or a key label. This covers `NAME: value`, `NAME=value`, a quoted value,
a token after `Bearer`, and a value that is one long lowercase word. It MUST also redact
a value that is followed by prose in the old diagnostic syntax
(`field=OMNIROUTE_API_KEY: <value> provider ...`). Changing the emitter MUST NOT change
these outputs.

#### Scenario: Realistic secret shapes
- **WHEN** `redact_text` receives, with fake values, `OMNIROUTE_API_KEY: sk-...`,
  `OMNIROUTE_API_KEY=abc123...`, `api_key: <long random token>`, a double-quoted or
  single-quoted value, `Authorization: Bearer <token>`, a bare `Bearer <token>`,
  `OMNIROUTE_API_KEY: <long lowercase word>` or `api_key: <long lowercase word>`
- **THEN** it returns the label followed by `[redacted]` and the value is gone
- **TEST** `tests/test_diagnostic_credential_name_redaction.py::test_realistic_secret_shapes_stay_redacted`

#### Scenario: Value followed by prose in the old diagnostic syntax
- **WHEN** `redact_text` receives `field=OMNIROUTE_API_KEY: <long lowercase word> provider 'omniroute' ...`
  or `field=OMNIROUTE_API_KEY: sk-... provider 'omniroute'`
- **THEN** it replaces only the value with `[redacted]` and keeps the prose
- **TEST** `tests/test_diagnostic_credential_name_redaction.py::test_realistic_secret_shapes_stay_redacted`
  (`field-label-lowercase-word-then-prose`, `field-label-sk-key-then-prose`)
