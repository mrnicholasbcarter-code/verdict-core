## Spec Delta

## MODIFIED Requirements

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
include host actions. The optional-tools loop MUST NOT offer ai-memory; it covers
only `context-mode` and `open-code-review` (ADR-050, D34).

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

#### Scenario: ai-memory is not offered in the setup wizard
- **WHEN** `verdict setup` runs the optional-tools loop
- **THEN** no row for `ai-memory` or `ai-memory (BOD-336)` is printed
- **TEST** `tests/test_visual_system.py` (plain-output hash guard on the setup screen)
  — the hash passes after removing the ai-memory offer
