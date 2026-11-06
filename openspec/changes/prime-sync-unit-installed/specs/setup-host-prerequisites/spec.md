## MODIFIED Requirements

### Requirement: Doctor reports each host prerequisite with its exact repair
`verdict doctor` MUST report ocr missing, a different version or a digest
mismatch; the serve unit missing or inactive and `/health` unreachable or its probe raising; the
Prime registry missing, without `providers.omniroute` or without models; a
failing `verdict-prime-sync.service`; and the acceptance, metadata and identify
timers. Every failed row MUST be an issue and MUST end with `repair: <command>`,
and the text renderer MUST print that exact command.

#### Scenario: Host rows name their repair
- **WHEN** a host prerequisite is missing or broken
- **THEN** its row fails and `repair_command` returns the row's exact command
- **TEST** `tests/test_doctor_host_prerequisites.py::test_missing_ocr_fails_with_the_setup_repair`,
  `::test_wrong_ocr_version_and_digest_mismatch_fail`,
  `::test_missing_serve_unit_fails_without_probing_health`,
  `::test_inactive_serve_unit_names_enable_now`,
  `::test_unreachable_health_names_restart`,
  `::test_prime_registry_rows_name_each_repair`,
  `::test_failing_prime_sync_service_names_the_repair`

#### Scenario: Timer rows name their installer
- **WHEN** the acceptance, metadata or identify timer is missing or failing
- **THEN** the row names `verdict acceptance install-service`, `verdict metadata install-service` or `verdict metadata refresh`, or `verdict identify install-service`
- **TEST** `tests/test_doctor_host_prerequisites.py::test_acceptance_timer_row_names_its_installer`,
  `::test_metadata_rows_name_the_exact_repair`,
  `::test_identify_and_prime_sync_timer_rows_name_their_repair`

#### Scenario: The prime-sync timer names the documented install
- **WHEN** `verdict-prime-sync.timer` is missing, inactive or failed
- **THEN** a missing timer names `verdict harness prime install-service`, an inactive one `systemctl --user enable --now verdict-prime-sync.timer`, and a failed one `verdict harness prime sync-models`; no repair names a path under the package's parent directory
- **TEST** `tests/test_doctor_host_prerequisites.py::test_a_missing_prime_sync_timer_names_the_documented_install`,
  `::test_identify_and_prime_sync_timer_rows_name_their_repair`,
  `tests/test_prime_sync_service.py::test_missing_timer_repair_outside_a_checkout_names_the_package_installer`

#### Scenario: Unknown serve health is never healthy
- **WHEN** `verdict-serve.service` is active but `/health` does not answer, or the probe raises
- **THEN** doctor fails the health row with `systemctl --user restart verdict-serve.service`, setup plans `host.serve`, and APPLY restarts the unit and fails with its logs if it stays unhealthy
- **TEST** `tests/test_doctor_host_prerequisites.py::test_a_health_probe_exception_is_a_failed_row`,
  `tests/test_setup_host.py::test_active_but_unhealthy_serve_is_planned_and_restarted`,
  `::test_a_health_probe_exception_is_never_healthy`,
  `::test_serve_still_unhealthy_after_restart_fails_with_its_logs`

#### Scenario: Failed rows fail doctor in both modes
- **WHEN** a host-prerequisite row fails
- **THEN** `verdict doctor --json` and text mode exit 1, and text mode prints `Repair: <command>`
- **TEST** `tests/test_cli_inprocess.py::test_cli_doctor_failed_host_prerequisite_names_its_repair_both_modes`

## ADDED Requirements

### Requirement: The prime-sync timer installs from the installed package
`verdict harness prime install-service` MUST render `verdict-prime-sync.service`
and `verdict-prime-sync.timer` from code, with no template file and no checkout.
The service MUST run the installing interpreter with
`-m verdict harness prime sync-models`. Its `WorkingDirectory` MUST be the
directory that holds the imported `verdict` package. The timer MUST fire 2
minutes after boot and then every 15 minutes, and MUST be persistent.
`--dry-run` MUST write nothing and run no `systemctl`. Install MUST save a unit
it replaces under the Verdict rollback directory, run `daemon-reload` only when
a unit changes, run `enable --now verdict-prime-sync.timer`, and restart the
timer when the timer unit was replaced. When either unit file exists,
`--uninstall` MUST disable both units and remove them.

#### Scenario: An installed wheel repairs and installs with no checkout
- **WHEN** `verdict` is imported from the built wheel extracted into a bare `site-packages`
- **THEN** the doctor repair for a missing timer is `verdict harness prime install-service`; `install-service --dry-run` renders a service whose `WorkingDirectory` is that `site-packages` and whose `ExecStart` is the interpreter running `-m verdict harness prime sync-models`, and writes nothing; install writes that service, reloads and enables the timer; `--uninstall` removes both units
- **TEST** `tests/test_prime_sync_service.py::test_installed_wheel_repair_and_installer_work_without_a_checkout`

#### Scenario: Install is bounded and reversible
- **WHEN** the units are previewed, installed, installed again, replaced and uninstalled
- **THEN** the preview writes nothing; the first install writes both units, reloads and enables the timer; a repeat only enables; a differing unit is replaced and its old copy saved; a replaced timer is also restarted; uninstall disables and removes both
- **TEST** `tests/test_prime_sync_service.py::test_dry_run_renders_the_units_from_this_interpreter_and_package`,
  `::test_install_repeat_replace_and_uninstall_are_bounded`,
  `::test_cli_dry_run_writes_nothing_and_the_action_is_a_mutation`
