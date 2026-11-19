## ADDED Requirements

### Requirement: Doctor gateway probes tolerate a slow but healthy gateway
`verdict doctor` MUST wait `VERDICT_DOCTOR_GATEWAY_TIMEOUT_S` seconds (default 10) for the
authenticated `GET /models` readiness probe and `VERDICT_DOCTOR_HEALTH_TIMEOUT_S` seconds
(default 3) for `GET /api/health`. Each probe MUST be retried exactly once after a timeout
and MUST NOT be retried after any HTTP answer. A probe MUST report healthy only on a 2xx
(`/models`) or 200 (`/api/health`).

#### Scenario: Slow first catalog answer
- **WHEN** the gateway answers `/models` with 200 after 3 s
- **THEN** the doctor probe reports the gateway reachable
- **TEST** `tests/test_doctor_gateway_probe_timeout.py::test_models_answering_after_3s_is_healthy`

#### Scenario: Gateway never answers
- **WHEN** the gateway accepts the connection but never answers
- **THEN** the probe is sent exactly twice and fails with a message naming the timeout and
  the setting that raises it
- **TEST** `tests/test_doctor_gateway_probe_timeout.py::test_models_never_answering_fails_naming_timeout_and_setting`,
  `::test_models_timeout_is_retried_exactly_once`,
  `::test_health_timeout_retried_once_then_names_setting`

#### Scenario: Gateway answers with an error
- **WHEN** the gateway answers 401, 403 or 5xx
- **THEN** the probe is sent once and is not healthy
- **TEST** `tests/test_doctor_gateway_probe_timeout.py::test_models_answer_is_never_retried_or_healthy`,
  `::test_health_error_status_is_not_retried_or_healthy`

#### Scenario: Timeout setting
- **WHEN** `VERDICT_DOCTOR_GATEWAY_TIMEOUT_S` is a finite number in (0, 120]
- **THEN** the probe uses it; any other value is ignored and the default applies
- **TEST** `tests/test_doctor_gateway_probe_timeout.py::test_models_timeout_setting_changes_the_timeout`,
  `::test_invalid_timeout_setting_falls_back_to_default`
