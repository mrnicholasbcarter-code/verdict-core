# Spec Delta

## Purpose

Defines installed-runtime provenance in `verdict doctor` and a plan-and-rollback contract for every service installer.

## ADDED Requirements

### Requirement: Doctor reports one row per loaded runtime unit

`installed_runtime_rows()` SHALL produce one row for each loaded systemd unit among prove-at-rest, census, acceptance, prime-sync and metadata-refresh, showing: the package the unit's `ExecStart` interpreter imports from its working directory; the package's recorded build; the gateway origin, from the unit's `OMNIROUTE_BASE_URL` Environment value, else "default"; and the effective state scope (`VERDICT_HOME`).

#### Scenario: Doctor rows
- Proof: `tests/test_doctor_installed_state.py::test_installed_runtime_provenance_and_shared_coordinator_cutover`

### Requirement: An unrecorded build is never reported OK

A row SHALL FAIL when: the interpreter's own install is not the reviewed build; the imported package is not the reviewed build, or has the same commit with different content; the state scope differs; or `VERDICT_HEALTH_CACHE` points at a different cache. A package whose build cannot be determined (no `direct_url.json`, no `verdict-build.json` stamp, no readable git HEAD) SHALL be reported as an unrecorded build and SHALL FAIL even if its install path matches the reviewed path.

#### Scenario: Unrecorded build never passes
- Proof: `tests/test_doctor_installed_state.py::test_installed_runtime_unrecorded_build_is_never_ok_and_gateway_is_redacted`

### Requirement: The reported gateway is reduced to scheme, host and port only

Any gateway origin value shown in a doctor row SHALL be reduced to `scheme://host:port`, plus a literal `/...` marker when the URL has a path. Query strings, userinfo, fragments, and path segments carrying tokens SHALL NOT appear in the detail text.

#### Scenario: Gateway redaction
- **THEN** a configured URL of `http://user:secret@host:1234/tok-secret/v1?key=secret` is shown as `http://host:1234/...`, with no secret substring present.

### Requirement: A qualification-owner row warns on divergent qualifier units

When two qualifier units (prove-at-rest and census) are loaded, the "Qualification owner" row SHALL warn when either is off the reviewed build or state scope, and SHALL be OK otherwise.

#### Scenario: Qualification owner
- Proof: read-only host run in `bod414-s11-result.md`: WARNING when prove-at-rest and census diverge.

### Requirement: Every installer returns a plan and writes a rollback before mutating units

Each of the prove-at-rest, acceptance, and census installers SHALL return a `plan` (per-unit `create`/`replace`/`keep`, or `remove`/`absent` for uninstall) and a `rollback` path. `save_rollback` SHALL copy every unit file that will be replaced or removed into `$VERDICT_HOME/service-rollback/<UTC>/` before any unit file is written. A dry run SHALL return the plan and write nothing.

#### Scenario: Dry run preserves existing config
- Proof: `tests/test_setup_preservation_ui.py::test_setup_dry_run_preserves_existing_config`

#### Scenario: Plan and rollback shown
- **THEN** the warm-cache setup step prints the unit plan and the rollback path after a confirmed install; declining the prompt mutates nothing.
