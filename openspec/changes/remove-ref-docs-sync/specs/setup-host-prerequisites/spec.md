## Spec Delta

## REMOVED Requirements

### Requirement: Installed and checked (ref-docs timer)

The `host.ref-docs-timer` provider (`verdict-ref-docs.timer`) is removed. `verdict setup`
no longer plans, applies, or checks the hourly reference-docs sync timer.
`verdict doctor` no longer reports a reference-docs timer row or per-project sync rows.

**Reason:** The reference-docs sync wrote to ai-memory, which the operator replaced with
Hindsight on 2026-11-21 (ADR-050, D33). The timer drift caused the 15:00Z daily
acceptance run to go red. Removing the timer and its doctor rows eliminates the
ai-memory dependency from the setup and doctor paths.

**Migration:** Run `verdict memory ref-docs install-service --uninstall` before
reinstalling from merged main, then `systemctl --user daemon-reload`. The old timer
units are cleaned up; `verdict doctor` is green without them.

#### Scenario: Ref-docs timer is not planned

- **WHEN** `verdict setup --plan` runs
- **THEN** `host.ref-docs-timer` does not appear in the plan output
- **TEST** `tests/test_setup_host.py::test_ref_docs_timer_is_planned_until_installed_and_active`
  is deleted; `HOST_IDS` no longer contains `host.ref-docs-timer`

#### Scenario: Doctor does not check the ref-docs timer

- **WHEN** `installed_service_rows()` runs
- **THEN** no row with label `Reference-docs timer` or `Reference docs` is returned
- **TEST** `tests/test_doctor_host_prerequisites.py::test_ref_docs_timer_row_names_its_repair`,
  `::test_ref_docs_rows_show_each_project_tag_and_last_sync`,
  `::test_unknown_ref_docs_state_is_never_healthy` are deleted
