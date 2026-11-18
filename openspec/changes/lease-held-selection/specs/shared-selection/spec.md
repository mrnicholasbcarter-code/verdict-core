# Spec Delta

## Purpose

When the shared coordinator lease is held by another producer, selection still admits and chooses routes with fresh proof, skipping only probes and confirmations.

## MODIFIED Requirements

### Requirement: Selection during coordinator lease

When the shared coordinator lease (`verified-refresh.lock`) is held by another producer, selection SHALL still admit and choose routes that are already healthy, launchable, and have fresh agentic proof. Routes that would require a live probe or confirmation SHALL NOT be probed; instead, they SHALL receive the reason `lease_held` and be excluded from selection. The admitted set SHALL narrow only by removing routes that need probes, never by removing routes with fresh proof.

#### Scenario: Lease held, fresh proven route available

- **GIVEN** the coordinator lease is held, and at least one candidate route is healthy, launchable, and has a fresh passport (qualified within `PERMIT_PROOF_MAX_VALIDITY_SECONDS`)
- **WHEN** `EligibilityLadder.select` runs
- **THEN** that route is chosen, zero probe calls are made, and the selection receipt shows `reason: "selected"` for the chosen route and `reason: "lease_held"` for any routes that would have needed a probe

#### Scenario: Lease held, no fresh proven route

- **GIVEN** the coordinator lease is held, and all candidate routes would need a probe (unhealthy, not launchable, or stale proof)
- **WHEN** `EligibilityLadder.select` runs
- **THEN** no probe calls are made, `chosen` is `None`, every candidate has `reason: "lease_held"`, and selection fails closed with a named reason

#### Scenario: Lease not held

- **GIVEN** the coordinator lease is not held
- **WHEN** `EligibilityLadder.select` runs
- **THEN** behaviour is unchanged from the existing specification (probes dispatched up to `max_probes`, healthy+launchable+fresh-proof routes chosen)