# Spec Delta: Reuse fresh route evidence

## ADDED Requirements

### Requirement: Sufficient fresh proof SHALL avoid confirm calls
The gate SHALL use its existing deterministic ordering to select at most
max_confirm_candidates fresh admitted identities without spending confirm
requests when enough fresh identities exist. Prior admission exclusions,
authorization, quota and cooling constraints SHALL remain authoritative.

#### Scenario: Fresh shortlist is sufficient
- **WHEN** at least max_confirm_candidates admitted identities have fresh proof
- **THEN** no confirm transport calls are made and the fresh shortlist is admitted

### Requirement: Refresh requests SHALL be bounded
A refresh pass SHALL attempt at most max_confirm_candidates identities and
obey any tighter explicit budget. Failed confirms MAY be replaced only inside
that bound. Unrefreshed expired passports SHALL remain excluded as passport_stale.
Skipped observations SHALL NOT overwrite stored proof.

#### Scenario: First stale candidate fails
- **WHEN** the first confirm fails and a second candidate fits the request budget
- **THEN** the second candidate is attempted and only successful proof is admitted

### Requirement: Reselect SHALL retain the snapshot cache
A retry SHALL retain the normal snapshot TTL cache, record the failed route in
the health cache, and exclude it from the next selection. Two failed execution
attempts SHALL still deny with fail_closed_attempts_exhausted.

#### Scenario: Completion fails within the snapshot TTL
- **WHEN** a retryable completion fails while the cached snapshot remains current
- **THEN** a different route is selected without another snapshot fetch
