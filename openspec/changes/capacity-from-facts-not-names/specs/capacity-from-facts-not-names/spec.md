## ADDED Requirements

### Requirement: Capacity comes from facts, never from names

A route's capacity class SHALL be decided only from declared facts (explicit pricing, the OpenRouter
public 0/0 set, the gateway's free-tier membership, connection free signals, oauth subscription) or
observations (billing 402 overrides). A model name, a `:free` suffix, a provider prefix or a
provider-name list SHALL NOT make a route FREE or not-FREE. With no such signal the class SHALL be
`UNKNOWN` with evidence `no_free_evidence`.

#### Scenario: A free-looking name with no facts
- **WHEN** a route id ends in `:free` or its provider name sounds free or is a prepaid-credit provider, and no fact is present
- **THEN** its capacity class is `UNKNOWN` and evidence is `no_free_evidence`

#### Scenario: A declared zero price
- **WHEN** the route's inventory price is explicitly 0 for input and output
- **THEN** its capacity class is `FREE` and evidence is `all_zero_pricing`, whatever its name

#### Scenario: Gateway free-tier membership
- **WHEN** the gateway's free-tier summary lists the route
- **THEN** its capacity class is `FREE` and evidence is `gateway_free_tier`

### Requirement: Preflight route cap

The certification preflight SHALL screen at most 30 routes, one per backend pool.

#### Scenario: Preflight arguments
- **WHEN** the rehearsal runs its preflight
- **THEN** the identify screen receives `--max-routes 30 --max-per-pool 1 --capacity free`
