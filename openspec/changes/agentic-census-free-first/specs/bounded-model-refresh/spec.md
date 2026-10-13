# Spec Delta: Agentic evidence and background census

## ADDED Requirements

### Requirement: Trusted controller outcomes SHALL become exact-route agentic evidence
The census SHALL import trusted controller session outcomes into the health
cache through the existing merge lock. Each imported record SHALL carry the
exact route, timestamp, source and child provenance. An import SHALL NOT refresh
liveness, grant capacity or change admission.

#### Scenario: A controller outcome is imported
- **WHEN** a trusted controller session finished a task on an exact route
- **THEN** the health cache records agentic evidence for that route with its timestamp, source and child id
- **AND** the route's liveness timestamp and admission state are unchanged

### Requirement: The consented census daemon SHALL have explicit user-service setup
The existing consented, budgeted census daemon SHALL be installable and
removable as a systemd user service, with a dry-run that prints the planned
unit as JSON and changes nothing.

#### Scenario: Dry-run install
- **WHEN** the operator runs the service install with dry-run
- **THEN** the planned unit is printed as JSON and no file or unit is written

### Requirement: Eligibility SHALL show full-cache health and fresh agentic qualification
Eligibility output SHALL count health states over the full cache and show fresh
agentic qualification per capacity class.

#### Scenario: Counts by capacity class
- **WHEN** the cache holds fresh agentic evidence for one free and one subscription route
- **THEN** eligibility reports one qualified route in each of those capacity classes
