## ADDED Requirements

### Requirement: Capable subscription fallback
Ordinary requests SHALL prefer qualified free routes and SHALL fall back to qualified subscription routes without a client retry, subject to existing attempt and retry-safety bounds. Capable metered routes SHALL be the last resort after free and subscription, with existing payment-required negatives excluded. Subscription routes SHALL sort by lowest median token consumption across validated accepted attempts of the same task family, then rating. A model/family SHALL have at least three unique accepted attempts to supply the median; otherwise usage is unknown. Unknown usage SHALL follow known usage.

#### Scenario: Free attempts fail
- **WHEN** all eligible free routes fail, a capable subscription route has fresh admission proof, and an attempt remains
- **THEN** the subscription route is attempted before metered inside the same budget and the receipt records subscription fallback and its reason

#### Scenario: Caller-authorized free supply is unproven
- **WHEN** a caller-authorized possible free route has neither usable dispatch proof nor current explicit evidence that it is unavailable or unqualified, including a route outside the bounded shortlist
- **THEN** higher-tier fallback is denied and the receipt distinguishes unknown free supply from exhausted attempts

#### Scenario: Free supply is known unavailable or unqualified
- **WHEN** a free route has a current explicit denial or has explicitly failed task qualification, and no other possible free route remains unaccounted for
- **THEN** that route does not prevent a freshly admitted higher-tier fallback within the caller's existing attempt and spend limits

#### Scenario: A free-route check is inconclusive
- **WHEN** a free-route check has no qualification result or its negative passport evidence is stale
- **THEN** free supply remains unknown and no higher-tier fallback is sent on that basis

#### Scenario: Attempt budget runs out
- **WHEN** three eligible free routes fail within the existing three-attempt budget
- **THEN** no higher-tier route is sent and the receipt records budget exhaustion, not free-supply exhaustion

#### Scenario: No capable subscription
- **WHEN** no free, subscription or metered route meets this task's existing gates
- **THEN** dispatch fails closed with a named reason and sends nothing

### Requirement: Existing bounded screen handles rescreen requests
A fallback SHALL queue exact free-route hints in existing persisted health state. The existing explicitly consented screen SHALL prioritize queued free routes without changing its caps, cooldowns, proof rules or timer.

#### Scenario: Bounded rescreen
- **WHEN** a free-route hint is queued and the existing screen runs with consent
- **THEN** the hinted free route sorts first within its capacity class, consumes the same route/provider/wall budgets, and the hint is removed only after a real screen turn
