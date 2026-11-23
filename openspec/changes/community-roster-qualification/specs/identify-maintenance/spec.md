# Spec Delta

## ADDED Requirements

### Requirement: Community discovery preserves source identity and provenance

Verdict SHALL parse versioned community source snapshots as data, retain the source provider and full model ID, and report an invalid or empty active roster as a source failure. A pinned snapshot SHALL NOT be presented as a current upstream refresh.

#### Scenario: An upstream model has a nested ID
- **WHEN** the roster declares `nvidia/model` under provider `nvidia`
- **THEN** its source identity is `nvidia/nvidia/model`

### Requirement: Community inventory can narrow native screening

An opt-in community roster report SHALL retain unmatched entries and give a reason for each entry not screened. It SHALL join exact provider/model identities to concrete gateway routes. Community membership SHALL NOT establish free capacity, reachability, entitlement, or tool proof.

#### Scenario: External pricing does not decide qualification
- **WHEN** a reachable exact route with usable connection evidence lacks a price or has a positive external list price
- **THEN** it remains in the inventory and may be screened if the operator authorizes the send
- **AND** only the measured outcome determines liveness and capability

#### Scenario: A route cannot be screened
- **WHEN** the route is absent, lacks usable connection evidence, has a billing refusal still inside its negative window, or is blocked by the operator's spend policy
- **THEN** Verdict reports the reason without sending a probe

#### Scenario: One model refuses while another shares the connection
- **WHEN** a model-specific billing refusal is observed
- **THEN** its exact identity and confirmed aliases are skipped on subsequent screens
- **AND** sibling models remain screenable unless there is independently established account-wide refusal or a rate limit

### Requirement: Working, capable, and dispatchable are separate facts

A successful exact-route inference SHALL establish liveness only. Independent capability checks SHALL establish only what they measured. A roster match or a liveness result SHALL NOT grant dispatch authority. Repeat bounded screening SHALL retain untested and deferred entries so that coverage is not limited to an arbitrary sample.

#### Scenario: A screen reaches its budget
- **WHEN** the native screen records a tool pass for one route and defers others at its route cap
- **THEN** the report distinguishes the tool pass from the deferred results and does not report dispatch authority for any route
