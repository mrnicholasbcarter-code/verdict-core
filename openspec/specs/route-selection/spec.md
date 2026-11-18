# route-selection Specification

## Purpose

Rank and actually select qualified routes by free, subscription, metered, unknown capacity, then existing rating and tie-breaks.

## Requirements

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

### Requirement: Serve-path cheapest qualified capacity tier
For ordinary tasks the serve/route candidate pool SHALL order by the shared capacity tier before Top-k, then preserve existing within-tier score and demotion order. Frontier-worthy tasks SHALL retain their existing proof-bar ranking until outcome evidence exists. Existing admission, qualification, explicit filters, alias dedupe and provider-diversity rules SHALL remain in force.

#### Scenario: Free route falls outside score-only Top-k
- **WHEN** a qualified free route has lower score than subscription candidates filling score-only Top-k
- **THEN** the free route is shortlisted and selected first

### Requirement: Route-scoped billing evidence
An upstream HTTP 402 SHALL record the existing long-lived route-scoped payment negative with metered capacity evidence, visible to both selectors. Sibling routes SHALL remain unaffected.

#### Scenario: Insufficient credit
- **WHEN** one dispatched route returns 402
- **THEN** that route is METERED while the negative is active and is not redispatched

### Requirement: Read-only choose
The choose command with --no-confirm SHALL never send a model confirmation or write selection evidence. It SHALL select only routes with valid exact-route proof (passport admitted and not expired) and otherwise return no eligible route. Proof older than the 10-minute permit freshness window is accepted when the passport remains valid.

#### Scenario: Valid but older-than-10-minute proof is selected
- **WHEN** --no-confirm is set and a route has an admitted passport that is not expired but qualified_at exceeds 600 seconds
- **THEN** the route is selected with zero probes and zero writes

#### Scenario: Expired or missing passport is not selected
- **WHEN** --no-confirm is set and a route's passport is missing, expired, unauthorized, ineligible, or has newer negative evidence
- **THEN** the route is skipped with a reason naming the condition (not probe_budget_exhausted)

#### Scenario: No valid proof
- **WHEN** --no-confirm is set and no route has valid proof
- **THEN** selected is null and no confirmation is sent

### Requirement: Coding-task rating
Obvious code-edit task text SHALL use the coding rating through the shared family inference. Explicit task families SHALL take precedence and model names SHALL NOT determine task family.

#### Scenario: Python edit request
- **WHEN** task text is "edit a python function and run its test"
- **THEN** choose and serve use a code-edit family and aa_coding

## Tasks
- [ ] (post-merge) Install from merged main and confirm `verdict choose --task-class implementation --no-confirm --json` selects a route
