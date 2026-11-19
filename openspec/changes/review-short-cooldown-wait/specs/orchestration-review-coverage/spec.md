## ADDED Requirements

### Requirement: Reviewer selection SHALL wait out one short cooldown
When reviewer selection returns no route, the reviewer SHALL wait once per review level for the
earliest route whose only rejection is a cooldown that ends within
`RuntimePolicy.max_cooldown_wait_seconds` and within the remaining review deadline, and then SHALL
select again. A route SHALL qualify only when the selector, given the same requirements, assesses
it as eligible at the end of its cooldown. Implementer routes, excluded families and backend pools,
and routes below the reviewer floor SHALL never be waited for. The wait SHALL use the runtime's
clock and sleep and SHALL be recorded as a `cooldown` event with scope `wait`, category
`waiting_for_capacity`, `until`, and `phase` `review`. The waited-for route SHALL still pass
selection, the launch gate and the pre-dispatch re-check, and reviewer attempts SHALL stay bounded
by `max_reviewer_attempts` and the review deadline.

#### Scenario: Only reviewer route cools for a few seconds
- **WHEN** no reviewer is eligible and the only route that meets the reviewer floor is in a
  cooldown that ends in 5 seconds
- **THEN** the reviewer waits once, records a `waiting_for_capacity` wait event, selects that
  route and runs the review

#### Scenario: Cooldown longer than the policy limit or the review deadline
- **WHEN** the only cooling route's cooldown ends after the policy limit or after the review deadline
- **THEN** the review returns ERROR "no independent reviewer eligible" without waiting

#### Scenario: Cooling route that fails another rule
- **WHEN** the cooling route is an implementer route, is in an excluded family or pool, or is below
  the reviewer floor
- **THEN** the review returns ERROR "no independent reviewer eligible" without waiting

#### Scenario: Route not usable after the wait
- **WHEN** after the wait the route is still cooling or fails the pre-dispatch re-check
- **THEN** the review returns ERROR after exactly one wait and does not wait again

#### Scenario: Reviewer available
- **WHEN** a reviewer route is eligible
- **THEN** no wait happens
