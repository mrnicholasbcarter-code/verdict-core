# Spec Delta

## ADDED Requirements

### Requirement: Planners come from the frontier band of fresh ratings

Planner selection for autodev, orchestrate and the certification preflight SHALL narrow admitted planner candidates to routes whose fresh, exactly joined planner rating is at least the frontier band times the best fresh rating for that metric in the metadata snapshot. Route names, declared tiers and the id classifier SHALL NOT qualify a planner. Ratings SHALL NOT admit a route that failed admission.

#### Scenario: Unrated frontier-named route
- **WHEN** an admitted route has no fresh rating
- **THEN** it is not a planner candidate

#### Scenario: Frontier band available
- **WHEN** an admitted route is in the frontier band
- **THEN** a frontier-band route is chosen, ordered FREE, UNKNOWN, SUBSCRIPTION, then rating

### Requirement: Planner fallback is one band and recorded

If no admitted route is in the frontier band, planner selection SHALL use the fallback band and SHALL record the required band, used band, reason, best score and threshold in the run report and receipt. With no route in either band, selection SHALL fail closed with `no_frontier_or_fallback_planner`.

#### Scenario: Frontier supply down
- **WHEN** the only admitted rated routes are in the fallback band
- **THEN** one is chosen and the downgrade is recorded

#### Scenario: Nothing qualifies
- **WHEN** no admitted route is in either band
- **THEN** planning fails closed with `no_frontier_or_fallback_planner`
