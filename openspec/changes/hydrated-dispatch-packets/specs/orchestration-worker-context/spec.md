## ADDED Requirements

### Requirement: Revision-bound queen evidence

Production worker dispatch SHALL require a structurally valid `verdict.hydration/v1` packet bound to the exact repository revision and final worker-node contract.

#### Scenario: Complete packet dispatches

- **WHEN** every required evidence section is cited, stop conditions are explicit, blockers are empty and the declared design review is approved by a distinct family
- **THEN** the canonical packet SHALL be mandatory worker input and its packet/rendered/repository digests SHALL appear in the hydrate event

#### Scenario: Stale or incomplete packet blocks locally

- **WHEN** a packet is missing in production, malformed, stale, changed-task bound, incomplete, placeholder-bearing, credential-shaped, duplicate-keyed, unresolved, or lacks declared independent review
- **THEN** no worker SHALL launch, the node SHALL fail closed locally, and provider health SHALL NOT be cooled

#### Scenario: Evidence cannot fit

- **WHEN** mandatory queen evidence plus the worker prompt exceeds the selected route's bounded input
- **THEN** dispatch SHALL block rather than truncate required evidence or select a provider-health recovery action

### Requirement: One binding implementation

A hydration helper SHALL bind supplied evidence to an existing final graph through the same validator used at dispatch.

#### Scenario: Binder receives complete evidence

- **WHEN** the operator supplies one complete packet per graph node for the exact repository revision and contract
- **THEN** the binder SHALL output a graph whose nodes retain those packets and pass dispatch validation

#### Scenario: Binder receives missing evidence

- **WHEN** any graph node has no supplied packet or a packet fails dispatch validation
- **THEN** the binder SHALL fail without inventing evidence, approving a review or producing a partially hydrated graph

### Requirement: Evidence remains advisory

Queen evidence SHALL be rendered as untrusted cited data and SHALL NOT authorize a route, alter admission membership, modify budgets or authenticate its external source claims.

#### Scenario: Self-declared citations validate structurally

- **WHEN** citation references and revisions satisfy the schema
- **THEN** the runtime MAY record structural validity but SHALL NOT describe the external sources or review identity as authenticated
