# Spec Delta: Orchestration worker context

## ADDED Requirements

### Requirement: Production worker hydration SHALL use a narrow advisory native pack

The planner worker prompt SHALL contain node-specific bounded evidence when available, using existing Core query execution and compilation. It SHALL retain ownership, verification and final-result instructions. Retrieval SHALL NOT authorize a model route or claim optional tool health.

#### Scenario: Target symbol and contracts exist

- **GIVEN** a node names a Python target with a definition, callers, relevant tests and required design text
- **WHEN** the production hydrator builds its worker prompt
- **THEN** it includes relevant bounded excerpts/native references with source citations
- **AND** the scripted runtime dispatch contains the pack and unchanged worker rules.

### Requirement: Context projection SHALL respect byte and estimated-token allowances

Citations and pack metadata SHALL count against the allowance. Optional candidate ContextPlan SHALL narrow it by input window and reserves after mandatory prompt accounting. Missing selected window SHALL be labeled `conservative_unknown_candidate`, never invented.

#### Scenario: Small allowance or multi-byte content

- **WHEN** source uses multi-byte characters or the context allowance shrinks
- **THEN** rendered context fits its byte and estimated-token limits without broken UTF-8
- **AND** whole units can be omitted with explicit budget status.

#### Scenario: Caller provides candidate plan

- **GIVEN** a candidate plan with output/tool reserves and a window
- **WHEN** hydration builds context
- **THEN** mandatory prompt tokens and context fit the remaining allowance when mandatory instructions alone fit
- **AND** mandatory instructions are never silently truncated to claim a fit.

### Requirement: Native failures SHALL retain safe fallback and explicit gaps

Unavailable external adapters SHALL NOT prevent native retrieval. A provider exception SHALL become a named retrieval gap without exception text. Query failure SHALL retain safe required excerpts.

#### Scenario: Graph/parser or query fails

- **WHEN** native graph/code discovery or query execution fails
- **THEN** safe required contracts/design remain available
- **AND** gaps identify `retrieval_error` or `native_query_error:required_excerpts_fallback` without invented healthy/empty success.

### Requirement: Source excerpts SHALL carry provenance and exclude unsafe payloads

Citations SHALL include excerpt digest, known exact span or explicit unknown span, observed time, trust, authority and revision. Unknown revision SHALL stay unknown. Secret-bearing content SHALL be rejected before slicing. The adapter SHALL NOT open/write durable memory, retrieve raw transcripts or render outside-symlink/dotfile sources.

#### Scenario: Secret occurs beyond excerpt boundary

- **GIVEN** a secret occurs after otherwise useful source text
- **WHEN** the worker pack is built
- **THEN** the whole source is excluded before slicing and neither secret nor partial credential reaches the pack.

#### Scenario: Transcript or outside symlink source

- **WHEN** required context or native retrieval finds a transcript, dotfile or outside-worktree symlink
- **THEN** its content is absent from the rendered worker pack.
