## ADDED Requirements

### Requirement: The ocr pin has one source and one check
`verdict setup`, `verdict doctor` and `scripts/check_dev_readiness.py` MUST read
the ocr release tag and SHA-256 only from `OCR_VERSION` and `OCR_SHA256` in
`.github/workflows/certify-rehearsal.yml`, through `verdict.ocr_release`. A
binary MUST count as the pinned release only when its SHA-256 equals the pin.
Setup MUST hash the downloaded asset before writing and MUST NOT install it on a
mismatch.

#### Scenario: Readiness and doctor share the pin
- **WHEN** the readiness script, setup or doctor needs the ocr pin
- **THEN** each uses `verdict.ocr_release.ocr_pin` and `inspect_ocr`
- **TEST** `tests/test_ocr_release.py::test_readiness_script_shares_the_helper`,
  `::test_pin_is_read_from_the_real_rehearsal_workflow`

#### Scenario: A digest mismatch never installs
- **WHEN** the downloaded asset does not hash to `OCR_SHA256`
- **THEN** nothing is written and the existing binary is unchanged
- **TEST** `tests/test_ocr_release.py::test_install_never_writes_on_a_digest_mismatch`,
  `tests/test_setup_host.py::test_an_ocr_digest_mismatch_never_installs`

### Requirement: The serve unit is installed with a private env file
The serve installer MUST render `verdict-serve.service` bound to
`127.0.0.1:8000` with `WorkingDirectory=%h` and
`EnvironmentFile=%h/.config/verdict/serve.env`. The env file MUST be mode 0600
and hold `LLMGATE_AUTH_TOKEN` and `VERDICT_RECEIPTS_DB`. An existing token MUST
be reused; otherwise one MUST be generated with `secrets.token_urlsafe` and
stored in the credential store. Dry-run MUST NOT write, generate a token or run
systemctl. Token values MUST NOT appear in any returned payload or error.

#### Scenario: Generated token, private file
- **WHEN** no token exists and the installer applies
- **THEN** a generated token is in the 0600 env file and the credential store, and not in the result
- **TEST** `tests/test_serve_service.py::test_apply_generates_a_token_stores_it_and_writes_a_private_env_file`

#### Scenario: Existing token reused
- **WHEN** a token is exported, stored, or already in the env file
- **THEN** that token is written and no new one is generated
- **TEST** `tests/test_serve_service.py::test_apply_reuses_an_existing_token`

#### Scenario: Dry-run is mutation-free
- **WHEN** the installer runs with `dry_run`
- **THEN** no file is written and systemctl is not called
- **TEST** `tests/test_serve_service.py::test_dry_run_writes_nothing_and_generates_no_token`

### Requirement: Setup plans and applies host prerequisites behind the consent gate
An all-scope `verdict setup` plan MUST contain one consent-required action per
unsatisfied host prerequisite (`host.ocr`, `host.metadata`,
`host.acceptance-timer`, `host.identify-timers`, `host.serve`,
`host.prime-registry`), and planning MUST NOT mutate. APPLY MUST use the
existing consent policy and the existing installers. Metadata refresh MUST run
only when the store is missing. A Prime registry MUST be created only when
`models.json` is missing, with `apiKey` set to the env var name
`OMNIROUTE_API_KEY`, followed by `sync-models` and `enable`. The classic
`--dry-run`/`--plan` preview MUST NOT probe the host, and scoped plans MUST NOT
include host actions.

#### Scenario: Planning is mutation-free
- **WHEN** a host lacks every prerequisite and setup plans
- **THEN** six host actions are listed in apply order and no file, unit or download changes
- **TEST** `tests/test_setup_host.py::test_plan_proposes_each_missing_prerequisite_in_order_without_mutating`,
  `::test_bootstrap_plan_mode_includes_host_actions_and_stays_mutation_free`,
  `::test_recommended_setup_previews_host_actions_without_mutation`,
  `::test_classic_dry_run_and_plan_never_probe_the_host`

#### Scenario: Consented APPLY installs through the existing installers
- **WHEN** every host action is authorized
- **THEN** ocr, the metadata store and timer, the acceptance and identify timers, the serve unit and the Prime registry are installed, and a second plan is empty
- **TEST** `tests/test_setup_host.py::test_apply_with_consent_installs_every_prerequisite_through_the_existing_installers`

#### Scenario: No consent, no mutation
- **WHEN** a non-interactive APPLY names no allowlist
- **THEN** consent is blocked and nothing changes; an allowlist authorizes only the named prerequisite, and host ids never authorize a catalog install
- **TEST** `tests/test_setup_host.py::test_non_interactive_apply_without_an_allowlist_mutates_nothing`,
  `::test_an_allowlist_authorizes_only_the_named_prerequisite`,
  `::test_a_host_only_allowlist_never_authorizes_catalog_installs`

#### Scenario: Existing state is respected
- **WHEN** the metadata store or `models.json` already exists
- **THEN** no refresh runs, and the registry is not planned or touched
- **TEST** `tests/test_setup_host.py::test_existing_metadata_store_is_not_refreshed_again`,
  `::test_an_existing_prime_registry_is_left_alone`

#### Scenario: Drift is re-applied and rollback is honest
- **WHEN** an applied prerequisite drifts, or setup rolls back host actions
- **THEN** it is planned and applied again, and rollback reports manual undo with the uninstall command
- **TEST** `tests/test_setup_host.py::test_drifted_prerequisites_are_planned_and_applied_again`,
  `::test_rollback_names_manual_undo_for_host_actions`

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
- **THEN** a missing timer names `cp <checkout>/deploy/systemd/verdict-prime-sync.{service,timer} ~/.config/systemd/user/ && systemctl --user daemon-reload && systemctl --user enable --now verdict-prime-sync.timer` (the steps in `docs/guides/harnesses.md`; setup has no prime-sync installer), an inactive one `systemctl --user enable --now verdict-prime-sync.timer`, and a failed one `verdict harness prime sync-models`
- **TEST** `tests/test_doctor_host_prerequisites.py::test_a_missing_prime_sync_timer_names_the_documented_install`,
  `::test_identify_and_prime_sync_timer_rows_name_their_repair`

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
