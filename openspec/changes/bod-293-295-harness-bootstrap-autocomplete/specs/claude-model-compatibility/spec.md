# Spec Delta: Claude Code exact-id compatibility report

## Purpose

Expose Claude Code transport and exact model selection limits honestly through a read-only per-id report, without mistaking gateway health or an OpenAI side path for native Claude support.

## ADDED Requirements

### Requirement: Claude discovery and compatibility reporting SHALL be read-only

The compatibility consumer SHALL reuse Claude discover/status semantics without calling enable, disable, certify, probes, refresh, live transport construction, or network. It SHALL report binary/config presence, safe credential-presence booleans, configured mode facts and exact selected ids. It SHALL NOT execute credential helper commands, print secret values/auth.json content, write settings/hooks, or create backup/rollback files. Missing binary/config/credentials SHALL remain explicit, not be assumed ready.

#### Scenario: Binary or credentials are absent
- **WHEN** the Claude binary is absent or required credentials are missing/unknown
- **THEN** compatibility SHALL disclose the missing prerequisite and SHALL not label selected ids executable
- **AND** no configuration or credential writes SHALL occur

#### Scenario: Discovery does not invoke health certification
- **WHEN** a compatibility report is requested
- **THEN** discovery/status SHALL not invoke certify or a health-check/model transport and SHALL remain read-only

### Requirement: Each selected exact id SHALL have native side-path and reasons fields

`verdict.claude-model-compat/v1` SHALL contain generated_at, harness, requested_mode, exact selected_ids, installed, credentials_present, config_digest, config_changed, rows, reasons, apply_available=false and needs_owner containing BOD-102. Every row SHALL contain route_id, provider, projection health_status, checked_at, fresh_until, expires_at, native, side_path and reasons. Unknown evidence SHALL remain null/unknown, not a successful readiness claim. Candidate identities SHALL be validated exactly with no auto models, fuzzy model correction, provider suffix guessing or gateway-inventory verification.

#### Scenario: Multiple selected ids have independent results
- **WHEN** one selected id is freshly VERIFIED and another is stale or unrecognized
- **THEN** both SHALL get independent exact-id rows, unchanged health metadata, native/side-path fields and explicit stale/unsupported-identity reasons

#### Scenario: Gateway catalog is not proof
- **WHEN** an id appears in inventory without validated execution health
- **THEN** the report SHALL keep it unverified/inventory-only as projected and SHALL never call the inventory a verified list

### Requirement: Native Anthropic Messages mode SHALL remain unsupported and NEEDS_OWNER

Each native row SHALL state `transport=anthropic-messages`, `endpoint=/v1/messages`, `compatibility=unsupported`, `disposition=NEEDS_OWNER`, `exact_selection_tested=false`, and `selectable=false`. The report SHALL explain that BOD-102's Anthropic Messages proxy is backlog and that Verdict currently has no `/v1/messages` route. A VERIFIED Claude-looking gateway id or an existing SessionStart gate SHALL NOT establish native transport or selected-model support. Requested mode `native` SHALL only filter/explain the report; it SHALL never write a setting or launch a model.

#### Scenario: Healthy Claude-looking model cannot enable native mode
- **WHEN** `cc/claude-opus-5-5` has current VERIFIED projection evidence and native mode is requested
- **THEN** native SHALL still be unsupported / NEEDS_OWNER with native_messages_proxy_missing and no apply option

#### Scenario: SessionStart gate is not native transport
- **WHEN** the current Claude settings contain the Verdict SessionStart gate
- **THEN** native exact model selection SHALL remain unsupported and SHALL not inherit support from the gate

### Requirement: The OpenAI side path SHALL be labelled separately and unproven for exact Claude selection

The side_path object SHALL use `transport=openai-compatible` and `endpoint=/v1/chat/completions`, with configured, compatibility, exact_selection_tested=false, selectable=false and reasons. Compatibility SHALL be `configured_unproven`, `not_configured`, or `blocked`, reflecting safe local facts. The UI SHALL call it `OpenAI side path; not native Claude Code selection`. Existing OPENAI_BASE_URL and gate configuration SHALL not be presented as a working native picker or tested per-id Claude route. Every selected id SHALL retain selected_id_unproven unless a future separate design introduces trusted end-to-end transport AND exact selection proof; this story SHALL not add such a claim.

#### Scenario: Configured side path is visible but not selectable
- **WHEN** local Claude settings point OpenAI-compatible traffic at Verdict and credentials appear present
- **THEN** side_path SHALL say configured_unproven with exact_selection_tested=false and SHALL not say the selected id works in Claude Code

#### Scenario: Side-path mode selection is a report filter
- **WHEN** `/bootstrap claude mode=openai-side-path` is entered
- **THEN** the system SHALL display the separately labelled side path and SHALL not set a model/env/transport preference

### Requirement: Changed configuration SHALL invalidate actionable compatibility facts

The consumer SHALL compare the config byte digest captured at report start with the digest immediately before presentation. Any mismatch SHALL set config_changed=true, add config_changed reasons and block side-path readiness rather than keep an old actionable result. It SHALL not merge, retry an apply, or overwrite the changed file. Read-only digests SHALL never include credential values in output.

#### Scenario: Owner changes settings during report preparation
- **WHEN** the Claude settings digest changes before report presentation
- **THEN** the final report SHALL disclose config_changed and SHALL not present old side-path configuration as ready

### Requirement: BOD-294 SHALL offer no selected-models apply

There SHALL be no Claude selected-models write API, registry mutation action, y/N apply branch, force flag, or bootstrap enable/rollback in this story. `/bootstrap claude [ids...] [mode=native|openai-side-path]` and `verdict harness claude compat [ids...] [--mode native|openai-side-path] [--json]` SHALL be read-only report surfaces. Omitted ids SHALL be sourced from current exact Prime scope entries joined to the last local projection, with that source explicitly labelled; absent local input SHALL produce an empty diagnostic report, never a live catalog sweep. An attempt to apply/select native models through this surface SHALL return unsupported help with no writes. Existing harness enable/disable/certify behavior and certification claims SHALL remain unchanged. Any future apply requires tested transport plus exact selection, then a separately designed backup/rollback transaction.

#### Scenario: Native apply input is refused
- **WHEN** an owner requests Claude bootstrap apply or a selected-model setting through this report command
- **THEN** the system SHALL refuse with the BOD-102/native-selection limitation and SHALL not create settings, hooks or backup files

#### Scenario: JSON report has honest limits
- **WHEN** `verdict harness claude compat <ids> --json` is run
- **THEN** stdout SHALL contain one sanitized compatibility envelope with apply_available=false and no working native-selected-model claim
