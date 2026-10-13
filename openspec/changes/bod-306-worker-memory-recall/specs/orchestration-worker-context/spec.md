# Spec Delta: Orchestration worker context

## MODIFIED Requirements

### Requirement: Worker packs SHALL recall gated memory facts as cited advisory evidence

A worker pack SHALL include at most two active, unexpired, gate-verified MemoryPlane records from the `decisions`, `feedback`, `patterns` and `evidence` namespaces whose sensitivity is `public`, `standard` or `internal`, selected by this node's objective, acceptance criteria and symbols. Each recalled record SHALL be redacted and cited by its record id, digest, authority and trust. Packs SHALL NOT recall transcripts, session history, unverified records or other namespaces, and SHALL NOT write or create memory.

#### Scenario: Relevant gated decision exists

- **GIVEN** MemoryGate wrote a decision and an operator correction that mention the node's target symbol
- **WHEN** the production hydrator builds the worker prompt
- **THEN** both facts appear with `memory:<record_id>` citations after the code evidence
- **AND** the prompt evidence lists them as pack sources.

#### Scenario: Ineligible records exist

- **GIVEN** an unverified record, a confidential record, a session-history record and an unrelated pattern
- **WHEN** the pack is built
- **THEN** none of them appears in the prompt.

#### Scenario: No plane, a broken plane, or pending WAL frames

- **WHEN** the plane file is missing, cannot be opened, or has uncheckpointed WAL frames
- **THEN** the pack keeps its code evidence and reports `memory.search:no_default_location`, `memory.search:memory_plane_unavailable` or `memory.search:memory_plane_busy`
- **AND** no file in the plane's directory is created or changed.

#### Scenario: Recalled fact holds a credential

- **WHEN** a recalled record contains a credential shape
- **THEN** the rendered pack shows `[redacted]` in its place.
