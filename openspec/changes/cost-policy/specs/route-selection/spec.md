## ADDED Requirements

### Requirement: Cost order comes from the operator's policy

Selection SHALL rank capacity classes in the order set by the operator's cost policy. No module SHALL keep its own fixed order. Picking a route while an admitted route of a class earlier in the policy order remains SHALL raise. A policy that fails validation SHALL stop the command with the failing key named, and SHALL never fall back to a built-in order silently.

#### Scenario: Operator puts unknown before subscription

- **GIVEN** a policy with order `free, unknown, subscription, metered`
- **WHEN** an admitted `UNKNOWN` route and an admitted `SUBSCRIPTION` route both clear a worker node's requirements, and no `FREE` route does
- **THEN** the `UNKNOWN` route is selected first

#### Scenario: Default policy keeps today's order

- **WHEN** no cost policy is configured
- **THEN** the order is `free, subscription, metered, unknown`, and selection matches the pre-change behaviour

### Requirement: Unknown-cost mode decides how routes without cost facts are used

A route with no declared or observed cost fact SHALL keep the class `UNKNOWN`. Under `assume_free` it SHALL be admitted on the same evidence as a `FREE` route. Under `probe_only` it SHALL be admitted only after a consented, budgeted probe succeeded with no observed charge. Under `exclude` it SHALL never be a candidate. A zero cost header or a zero call-log cost SHALL NOT count as a cost fact.

#### Scenario: assume_free route without tool proof

- **WHEN** the mode is `assume_free` and an `UNKNOWN` route has no tool proof
- **THEN** it is not admitted as an implementation worker

#### Scenario: Zero cost header

- **WHEN** an `UNKNOWN` route's response carries a zero `x-omniroute-response-cost`
- **THEN** the route stays `UNKNOWN`

### Requirement: Unknown routes are capped before their first cost fact

An `UNKNOWN` route SHALL be skipped, with the reason `unknown_daily_cap`, once its dispatched calls in the current UTC day reach the policy cap. The cap SHALL stop applying once a cost fact classifies the route.

#### Scenario: Cap reached

- **WHEN** an `UNKNOWN` route has reached the daily cap
- **THEN** it is skipped with `unknown_daily_cap`, and the receipt shows it

### Requirement: Subscription reserve protects quota for planning and review

With the reserve on, a worker node SHALL NOT use a subscription route whose last quota reading is below the reserve threshold. Planner and reviewer roles MAY still use it. An unreadable quota, including a `null` percentage, SHALL NOT trigger the reserve, and the receipt SHALL record `reserve_unreadable`.

#### Scenario: Unknown quota percentage

- **WHEN** the quota reading for a subscription route reports `percentRemaining: null`
- **THEN** the reserve does not exclude the route and the receipt records `reserve_unreadable`

### Requirement: Receipts record the cost policy

Each selection receipt SHALL record the cost policy version, the selected route's cost class and deciding fact, and for an `UNKNOWN` route the mode that admitted it.

#### Scenario: Receipt after a selection

- **WHEN** a route is selected
- **THEN** its receipt includes `cost_policy_version` and `cost_class`

## MODIFIED Requirements

### Requirement: Assumed-free capacity is not capability proof
Routes with a connected account and no declared or observed cost fact SHALL be `UNKNOWN` with evidence rule `no_free_evidence`, and the operator's unknown-cost mode SHALL decide how they are used. When the mode admits them, they SHALL pass the same tool proof bar as every free worker.

#### Scenario: Missing price and missing tool screen
- **WHEN** a connected API-key route has no price and no tool screen
- **THEN** it is classified `UNKNOWN` and is not eligible as an implementation worker
