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
a name whose current environment value equals the value that the credential store's
environment load copied in for that name in its latest load. They MUST report `env`
for every other non-blank environment value. That includes a value exported before the
load that equals the stored value, because the load does not copy a name that is
already set. Provenance MUST come from the load's injection record, which holds a
digest of each copied value and never the value. It MUST NOT be derived from comparing
the environment value with the credential store's contents. The listing MUST NOT
contain the value. Limitation: the record cannot observe changes between the load and
the listing. A value that the process changes away and then sets back to the exact
copied string in the same process cannot be told apart from the copy, and it is
reported as `store`.

#### Scenario: Value copied from the store by the CLI entry
- **WHEN** the CLI entry copied a stored credential into the environment, and doctor or
  `verdict credentials list` lists it
- **THEN** its source is `store`, in `--json` and text output, and the output does not
  contain the value
- **TEST** `tests/test_credentials_store.py::test_get_credential_source_names_a_value_load_into_env_copied_as_the_store`,
  `tests/test_cli_inprocess.py::test_cli_credentials_list_names_store_copies_and_exports_without_values`,
  `tests/test_cli_inprocess.py::test_cli_doctor_rejected_store_management_token_fails_both_modes_without_the_value`

#### Scenario: Value that differs from the copy, or was exported before the load
- **WHEN** the current value differs from the value the latest load copied, or the name
  was exported before the load (even with a value equal to the stored one)
- **THEN** its source is `env`, and the output does not contain the value
- **TEST** `tests/test_credentials_store.py::test_get_credential_source_names_a_value_load_into_env_copied_as_the_store`,
  `tests/test_cli_inprocess.py::test_cli_credentials_list_names_store_copies_and_exports_without_values`,
  `tests/test_credentials_store.py::test_get_credential_source_from_env`

#### Scenario: A later load that does not copy the name drops the earlier copy
- **WHEN** one load copied a stored value, the process then exports that name itself
  with the same string, and a later load runs and does not copy the name
- **THEN** the injection record no longer holds the name, and its source is `env`
- **TEST** `tests/test_credentials_store.py::test_get_credential_source_drops_a_copy_the_next_load_does_not_repeat`

#### Scenario: Value changed away and set back in process is indistinguishable (limitation)
- **WHEN** the process changes the copied value and later sets it back to the exact
  copied string
- **THEN** its source is reported as `store`. This is a documented limitation, not a
  guarantee.
- **TEST** `tests/test_credentials_store.py::test_limitation_value_changed_away_and_back_in_process_lists_as_store`
