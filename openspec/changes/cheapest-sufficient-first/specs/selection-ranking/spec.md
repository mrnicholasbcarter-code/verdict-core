# Spec Delta: Cheapest sufficient route first

## Purpose

Ensure the route with the smallest tier overage above the node's floor (slack)
ranks first within each capacity class, so the least capable route that still
meets the requirement is preferred over a stronger (more expensive) one.

## MODIFIED Requirements

### Requirement: `_rank_key` SHALL place slack before rating within a capacity class

The selection sort key SHALL order admitted routes by slack (ascending) before
any rating-based key, so the route that most closely matches the floor (slack=0)
wins over an over-qualified route (slack>0) with a better rating.

Subscription token usage SHALL follow slack and precede rating, preserving D3 of
`2026-11-15-shared-selection-contract` for within-slack tie-breaking.

The full sort key within a capacity class is therefore:
`slack → subscription_tokens → rating_rank → neg_rating → declared_tier → …`

#### Scenario: Tier-1 route beats tier-0 route for a tier-1 required task

- **WHEN** a task requires `max_capability_tier=1`
- **AND** the admitted set contains a tier-1 subscription route (slack=0) and a
  tier-0 subscription route (slack=1)
- **THEN** the tier-1 route SHALL be selected
- **AND** `rank_components["slack"]` SHALL be 0 for the tier-1 route and 1 for
  the tier-0 route

#### Scenario: Escalation to ceiling=0 selects tier-0 when tier-1 is excluded

- **WHEN** escalation ceiling is 0
- **AND** the admitted set contains a tier-1 and a tier-0 subscription route
- **THEN** the tier-1 route SHALL fail with reason `below_escalation_floor`
- **AND** the tier-0 route SHALL be selected

#### Scenario: Equal slack is broken by rating quality

- **WHEN** two admitted routes have the same slack
- **AND** one has declared-tier evidence (rating_rank=2) and the other has
  unknown rating (rating_rank=3)
- **THEN** the route with declared-tier evidence SHALL be selected

### Requirement: `rank_components` SHALL expose `ordering_basis`

The `rank_components` and `_rank_components_for_display` dicts SHALL include the
key `"ordering_basis"` with the constant value `"cheapest_sufficient_first"` so
consumers can confirm which policy produced the selection.

#### Scenario: Rank components include ordering_basis

- **WHEN** any route is assessed by `EligibilityLadder`
- **THEN** `rank_components["ordering_basis"]` SHALL equal
  `"cheapest_sufficient_first"`
