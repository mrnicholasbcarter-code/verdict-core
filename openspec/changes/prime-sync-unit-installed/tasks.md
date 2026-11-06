# Tasks

Owned files: `verdict/prime_sync_service.py`, `verdict/doctor_diagnostics.py`
(`_prime_sync_timer_repair` only), `verdict/actions/extra.py`,
`verdict/actions/registry.py`, `verdict/commands/parsers_harness.py`,
`verdict/commands/dispatch.py`, `deploy/systemd/verdict-prime-sync.*` (removed),
`docs/guides/harnesses.md`, `docs/CLI_REFERENCE.md` (prime-sync lines only),
`CHANGELOG.md`; `tests/test_prime_sync_service.py`,
`tests/test_doctor_host_prerequisites.py` (the prime-sync test only); this change.

## 1. Tests first
- [x] 1.1 `test_missing_timer_repair_outside_a_checkout_names_the_package_installer`
  and `test_installed_wheel_repair_and_installer_work_without_a_checkout` (the
  built wheel extracted into a bare `site-packages`). Red first: the repair was
  `cp <site-packages>/deploy/systemd/verdict-prime-sync.{service,timer} ...`.
- [x] 1.2 `test_dry_run_renders_the_units_from_this_interpreter_and_package`,
  `test_install_repeat_replace_and_uninstall_are_bounded` and
  `test_cli_dry_run_writes_nothing_and_the_action_is_a_mutation`. Red first:
  `No module named 'verdict.prime_sync_service'`.
- [x] 1.3 `test_a_missing_prime_sync_timer_names_the_documented_install` expects
  `verdict harness prime install-service`. Red first: exact-text mismatch.

## 2. Render the units from code
- [x] 2.1 `verdict/prime_sync_service.py` `manage_service()`, mirroring
  `verdict/acceptance_service.py` `manage_service()`.
- [x] 2.2 `verdict harness prime install-service` and the mutation action
  `harness.prime.install-service`.
- [x] 2.3 The doctor names the command for a missing timer and no longer imports
  `package_root`.
- [x] 2.4 Remove `deploy/systemd/verdict-prime-sync.*`. Update
  `docs/guides/harnesses.md`, `docs/CLI_REFERENCE.md` and `CHANGELOG.md`.

## 3. Live acceptance (pending; needs merged main)
AGENTS.md "Definition of done" part 1 needs an installed build of merged main.
Nothing below has run.
- [ ] 3.1 Install merged main non-editable on the operator host. Run
  `verdict harness prime install-service --dry-run`. Its plan must name the
  venv interpreter and `site-packages` as `WorkingDirectory`. Record the date,
  SHA, command and raw output.
- [ ] 3.2 Run `verdict harness prime install-service`. Then `verdict doctor`
  must show `Prime-sync timer`, `Prime-sync code path` and
  `Runtime prime-sync` as `ok`. Record the raw output and the rollback path.
