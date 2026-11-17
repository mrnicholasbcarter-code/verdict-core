# Spec Delta

## ADDED Requirements

### Requirement: Scheduled screening covers every stale route

The scheduled screen MUST probe every free and subscription route whose agentic proof is missing or stale, subject to the existing consent and budget rules, rather than limiting the run to a fixed count of routes. The screen MUST remain bounded by a wall-clock timeout.

#### Scenario: Stale inventory is screened

- **WHEN** the scheduled screen runs with explicit live-probe consent
- **THEN** every free and subscription route without fresh agentic proof is eligible for screening
- **AND** the run stops within its configured wall-clock budget
- **AND** routes with fresh proof are skipped
