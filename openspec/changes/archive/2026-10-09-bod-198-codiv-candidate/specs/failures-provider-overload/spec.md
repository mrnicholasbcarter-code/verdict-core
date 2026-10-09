# Spec Delta

## Purpose

Classifies 529 responses as provider infrastructure pressure (temporary capacity issue)
rather than model quality degradation, enabling appropriate recovery policy.

## ADDED Requirements

### Requirement: 529 SHALL indicate provider capacity pressure, not quality degradation

When a provider returns 529 (Service Overload), this SHALL be classified as provider
infrastructure pressure (temporary capacity issue) using the existing `UPSTREAM` or
new `PROVIDER_OVERLOAD` failure class.

529 SHALL NOT be interpreted as permanent model quality degradation.

#### Scenario: 529 classified as infrastructure pressure
- **WHEN** provider returns 529 response
- **THEN** failure class is `UPSTREAM` or `PROVIDER_OVERLOAD` (infrastructure)
- **AND NOT** classified as model capability deficit

#### Scenario: 529 allows bounded retry
- **WHEN** provider returns 529 response
- **THEN** bounded retry is permitted (transient failure policy)

#### Scenario: 529 does not trigger model downgrade
- **WHEN** provider returns 529 response  
- **THEN** route's model quality score is not degraded
