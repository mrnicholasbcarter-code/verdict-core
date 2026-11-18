# Spec Delta: Read-only choose accepts valid proof older than 10 minutes

## MODIFIED Requirements

### Requirement: Read-only choose
The choose command with --no-confirm SHALL never send a model confirmation or write selection evidence. It SHALL select only routes with valid exact-route proof (passport admitted and not expired) and otherwise return no eligible route. Proof older than the 10-minute permit freshness window is accepted when the passport remains valid.

#### Scenario: Valid but older-than-10-minute proof is selected
- **WHEN** --no-confirm is set and a route has an admitted passport that is not expired but qualified_at exceeds 600 seconds
- **THEN** the route is selected with zero probes and zero writes

#### Scenario: Expired or missing passport is not selected
- **WHEN** --no-confirm is set and a route's passport is missing, expired, unauthorized, ineligible, or has newer negative evidence
- **THEN** the route is skipped with a reason naming the condition (not probe_budget_exhausted)

#### Scenario: No fresh proof
- **WHEN** --no-confirm is set and no route has valid proof
- **THEN** selected is null and no confirmation is sent