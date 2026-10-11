# Spec Delta

# Orchestration changes

## ADDED Requirements

### Requirement: Durable real-terminal availability feedback

The system SHALL project classified live-worker terminal availability failures
into the existing HealthCache using its serialized atomic writer. It SHALL NOT
create a second session-outcome ledger. It SHALL NOT record task verification,
ownership, task feedback, review judgement, context overflow, gateway-local busy,
unknown classification, success or injected/scripted terminals as negative health.

#### Scenario: Real upstream failure

- **WHEN** a live-attested worker terminates with a classified route-local 503
- **THEN** its selected route has durable negative health until the bounded deadline
- **AND** sibling routes and pool budgets are unchanged

#### Scenario: Invalid task implementation

- **WHEN** code verification, ownership, task feedback or review judgement fails
- **THEN** no route-health or credential-pool negative evidence is written

#### Scenario: Fixture or missing provenance

- **WHEN** the terminal is fault-injected, scripted or lacks live adapter attestation
- **THEN** it cannot supply negative real-session health evidence

### Requirement: Bounded negative scope

Route-scoped classifications SHALL remain exact-route negatives. Provider-scoped
credential quota, rate, authentication, payment and permission failures SHALL
block the selected canonical credential pool when known, or the provider when no
pool is known. The negative and scoped blocker SHALL share the classifier cooldown,
capped at 24 hours. Expired negatives SHALL become unprobed, never healthy.

#### Scenario: Shared credential aliases

- **WHEN** a provider-scoped credential failure occurs on one alias
- **THEN** a durable pool blocker uses the selected canonical pool name without an alias prefix
- **AND** no unrelated pool is blocked

#### Scenario: Per-model quota

- **WHEN** quota exhaustion is classified as route-scoped
- **THEN** only that route becomes negative and the shared pool bucket stays usable

#### Scenario: Bounded expiry

- **WHEN** a classified retry hint exceeds 24 hours
- **THEN** the route, scoped blocker and any zeroed bucket expire within 24 hours
- **AND** expiry does not authorize or admit the route

### Requirement: Observed turns and secret-safe provenance

Feedback SHALL store a nullable real turn count and its source. Explicit valid
counts SHALL take priority over valid terminal usage counts. Missing or invalid
counts SHALL remain unknown, never fabricated. Provenance SHALL contain only a
bounded opaque digest, not raw errors, transcripts, credentials or session refs.
Feedback SHALL NOT create capability qualification or positive health evidence.

#### Scenario: No turn telemetry

- **WHEN** a failing real terminal has no turn count
- **THEN** durable feedback stores a null count and an explicit unknown source

#### Scenario: Real turn telemetry

- **WHEN** terminal usage reports three observed assistant turns
- **THEN** feedback preserves the count and reports usage.turns as its source
- **AND** no capability pass is created
