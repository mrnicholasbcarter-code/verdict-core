# Spec Delta: Cheapest sufficient route first with role split

## Purpose

Ensure the route with the smallest tier overage above the node's floor (slack)
ranks first within each capacity class for worker/implement nodes, so the least
capable route that still meets the requirement is preferred over a stronger (more
expensive) one.

Planner and reviewer roles use the inverse ordering (strongest-first) because
decomposition quality drives everything downstream.

## ADDED Requirements

### Requirement: `TaskRequirements` SHALL carry a `role_ordering` field

`TaskRequirements` SHALL expose a `role_ordering: str` field with the default
value `"cheapest_sufficient_first"`.  Callers that need strongest-first SHALL
pass `role_ordering="strongest_first"` explicitly.

#### Scenario: Default role_ordering is cheapest_sufficient_first

- **WHEN** a `TaskRequirements` is constructed without `role_ordering`
- **THEN** `requirements.role_ordering` SHALL equal `"cheapest_sufficient_first"`

## MODIFIED Requirements

### Requirement: `_rank_key` ordering SHALL depend on `role_ordering`

`EligibilityLadder._rank_key` SHALL support two orderings, chosen by
`requirements.role_ordering` passed to `_assess_all`:

**cheapest_sufficient_first** (worker/implement nodes, the default):
Slack comes before rating so the least capable sufficient route wins.
Subscription token usage follows slack and precedes rating (D3 preserved).
Full sort key: `capacity_order → slack → subscription_tokens → rating_rank → …`

**strongest_first** (planner and reviewer roles):
Rating and declared tier come before slack so the highest-quality admitted route
wins.  Subscription token usage follows capacity_order, then rating.
Full sort key: `capacity_order → subscription_tokens → rating_rank → … → slack`

#### Scenario: Worker node picks the smallest-slack sufficient route

- **WHEN** `role_ordering="cheapest_sufficient_first"`
- **AND** the admitted set contains a tier-1 subscription route (slack=0) and a
  tier-0 subscription route (slack=1) for a tier-1 required task
- **THEN** the tier-1 route SHALL be selected
- **AND** `rank_components["ordering_basis"]` SHALL equal `"cheapest_sufficient_first"`

#### Scenario: Planner node picks the highest-rated admitted route

- **WHEN** `role_ordering="strongest_first"`
- **AND** the admitted set contains a tier-0 subscription route (slack=1) and a
  tier-1 subscription route (slack=0) for a tier-1 required task
- **THEN** the tier-0 route SHALL be selected (better rated despite worse slack)
- **AND** `rank_components["ordering_basis"]` SHALL equal `"strongest_first"`

#### Scenario: Escalation to ceiling=0 selects tier-0 when tier-1 is excluded

- **WHEN** escalation ceiling is 0
- **AND** the admitted set contains a tier-1 and a tier-0 subscription route
- **THEN** the tier-1 route SHALL fail with reason `below_escalation_floor`
- **AND** the tier-0 route SHALL be selected

#### Scenario: Equal slack is broken by rating quality (cheapest_sufficient_first)

- **WHEN** `role_ordering="cheapest_sufficient_first"` (or default)
- **AND** two admitted routes have the same slack
- **AND** one has declared-tier evidence (rating_rank=2) and the other has
  unknown rating (rating_rank=3)
- **THEN** the route with declared-tier evidence SHALL be selected

### Requirement: `rank_components` SHALL expose `ordering_basis`

The `rank_components` and `_rank_components_for_display` dicts SHALL include the
key `"ordering_basis"` whose value reflects the ordering actually used:
`"cheapest_sufficient_first"` or `"strongest_first"`.

#### Scenario: Rank components reflect the active ordering

- **WHEN** a route is assessed by `EligibilityLadder` with `role_ordering="strongest_first"`
- **THEN** `rank_components["ordering_basis"]` SHALL equal `"strongest_first"`
- **WHEN** a route is assessed with `role_ordering="cheapest_sufficient_first"` (or default)
- **THEN** `rank_components["ordering_basis"]` SHALL equal `"cheapest_sufficient_first"`
