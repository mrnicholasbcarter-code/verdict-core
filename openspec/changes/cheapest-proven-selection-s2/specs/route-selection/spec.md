## ADDED Requirements
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
The choose command with --no-confirm SHALL never send a model confirmation or write selection evidence. It SHALL select only routes with fresh exact-route proof and otherwise return no eligible route.

#### Scenario: No fresh proof
- **WHEN** --no-confirm is set and no route has fresh proof
- **THEN** selected is null and no confirmation is sent

### Requirement: Coding-task rating
Obvious code-edit task text SHALL use the coding rating through the shared family inference. Explicit task families SHALL take precedence and model names SHALL NOT determine task family.

#### Scenario: Python edit request
- **WHEN** task text is "edit a python function and run its test"
- **THEN** choose and serve use a code-edit family and aa_coding
