# Spec Delta: setup-host-prerequisites

## ADDED Requirements

### Requirement: Prime Agent is an optional host prerequisite

On a host without the Prime Agent binary (`prime-agent` or `prime` on PATH),
`verdict setup` MUST NOT plan or apply `host.prime-registry`, and
`verdict doctor` MUST report the Prime registry row and a missing
`verdict-prime-sync.timer` row as `not-installed`, naming the optional harness
and `--executor prime-headless`. A `not-installed` row MUST NOT be an issue and
MUST NOT be `ok`. With the binary present, setup and doctor behave as before.

#### Scenario: No Prime binary, no Prime setup

- **WHEN** `verdict setup --recommended` plans and applies on a host without Prime
- **THEN** no Prime registry action is planned or applied and no Prime file or unit changes
- **TEST** `tests/test_setup_host.py::test_recommended_setup_without_prime_never_plans_or_installs_prime`

#### Scenario: No Prime binary, doctor stays clean

- **WHEN** doctor runs on a host without Prime
- **THEN** the Prime rows are `not-installed`, are not issues, and doctor does not exit 1 for them; with Prime present a missing timer still fails with its repair
- **TEST** `tests/test_doctor_host_prerequisites.py::test_prime_rows_without_prime_are_not_installed_never_ok_or_failed`,
  `::test_missing_prime_sync_timer_without_prime_is_not_installed`,
  `tests/test_cli_inprocess.py::test_cli_doctor_optional_prime_not_installed_is_not_an_issue`

#### Scenario: Damaged Prime config never crashes doctor

- **WHEN** the Prime binary is present and `models.json` is unreadable
- **THEN** doctor still returns rows: the Prime registry row fails, and the Prime harness row is `unknown` (never ok, not a second blocking row) and points at the registry repair
- **TEST** `tests/test_doctor_host_prerequisites.py::test_damaged_prime_config_never_crashes_doctor_and_harness_is_not_ok`,
  `::test_damaged_prime_config_never_crashes_setup_planning`
