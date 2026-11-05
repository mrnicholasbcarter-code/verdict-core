## ADDED Requirements

### Requirement: Serve resolves the management token through the one resolver
`verdict serve` MUST obtain the OmniRoute management token only from
`credentials_store.resolve_management_token`, both for the availability transport and
for the startup diagnostic. A non-blank environment value MUST win over the credential
store, a blank environment value MUST be ignored, and the same-named credential-store
field MUST be used when the environment has none. The transport MUST read the
`connections` runtime source only when a token is resolved, and `health` only otherwise.

#### Scenario: Environment wins over the store
- **WHEN** both the environment and the credential store hold a management token
- **THEN** serve's transport receives the environment token and reads health and connections
- **TEST** `tests/test_serve_management_token.py::test_serve_prefers_the_environment_token_over_the_store`

#### Scenario: Store used when the environment has none
- **WHEN** only the credential store holds a management token
- **THEN** serve's transport receives the stored token and reads health and connections
- **TEST** `tests/test_serve_management_token.py::test_serve_uses_the_store_token_when_the_environment_has_none`

#### Scenario: Blank environment value is ignored
- **WHEN** the environment exports a blank management token
- **THEN** serve uses the stored token, or reads health only when the store has none
- **TEST** `tests/test_serve_management_token.py::test_serve_ignores_a_blank_environment_token`,
  `::test_serve_reads_health_only_without_any_token`

#### Scenario: Startup diagnostic names what serve resolves
- **WHEN** serve builds its startup diagnostics
- **THEN** `runtime_sources_health_only` appears only when the resolver returns no token,
  and the report never contains the token value
- **TEST** `tests/test_serve_management_token.py::test_startup_diagnostic_reflects_a_store_token`,
  `::test_startup_diagnostic_treats_a_blank_environment_token_as_absent`

### Requirement: Management-token provenance is recorded, not inferred
`CredentialsStore.load_into_env` MUST record each name it copies into the process
environment, keyed to a digest of the copied value and never the value itself.
`credentials_store.resolve_management_token_source` MUST return the same token as
`resolve_management_token`. Its source MUST be `credential_store` when the token came
from the store field, or when the environment value is the one `load_into_env`
injected and has not changed since. Its source MUST be `environment` for every other
non-blank exported value, including one equal to the stored value. Provenance MUST NOT
be derived from comparing the exported value with the stored value.

#### Scenario: Exported value equal to the stored value
- **WHEN** the environment exports a management token equal to the credential-store field
- **THEN** the source is `environment`, and a rejected token's doctor issue says that the
  export overrides the credential store
- **TEST** `tests/test_credentials_store.py::test_management_token_source_records_real_provenance`,
  `tests/test_doctor_management_token.py::test_exported_token_equal_to_the_stored_value_is_the_environment`

#### Scenario: Value copied from the store by the CLI entry
- **WHEN** `load_into_env` copied the stored token into the environment, and the process
  has not changed it since
- **THEN** the source is `credential_store`. If the process later overwrites the value,
  the source is `environment`.
- **TEST** `tests/test_credentials_store.py::test_management_token_source_records_real_provenance`,
  `tests/test_doctor_management_token.py::test_store_value_copied_into_the_environment_is_still_the_credential_store`,
  `tests/test_cli_inprocess.py::test_cli_doctor_rejected_store_management_token_fails_both_modes_without_the_value`

### Requirement: Doctor validates the resolved management token
`verdict doctor` MUST report a "Management token" row from the same resolver. When no
token resolves, it MUST add an info row that says "runtime sources: health only" and
gives the repair command, and it MUST NOT make a request. When a token resolves, it MUST
make exactly one read-only GET of the management route serve reads
(`/api/resilience/connections`) on the configured gateway origin, through
`open_no_redirect`, with a bounded timeout and response size and no retry. A 200 answer
with a JSON object or array MUST be OK. A 401 or 403 answer MUST be a doctor issue. Any
other outcome MUST be a warning and never OK. Every row and issue MUST name the token's
source (`environment` or `credential_store`) and MUST NOT contain the token value. A
rejected or forbidden token MUST name the repair
`verdict credentials set OMNIROUTE_MANAGEMENT_TOKEN --stdin` and say that OmniRoute
accepts an `oma_` access token or a manage-scope API key.

#### Scenario: Absent token
- **WHEN** no management token resolves (none set, or the environment value is blank)
- **THEN** doctor adds an info row "runtime sources: health only" with the repair command,
  makes no request, and adds no issue or warning
- **TEST** `tests/test_doctor_management_token.py::test_absent_token_is_info_health_only_and_makes_no_request`

#### Scenario: Valid token
- **WHEN** the gateway answers the single GET with 200 and a JSON body
- **THEN** doctor adds an OK row naming the source, and the request carried the resolved
  token with the 5 s timeout
- **TEST** `tests/test_doctor_management_token.py::test_valid_environment_token_is_ok_with_one_bounded_get`,
  `::test_valid_store_token_names_the_credential_store`,
  `::test_store_value_copied_into_the_environment_is_still_the_credential_store`

#### Scenario: Rejected or forbidden token
- **WHEN** the gateway answers 401 or 403
- **THEN** doctor adds a failed row and one issue naming the HTTP status, the source, the
  fail-closed effect on serve and the exact repair command, and the rendered repair
  hint is that command
- **AND** an exported token's issue says that the export overrides the credential store
- **AND** `verdict doctor` and `verdict doctor --json` both exit 1
- **TEST** `tests/test_doctor_management_token.py::test_rejected_or_forbidden_token_is_a_failure_with_source_and_repair`,
  `::test_rejected_environment_token_says_the_export_overrides_the_store`,
  `tests/test_cli_inprocess.py::test_cli_doctor_rejected_store_management_token_fails_both_modes_without_the_value`

#### Scenario: Token cannot be verified
- **WHEN** the GET times out, the transport fails, the gateway answers 404, 429, 5xx or a
  non-200 success, the body is not JSON, or no gateway URL is configured
- **THEN** doctor adds a warning naming only the failure category, never an OK row, and
  makes at most one request
- **TEST** `tests/test_doctor_management_token.py::test_unverifiable_token_is_a_single_attempt_warning_never_ok`,
  `::test_unexpected_success_is_never_ok`, `::test_token_without_a_gateway_is_unverified_without_a_request`

#### Scenario: Token value never rendered
- **WHEN** doctor renders the absent, OK, warning, unreachable, 401 or 403 outcome as
  `--json` or as text (`TerminalUI` rows, warnings, summary and `doctor_presentation`
  repair hints)
- **THEN** the token value does not appear in either form
- **AND** an exported token that carries CR/LF is refused by the real
  `open_no_redirect` before any socket is opened (under `no_gateway_network`), and
  doctor reports only `ValueError`
- **TEST** `tests/test_doctor_management_token.py::test_token_value_never_appears_in_doctor_text_or_json`,
  `::test_header_injection_token_is_refused_by_the_real_opener_before_any_socket`,
  `tests/test_cli_inprocess.py::test_cli_doctor_rejected_store_management_token_fails_both_modes_without_the_value`
