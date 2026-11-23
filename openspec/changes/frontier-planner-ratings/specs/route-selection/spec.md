# Spec Delta

## ADDED Requirements

### Requirement: Planners come from the frontier band of fresh ratings

Planner selection for autodev, orchestrate and the certification preflight SHALL narrow admitted planner candidates to routes whose model ranks in the top 20% (operator-configurable) of distinct rated models in the route inventory by fresh, exactly joined `aa_intelligence`. The ranking SHALL use only the newest fetch of the rating source. A `:suffix` variant SHALL share its model's slot. Models tied with the cutoff score SHALL be included. Route names, declared tiers and the id classifier SHALL NOT qualify a planner. Ratings SHALL NOT admit a route that failed admission.

#### Scenario: Unrated frontier-named route
- **WHEN** an admitted route has no fresh rating in the newest fetch
- **THEN** it is not a planner candidate

#### Scenario: Suffix variant
- **WHEN** a model and its `:free` variant are both rated
- **THEN** they count as one model in the ranking

### Requirement: Strongest non-metered planner first

Within a band, planner selection SHALL choose the highest-rated admissible non-metered route (FREE, UNKNOWN or SUBSCRIPTION). Equal scores SHALL be ordered FREE and UNKNOWN before SUBSCRIPTION, then by higher `aa_coding`. A METERED route SHALL plan only when no non-metered route in the band is admissible, and only within the operator's spend limit.

#### Scenario: Stronger subscription route
- **WHEN** a band holds a free route rated 55 and a subscription route rated 62
- **THEN** the subscription route plans

#### Scenario: Equal scores
- **WHEN** a free route and a subscription route in a band have the same rating
- **THEN** the free route plans

### Requirement: Planner fallback is one band and recorded

If no admitted route is in the frontier band, planner selection SHALL use the fallback band (top 30%, operator-configurable). It SHALL record the required band, the used band, the reason, the model's rank and the band cutoff in the run report and receipt. With no route in either band, selection SHALL fail closed with `no_frontier_or_fallback_planner`.

#### Scenario: Frontier supply down
- **WHEN** the only admitted rated routes are in the fallback band
- **THEN** one is chosen and the downgrade is recorded

#### Scenario: Nothing qualifies
- **WHEN** no admitted route is in either band
- **THEN** planning fails closed with `no_frontier_or_fallback_planner`

### Requirement: Planner failover spans two families

Where a planner path keeps a failover list, the primary planner SHALL be the strongest admissible route in the best available band. A failover from another model family SHALL come from the frontier band when one is admissible, otherwise from the fallback band, and the band of each planner SHALL be recorded. With fewer than two model families admissible across both bands, autodev planner selection SHALL fail closed with a reason that names the number of families found.

#### Scenario: One family in the frontier band
- **WHEN** only Anthropic routes are admissible in the frontier band and an OpenAI route is admissible in the fallback band
- **THEN** the Anthropic route is the primary planner, the OpenAI route is the failover, and the failover is recorded as fallback band

#### Scenario: One family overall
- **WHEN** only one model family is admissible across both bands
- **THEN** autodev planner selection fails closed and names one family found

### Requirement: Doctor reports planner supply

`verdict doctor` SHALL warn when no admissible route is in the frontier band, and SHALL say whether the fallback band is reachable.

#### Scenario: Frontier band empty
- **WHEN** no admissible route is in the frontier band
- **THEN** doctor shows a `planner_supply` warning naming the reachable band, if any
