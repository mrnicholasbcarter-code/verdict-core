# Spec Delta: Partial salvage

## Purpose

Preserve a failed attempt's owned-file partial work and restore it into the
same node's next attempt on the production DAG retry path, with bounded
content-free handoff evidence and strict rejection boundaries.

## ADDED Requirements

### Requirement: The production runtime SHALL salvage owned partial work before a failed worktree is removed

`DagRuntime` SHALL capture owned-file changes from a failed attempt's worktree
before that worktree is removed, and SHALL restore them into the SAME node's
next attempt worktree after it is created. The salvage mechanism SHALL have no
production caller other than the runtime retry path and SHALL NOT change which
node, route or attempt runs next.

#### Scenario: Failed attempt leaves owned partial work

- **GIVEN** a node attempt writes changes to files inside its ownership contract
  and then fails verification
- **WHEN** the runtime retries the same node
- **THEN** the next attempt's worktree contains the salvaged owned-file changes
- **AND** acceptance, verification, admission and route selection behave as
  they did without salvage.

#### Scenario: Attempt fails with no owned changes

- **WHEN** a failed attempt modified no owned files
- **THEN** nothing is staged and the retry worktree is identical to a retry
  without salvage.

### Requirement: Handoff evidence SHALL be bounded and content-free

Salvage SHALL record handoff evidence in the existing append-only event log and
node history: relative paths, byte counts, content digests and skipped path
names only, bounded in count and size. File content SHALL never appear in
events, history or the receipt.

#### Scenario: Salvage occurs on a retry

- **WHEN** owned partial work is captured and restored
- **THEN** a bounded `salvage` event and history record name the paths,
  counts and digests
- **AND** no event, history record or receipt field contains file content.

### Requirement: Salvage SHALL reject unsafe or out-of-contract content

Capture SHALL skip paths outside the node's ownership contract.
Credential-bearing files SHALL be rejected whole before staging, with neither
content nor partial excerpts staged or emitted. Attempts whose terminal is
fault-injected SHALL be excluded from salvage entirely.

#### Scenario: Worker wrote outside its ownership contract

- **GIVEN** a failed attempt changed an owned file and an unowned file
- **WHEN** salvage captures the attempt
- **THEN** only the owned file is staged and restored
- **AND** the unowned path is named in skipped-path evidence without content.

#### Scenario: Partial work contains a credential

- **GIVEN** a changed owned file contains credential-bearing content
- **WHEN** salvage screens the file
- **THEN** the whole file is rejected before staging
- **AND** the rejection is recorded by path only.

#### Scenario: Failure was fault-injected

- **WHEN** an attempt fails with a fault-injected terminal
- **THEN** no partial work from that attempt is salvaged or restored.
