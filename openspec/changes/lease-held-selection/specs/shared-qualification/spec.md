# Spec Delta

## Purpose

When the shared coordinator lease is held by another producer, admission admits routes with fresh passports without sending confirmations.

## MODIFIED Requirements

### Requirement: Admission during coordinator lease

When the shared coordinator lease (`verified-refresh.lock`) is held by another producer, `gate_admit_prove_confirm` SHALL admit routes that have fresh passports (qualified within `PERMIT_PROOF_MAX_VALIDITY_SECONDS`) without sending confirmation calls. Routes with stale or missing passports SHALL be marked `confirm_unavailable` with reason "coordinator lease held by another producer" and excluded from the admitted set. The function SHALL NOT return an empty admitted set when fresh passports exist.

#### Scenario: Lease held, fresh passports available

- **GIVEN** the coordinator lease is held, and the shortlist contains at least one identity with a fresh passport
- **WHEN** `gate_admit_prove_confirm` runs
- **THEN** all fresh-passport identities are admitted, zero confirmation calls are made, stale/missing-passport identities are excluded with `confirm_unavailable`, and the returned receipt has a non-empty `admitted` tuple

#### Scenario: Lease held, no fresh passports

- **GIVEN** the coordinator lease is held, and no identity in the shortlist has a fresh passport
- **WHEN** `gate_admit_prove_confirm` runs
- **THEN** zero confirmation calls are made, all identities are excluded with `confirm_unavailable`, and the returned receipt has `admitted=()`

#### Scenario: Lease not held

- **GIVEN** the coordinator lease is not held
- **WHEN** `gate_admit_prove_confirm` runs
- **THEN** behaviour is unchanged from the existing specification (confirmation calls dispatched per budget, fresh passports admitted, stale ones confirmed or excluded)