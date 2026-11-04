## ADDED Requirements

### Requirement: Doctor verifies the resolved gateway key is DB-backed
`verdict doctor` MUST call `GET /api/v1/me/status` on the configured gateway origin with
the key from the single credential resolver, and MUST report OK only when the gateway
answers 200 with key metadata (`apiKey.id`).

#### Scenario: DB-backed key
- **WHEN** `me/status` answers 200 with `apiKey.id`
- **THEN** doctor adds an OK "Gateway key" row with the key name and no issue
- **TEST** `tests/test_doctor_gateway_key.py::test_db_backed_key_is_ok`

#### Scenario: Gateway env key or refused key
- **WHEN** `me/status` answers 401 or 403, any other non-200 success status, or 200
  without key metadata
- **THEN** doctor reports an issue naming `OMNIROUTE_API_KEY` in the gateway environment
  as the likely cause and the remedy
- **TEST** `tests/test_doctor_gateway_key.py::test_env_key_or_refused_key_is_an_issue`,
  `::test_status_without_key_metadata_is_an_issue`, `::test_non_200_success_status_is_never_ok`

#### Scenario: Key cannot be verified
- **WHEN** the request times out or the transport fails
- **THEN** doctor adds a warning and never an OK row
- **TEST** `tests/test_doctor_gateway_key.py::test_unverified_key_is_a_warning_never_ok`

#### Scenario: Not applicable
- **WHEN** no key is resolved, or the gateway answers 404
- **THEN** doctor makes no request (no key) or adds nothing (404)
- **TEST** `tests/test_doctor_gateway_key.py::test_no_resolved_key_makes_no_request`,
  `::test_gateway_without_per_key_status_adds_nothing`
