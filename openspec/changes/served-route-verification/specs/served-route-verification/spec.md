# Spec Delta

## Purpose

Every worker and relay attempt records the route OmniRoute actually served and
checks it against the route Verdict selected. A mismatch is a recorded failure.
An unknown served identity is never a match.

## ADDED Requirements

### Requirement: The served route SHALL be read from OmniRoute response headers
Verdict SHALL read the served identity from `X-OmniRoute-Provider`,
`X-OmniRoute-Model`, `X-OmniRoute-Decision` and
`X-OmniRoute-Selected-Connection-Id`, matching header names
case-insensitively. A header that is repeated with different values, empty, or
holds a control character SHALL make the identity unusable.

#### Scenario: Clean non-combo response
- **WHEN** a response to `cc/claude-opus-5-5` carries provider `cc`, model `claude-opus-5-5`, decision `strategy=single; provider=cc; latency_ms=812` and a connection id
- **THEN** the check is `verified`
- **AND** the connection id is recorded

#### Scenario: Repeated conflicting header
- **WHEN** `X-OmniRoute-Model` appears twice with different values
- **THEN** the check is `unknown`

### Requirement: Only an exact provider and model match SHALL verify the served route
The check SHALL be `verified` only when the served provider equals the selected
provider after mapping OmniRoute provider ids to their aliases, the served
model equals the selected model, any decision provider equals the served
provider, and any decision strategy is `single`. A known difference SHALL be
`mismatch`. Missing headers, an unusable identity or a selection without a
provider prefix SHALL be `unknown`. An `auto/*` or `combo/*` selection SHALL be
`opaque`: the served member is recorded and proves nothing.

#### Scenario: Different model served
- **WHEN** `cc/claude-opus-5-5` is selected and the response says model `claude-haiku-5-5`
- **THEN** the check is `mismatch`

#### Scenario: No identity headers
- **WHEN** a response carries none of the identity headers
- **THEN** the check is `unknown`
- **AND** it is never recorded as a match

#### Scenario: Opaque selection
- **WHEN** `auto/best-coding` is selected
- **THEN** the check is `opaque`

### Requirement: A served-route mismatch SHALL fail the attempt
A direct gateway worker attempt whose check is `mismatch` SHALL end with
`ok=False` and error `model_mismatch`, also when the answer text is complete.
The relay SHALL record a header mismatch as `identity_mismatch` and SHALL NOT
return that answer as a success. A missing body `model` SHALL NOT be replaced
by the selected route.

#### Scenario: Worker answer from another model
- **WHEN** a direct gateway worker gets a complete answer whose headers name a different model
- **THEN** the attempt fails with `model_mismatch`
- **AND** the route is rerouted and cooled by the existing recovery path

#### Scenario: Relay answer from another model
- **WHEN** the relay gets a response whose headers name a different model than the selected concrete route
- **THEN** the attempt is recorded as `identity_mismatch`

### Requirement: The run receipt SHALL bind the served identity per attempt
When an attempt carries a served-route check, its receipt row SHALL include
the check status, served route, provider, model, connection id, decision
strategy and reasons. Its `route_identity` SHALL be `match` only for
`verified`, `mismatch` for `mismatch`, and `unattested` for `unknown` and
`opaque`. Attempts without a check SHALL keep their previous classification,
so existing receipts still verify against their `events.jsonl` digest.

#### Scenario: Verified attempt in the receipt
- **WHEN** a run has one attempt with a `verified` check
- **THEN** the receipt attempt has `route_identity` `match` and the connection id

#### Scenario: Unknown attempt in the receipt
- **WHEN** an attempt check is `unknown` and the body model equals the route id
- **THEN** the receipt attempt has `route_identity` `unattested`
