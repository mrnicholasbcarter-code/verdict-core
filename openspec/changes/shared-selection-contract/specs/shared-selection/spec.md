# Spec Delta

## Purpose

States the selection contract every Verdict entrypoint must meet: one selector, a fixed tier order under
the cheapest-qualified-tier rule, no critical-path exception, and a receipt that names what happened to
free supply.

## ADDED Requirements

### Requirement: One selector across the entrypoints

`verdict choose`, `verdict route`, the serve candidate order, and `verdict orchestrate` SHALL name the
same first route for the same task and the same evidence. The first route SHALL be the first entry of the
ordering produced by the one live selection function, driven by the same admission ladder, under the
existing capability, proof, confirmation and authority gates. `--allow-legacy-selector` (or its
equivalent explicit request-context flag) SHALL be the only way a surface may be served by a different
selector, and it SHALL be explicit per request, never a silent default and never inferred.

#### Scenario: Four surfaces agree on the first route

- **GIVEN** one task, one evidence set with fresh proof, and no legacy-selector flag on any surface
- **WHEN** `verdict choose`, `verdict route`, a serve request for the same task, and `verdict orchestrate`
  select a route
- **THEN** all four name the same first route, and no surface invents a second ordering for the same task

#### Scenario: Legacy selector requires an explicit ask

- **WHEN** a caller does not set `--allow-legacy-selector` and the execution-path authority is required
- **THEN** the surface must not fall back to a legacy selector route and must fail closed with a named
  reason instead of returning a route chosen by a different selector

#### Scenario: Legacy selector is explicit and still recorded

- **WHEN** a caller sets `--allow-legacy-selector` explicitly
- **THEN** the legacy selector may serve that one request, and the result records that the legacy selector
  was used so it cannot be mistaken for shared-selector authority

### Requirement: Tier order is free, then subscription by observed usage, then metered

Selection SHALL exhaust capable free routes before considering subscription routes. Subscription routes
SHALL be ordered by observed token consumption for the task family — the median of accepted checked
attempts in that family, on the existing minimum of three unique samples, with unknown usage ordered after
measured usage — and never by remaining quota or estimated quota headroom. Permitted metered routes SHALL
be ordered after every capable free and subscription route. An explicit free-only caller SHALL remain
free-only: a caller-supplied capacity narrow may remove candidates and SHALL never add or widen one.

#### Scenario: A capable free route wins over a capable subscription route

- **GIVEN** a free route and a subscription route that both clear the same task requirements and proof bar
- **WHEN** the selector returns its first route
- **THEN** the first route is the free route, and the subscription route is ordered behind it

#### Scenario: Subscription order follows observed tokens, not quota

- **GIVEN** two capable subscription routes, one with at least three accepted same-family attempts and a
  known token median, and one with fewer samples
- **WHEN** the selector orders subscription candidates
- **THEN** the route with the lower measured token median is ordered first, a route without enough samples
  is ordered after measured routes, and remaining quota does not affect the order

#### Scenario: Metered is last

- **GIVEN** a capable free route, a capable subscription route and a permitted metered route, each clearing
  the task's gates with no active payment-required negative on the metered route
- **WHEN** the selector orders the candidates
- **THEN** the metered route is ordered after the free and subscription routes

#### Scenario: An explicit free-only caller stays free-only

- **GIVEN** a caller that admits only free capacity
- **WHEN** no free route clears the task's gates but a subscription route does
- **THEN** selection fails closed with a named reason and no subscription route is selected or sent

### Requirement: A frontier-worthy task gets a higher floor, not a tier exception

A task marked frontier-worthy SHALL raise the capability floor its candidates must clear. It SHALL NOT
skip a free route that clears that raised floor, and it SHALL NOT privilege a subscription route on the
grounds that the task is on the critical path. Existing worker and self-build capacity assignment is
unchanged and grants no paid authority.

#### Scenario: A free route clears the frontier floor

- **GIVEN** a frontier-worthy task whose raised capability floor is cleared by both a free route and a
  subscription route
- **WHEN** the selector returns its first route
- **THEN** the first route is the free route, and no critical-path exception substitutes the subscription
  route

#### Scenario: Free routes do not clear the frontier floor

- **GIVEN** a frontier-worthy task where no free route clears the raised capability floor and a subscription
  route does
- **WHEN** the selector returns its first route
- **THEN** the subscription route may be selected, and the receipt records that free routes failed the
  capability floor rather than that they were skipped to save time

### Requirement: Selection receipts name what happened to free supply

A selection receipt SHALL distinguish and name: eligible free routes that were attempted, routes known to
be unavailable, supply that was found but did not meet task qualification, and eligible routes left
unattempted because the caller's authority expired. The existing termination reasons `budget_exhausted`
and `free_supply_unknown` SHALL keep their current names and SHALL NOT be replaced by, or merged into, the
new classes.

#### Scenario: Free routes are attempted

- **GIVEN** two eligible free routes and a selection that sent to both
- **WHEN** the receipt is read
- **THEN** it names both attempted free routes as eligible free routes attempted

#### Scenario: A free route is known unavailable

- **GIVEN** a free route with a current explicit denial or cooldown
- **WHEN** the receipt is read
- **THEN** that route is named as known unavailable, and it is not counted as attempted or as unattempted
  supply

#### Scenario: Free supply exists but is unqualified

- **GIVEN** a free route that was discovered and does not meet this task's qualification
- **WHEN** the receipt is read
- **THEN** that route is named as unqualified supply, distinguishable from a route that was never found

#### Scenario: Caller authority expires before an eligible route is attempted

- **GIVEN** an eligible free route that remained unattempted because the caller's attempt or spend authority
  expired
- **WHEN** the receipt is read
- **THEN** that route is named as an eligible route left unattempted for expired caller authority, and it
  is not reported as unavailable or unqualified

#### Scenario: Attempt budget runs out

- **GIVEN** eligible free routes that consumed the caller's attempt budget
- **WHEN** the receipt is read
- **THEN** the termination reason is `budget_exhausted`, and it is not `free_supply_unknown`
