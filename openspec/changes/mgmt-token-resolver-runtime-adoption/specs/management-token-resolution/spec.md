## ADDED Requirements

### Requirement: Subagent selection and the worker runtime resolve the management token like serve
Subagent model selection and the worker runtime MUST obtain the OmniRoute management
token from the same resolver `verdict serve` uses, and MUST NOT read
`OMNIROUTE_MANAGEMENT_TOKEN` from the environment directly. A non-blank environment
value MUST win over the credential store. A blank environment value MUST be ignored.
The same-named credential-store field MUST be used when the environment has none, even
when the process did not start through the CLI entry. A credential store readable by
group or others MUST be ignored and MUST NOT stop the path. Each path MUST enable the
`connections` runtime source only when a token resolves, and MUST read `health` only
otherwise.

#### Scenario: Store-only token is used
- **WHEN** only the credential store holds a management token
- **THEN** the subagent selector's and the worker runtime's transports each receive the
  stored token and read `connections`
- **TEST** `tests/test_runtime_management_token.py::test_store_only_token_is_used`

#### Scenario: Exported token wins
- **WHEN** both the environment and the credential store hold a management token
- **THEN** each transport receives the environment token and reads `connections`
- **TEST** `tests/test_runtime_management_token.py::test_exported_token_wins_over_the_store`

#### Scenario: Blank exported value falls back to the store
- **WHEN** the environment exports a blank management token and the store holds one
- **THEN** each transport receives the stored token and reads `connections`
- **TEST** `tests/test_runtime_management_token.py::test_blank_exported_token_falls_back_to_the_store`

#### Scenario: Blank exported value with no stored token
- **WHEN** the environment exports a blank management token and the store holds none
- **THEN** each transport receives no token and reads `health` only
- **TEST** `tests/test_runtime_management_token.py::test_blank_exported_token_without_a_store_reads_health_only`

#### Scenario: No token anywhere keeps today's behaviour
- **WHEN** neither the environment nor the store holds a management token
- **THEN** each transport receives no token and reads `health` only
- **TEST** `tests/test_runtime_management_token.py::test_no_token_keeps_health_only`

#### Scenario: Insecure store is ignored
- **WHEN** the only stored token sits in a credential store readable by group or others
- **THEN** each transport receives no token, reads `health` only, and the path does not
  fail on the store
- **TEST** `tests/test_runtime_management_token.py::test_group_readable_store_is_ignored_not_fatal`

### Requirement: Credential listings report recorded provenance
The doctor "Credentials" list and `verdict credentials list` MUST report `store` for
an environment value that the credential store's environment load copied in, when the
process has not changed that value since. They MUST report `env` for every other
non-blank environment value, including one exported before the load that equals the
stored value. Provenance MUST come from the load's injection record and MUST NOT be
derived from comparing the environment value with the stored value. The listing MUST
NOT contain the value.

#### Scenario: Value copied from the store by the CLI entry
- **WHEN** the CLI entry copied a stored credential into the environment, and doctor or
  `verdict credentials list` lists it
- **THEN** its source is `store`, and the output does not contain the value
- **TEST** `tests/test_credentials_store.py::test_get_credential_source_names_a_value_load_into_env_copied_as_the_store`,
  `tests/test_cli_inprocess.py::test_cli_doctor_rejected_store_management_token_fails_both_modes_without_the_value`

#### Scenario: Value changed or exported by the process
- **WHEN** the process overwrites the copied value, or exported a value before the load
  (even one equal to the stored value)
- **THEN** its source is `env`
- **TEST** `tests/test_credentials_store.py::test_get_credential_source_names_a_value_load_into_env_copied_as_the_store`,
  `tests/test_credentials_store.py::test_get_credential_source_from_env`
