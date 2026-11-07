# Spec Delta

## ADDED Requirements

### Requirement: Reference docs follow the installed version
Verdict SHALL sync a tool's reference docs at the tag of the release its systemd unit runs, and SHALL refuse to sync when that version cannot be read.

#### Scenario: Upgrade moves the tag
- **WHEN** the installed OmniRoute package changes from v3.8.53 to v3.9.0
- **THEN** the next sync reads tag v3.9.0 and deletes pages that the new files no longer produce

#### Scenario: Unknown version is not guessed
- **WHEN** the unit's ExecStart does not name a node_modules install of the package
- **THEN** the sync fails and status reports the installed version as unknown

### Requirement: Incremental, cited pages
Verdict SHALL write each markdown section as one page of at most 3 KB with a citation, and SHALL rewrite a file's pages only when its blob sha or render format changes.

#### Scenario: Re-run without upstream change
- **WHEN** the sync runs twice at the same tag
- **THEN** the second run writes 0 pages and deletes 0 pages

### Requirement: Reference projects stay out of operator recall
Reference pages SHALL live in their own ai-memory project and SHALL NOT be added to operator-rule scopes.

#### Scenario: Scoped operator query with reference projects present
- **WHEN** the operator golden eval runs against project `operator` while `ref-omniroute` exists
- **THEN** no top-5 hit comes from `ref-omniroute`

#### Scenario: Reference pages do not lower operator recall
- **WHEN** the same eval runs on a store with the operator pages alone and on one with the operator and reference pages
- **THEN** hit@5 with the reference pages is not lower than without them

### Requirement: Installed and checked
`verdict setup` SHALL install the hourly reference-docs timer as a consented host prerequisite, and `verdict doctor` SHALL report the timer and each reference project's tag, installed tag and last sync, failing on a missing, stale, behind or unknown state.

#### Scenario: Synced tag behind the installed tag
- **WHEN** the manifest tag is v3.8.51 and v3.8.53 is installed
- **THEN** doctor reports the reference project row as failed with `synced v3.8.51 but v3.8.53 is installed`
