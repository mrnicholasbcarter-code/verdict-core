## ADDED Requirements

### Requirement: Catalog discovery resolves a declared gateway key through the shared resolver
When a provider declares `VERDICT_OMNIROUTE_API_KEY` or `OMNIROUTE_API_KEY` as its
`api_key_env` and has no explicit key, catalog discovery MUST authenticate `GET /models`
with the key that `credentials_store.resolve_gateway_key` returns. That resolver's order is
environment, `credentials.env`, the owner-only `omniroute-api-key` file, then Prime
`auth.json`. Discovery MUST NOT send the gateway key to any other provider.

#### Scenario: Key held only in the key file
- **WHEN** `OMNIROUTE_API_KEY` is unset, `providers.omniroute` declares
  `api_key_env: OMNIROUTE_API_KEY`, and the owner-only key file holds the key
- **THEN** the CLI route gate's discovery request carries `Authorization: Bearer <file key>`
  and returns the gateway catalog
- **TEST** `tests/test_bod416_route_key_file_fallback.py::test_route_catalog_discovery_sends_key_file_key_when_env_unset`

#### Scenario: Same route decision as an exported key
- **WHEN** no admit snapshot decides the route and the key is held only in the key file
- **THEN** `verdict route` picks the same catalog model as it does with the key exported,
  not the primary-model fallback
- **TEST** `tests/test_bod416_route_key_file_fallback.py::test_cli_route_decision_is_the_same_for_key_file_and_exported_key`

#### Scenario: An exported key still wins
- **WHEN** `OMNIROUTE_API_KEY` is exported (even a stale value) and the key file also exists
- **THEN** discovery sends the exported value
- **TEST** `tests/test_bod416_route_key_file_fallback.py::test_route_catalog_discovery_stale_env_key_still_wins_over_key_file`

#### Scenario: Other providers never get the gateway key
- **WHEN** a provider declares no `api_key_env`, or a non-gateway env name that is unset
- **THEN** its discovery request carries no Authorization header, even when the key file exists
- **TEST** `tests/test_bod416_route_key_file_fallback.py::test_catalog_discovery_never_sends_the_gateway_key_to_other_providers`
