## ADDED Requirements
### Requirement: Cheapest qualified capacity tier
The selector SHALL rank qualified free routes before subscription and metered routes, preserving existing within-tier rating and tie-break order. Explicit capacity and spend filters SHALL still narrow membership.

#### Scenario: Unrated free route has tool proof
- **WHEN** an unrated free route passes the task's existing gates and a higher-rated subscription route also passes
- **THEN** the actual selected route is free

### Requirement: Current exact-route dispatch proof
A real select-time confirmation, screen pass or completed dispatch SHALL produce current exact-route passport proof. The permit maximum SHALL remain 600 seconds and negative concurrent evidence SHALL win. Revoked epochs and consumed attempts SHALL remain denied.

#### Scenario: Proof is stale but confirm succeeds
- **WHEN** passport proof exceeds 600 seconds and one budgeted confirm succeeds without newer negative evidence
- **THEN** the selector refreshes proof and a new scoped attempt can obtain a permit

### Requirement: Assumed-free capacity is not capability proof
Routes with a connected account and no specific paid or subscription evidence SHALL be FREE with evidence rule assumed_free_no_paid_evidence. They SHALL pass the same tool proof bar as every free worker.

#### Scenario: Missing price and missing tool screen
- **WHEN** a connected API-key route has no price and no tool screen
- **THEN** it is classified FREE but is not eligible as an implementation worker
