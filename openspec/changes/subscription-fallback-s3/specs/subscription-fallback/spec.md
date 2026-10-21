## ADDED Requirements

### Requirement: Capable subscription fallback
Ordinary requests SHALL prefer qualified free routes and SHALL fall back to qualified subscription routes without a client retry, subject to existing attempt and retry-safety bounds. Metered routes SHALL NOT enter this fallback. Subscription routes SHALL sort by lowest median token consumption across validated accepted attempts of the same task family, then rating. A model/family SHALL have at least three unique accepted attempts to supply the median; otherwise usage is unknown. Unknown usage SHALL follow known usage.

#### Scenario: Free attempts fail
- **WHEN** all free attempts within the request budget fail and a capable subscription route has fresh admission proof
- **THEN** the subscription route is attempted last inside the same budget and the receipt records subscription fallback and its reason

#### Scenario: No capable subscription
- **WHEN** no free route or subscription route meets this task's existing gates
- **THEN** dispatch fails closed with a named reason and never uses metered fallback

### Requirement: Existing bounded screen handles rescreen requests
A fallback SHALL queue exact free-route hints in existing persisted health state. The existing explicitly consented screen SHALL prioritize queued free routes without changing its caps, cooldowns, proof rules or timer.

#### Scenario: Bounded rescreen
- **WHEN** a free-route hint is queued and the existing screen runs with consent
- **THEN** the hinted free route sorts first within its capacity class, consumes the same route/provider/wall budgets, and the hint is removed only after a real screen turn
