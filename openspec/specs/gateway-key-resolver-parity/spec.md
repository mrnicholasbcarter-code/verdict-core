## Purpose

Keep direct gateway execution and credential live checks aligned with the shared gateway key resolver.

## Requirements

### Requirement: Direct-gateway executor resolves the gateway key through the shared resolver
`DirectGatewayExecutor` SHALL take its gateway key from an explicit `api_key` argument when
one is given, otherwise from `credentials_store.resolve_api_key`, otherwise from
`OPENAI_API_KEY`. It SHALL NOT keep a separate environment-only precedence chain.

#### Scenario: Key is only in the owner-only key file
- **WHEN** no gateway key is exported and the owner-only `omniroute-api-key` file holds a key
- **THEN** the executor sends that key as its bearer credential

#### Scenario: Exported key wins over the key file
- **WHEN** `VERDICT_OMNIROUTE_API_KEY` is exported and the key file holds a different key
- **THEN** the executor sends the exported key

#### Scenario: OPENAI_API_KEY stays the last resort
- **WHEN** the shared resolver finds no gateway key and `OPENAI_API_KEY` is exported
- **THEN** the executor sends `OPENAI_API_KEY`

### Requirement: Credential live check resolves a gateway key through the shared resolver
`credentials.test` SHALL fall through to `credentials_store.resolve_api_key` when the named
credential is a gateway key name and is unset in the environment and the credential store.
It SHALL NOT report `missing` for a gateway key that `credentials.list` reports as present.
The key value SHALL NOT appear in the action result.

#### Scenario: Key is only in the owner-only key file
- **WHEN** `credentials.test` runs for `OMNIROUTE_API_KEY` with the key only in the key file
- **THEN** the live check runs with that key and its result is reported

#### Scenario: Other credentials are unchanged
- **WHEN** `credentials.test` runs for a credential that is not a gateway key name and is unset
- **THEN** it reports `missing` without consulting the gateway key resolver
