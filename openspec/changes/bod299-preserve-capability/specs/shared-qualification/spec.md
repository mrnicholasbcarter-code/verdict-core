## ADDED Requirements

### Requirement: Availability failure preserves only prior exact-route capability

The health cache SHALL retain an existing successful agentic proof strictly older than a recognized no-answer availability failure, retaining its original timestamp. The route SHALL remain negative until its unchanged backoff expires, and SHALL remain unprobed rather than healthy after that expiry until new health is observed. Retained capability SHALL NOT authorize dispatch without the existing live eligibility and permit checks.

#### Scenario: Pass then availability failure then recovery
- **GIVEN** an exact route has a successful agentic proof strictly older than the new observation
- **WHEN** a single-call or agentic probe records a no-answer timeout, upstream, rate-limit, authentication, payment or permission failure
- **THEN** the prior proof and timestamp remain unchanged across save/reload and serialized merge
- **AND** current health is negative with a new last_failure_at and unchanged negative backoff policy
- **AND** backoff expiry alone is unprobed, not healthy
- **AND** later single-call health recovery does not renew capability expiry or alter a sibling route

#### Scenario: Answered capability failure revokes
- **GIVEN** an exact route has a successful agentic proof
- **WHEN** an answered tool/agentic attempt fails required behavior, or a result is unknown, malformed or model-mismatched
- **THEN** the probe capability is revoked and later single-call health success does not resurrect it
- **AND** existing newer answered agentic-probe session revocation and session failure denial remain authoritative

#### Scenario: Missing or expired proof cannot gain qualification
- **GIVEN** a route has no successful proof or its original proof is expired
- **WHEN** a no-answer availability failure is recorded
- **THEN** capability remains unqualified and no failure renews its timestamp
