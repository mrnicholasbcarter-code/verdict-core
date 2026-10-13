# Spec Delta: Quota truth

## ADDED Requirements

### Requirement: The verified-model projection SHALL expose availability evidence per connection scope
Each connection observation SHALL keep provider and scope (account, pool,
provider or model) separate and expose configured, active, nullable quota
percent and window, observed cooldown, source, timestamp and age. Missing quota
SHALL be UNKNOWN; quota window thresholds are policy and SHALL NOT provide usage.
Account identifiers SHALL be hashed at display and provider errors reduced to a
category.

#### Scenario: Quota is not reported
- **WHEN** a connection is configured and active but reports no usage
- **THEN** its quota percent is UNKNOWN, never 100%

### Requirement: A current observed cooldown SHALL beat percentage and cached health
A current cooldown SHALL block admission even when the displayed quota is 100%
or a cached probe is healthy. A timestamped 429 without an explicit cooldown
SHALL use the health-cache rate-limit window; a newer route success SHALL
supersede that error. Expired cooldowns SHALL stay historical evidence and SHALL
NOT block.

#### Scenario: 100% display with a current 429
- **WHEN** a connection shows 100% remaining and has a current account 429
- **THEN** routes bound to that account are not admitted until the cooldown ends

#### Scenario: Expired cooldown
- **WHEN** the only cooldown has expired
- **THEN** it is shown as history and does not block admission

### Requirement: Scoped blocks SHALL respect route binding
An account-scoped block SHALL reuse the existing account/pool exhaustion path. A
usable sibling account MAY serve an unbound route; it SHALL NOT serve an
explicitly bound route.

#### Scenario: Bound route on a cooled account
- **WHEN** a route is bound to a cooled account and a sibling account is usable
- **THEN** the bound route is blocked and an unbound route may use the sibling
