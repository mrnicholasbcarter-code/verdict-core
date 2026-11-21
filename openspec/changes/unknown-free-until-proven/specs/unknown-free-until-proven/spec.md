# Spec Delta: UNKNOWN routes are free-tier until a probe charge proves otherwise

## Context

Verdict classifies routes without declared pricing or connection-level free
signals as `UNKNOWN` capacity.  Before this change, UNKNOWN routes were never
probed and never admitted via the discovery registry path, blocking ~2,135
potentially-free routes.  Operator decision D31 (2026-11-21) changes admission
policy: UNKNOWN is free-tier until a probe observation proves it costs money.

## MODIFIED Requirements

### Requirement: Prober SHALL probe FREE, SUBSCRIPTION, and UNKNOWN routes

**Amends** `discovery-prober-registry` / "Prober SHALL only probe FREE and
SUBSCRIPTION routes".

`verdict discovery refresh` SHALL probe routes whose capacity class is `"free"`,
`"subscription"`, or `"unknown"`.  Routes classified `"metered"` or in the
discovery METERED-override file MUST NOT be probed.

`verdict discovery refresh --free-only` SHALL include `"unknown"` routes
alongside `"free"` routes; `"subscription"` routes are still excluded.

#### Scenario: UNKNOWN route is probed in free-only mode

- **GIVEN** a route whose capacity_class is `"unknown"`
- **WHEN** `verdict discovery refresh --free-only` is called
- **THEN** an HTTP probe is made to that route and it may appear in the registry

#### Scenario: SUBSCRIPTION route is excluded from free-only mode

- **GIVEN** a route whose capacity_class is `"subscription"`
- **WHEN** `verdict discovery refresh --free-only` is called
- **THEN** no HTTP probe is made to that route

#### Scenario: Metered-override route is never re-probed

- **GIVEN** a route that appears in `$VERDICT_HOME/discovery/metered.json`
- **WHEN** `verdict discovery refresh` is called with any flags
- **THEN** no HTTP probe is made to that route

### Requirement: EligibilityLadder SHALL accept registry tool_ok proof for FREE and UNKNOWN routes

**Amends** `admission-reads-registry-proof` / "EligibilityLadder SHALL accept
registry tool_ok proof for FREE routes".

A registry proof qualifies when ALL of the original seven conditions hold, with
condition 1 expanded:

1. The route's capacity class is `CapacityClass.FREE` **or**
   `CapacityClass.UNKNOWN`.  (SUBSCRIPTION, METERED and any other class never
   qualify.)
3. `entry.capacity_class in {"free", "unknown"}`.

#### Scenario: Fresh UNKNOWN registry proof admits an UNKNOWN route

- **GIVEN** an UNKNOWN route with no health-cache entry
- **AND** the discovery registry holds an entry with `capacity_class="unknown"`,
  `tool_ok=True`, `answered=True`, a matching `served_model`, and `checked_at`
  within the registry TTL
- **WHEN** `EligibilityLadder._task_gate` is called
- **THEN** the route passes (empty reason returned), not `no_agentic_probe`

#### Scenario: UNKNOWN route without registry entry is not admitted

- **GIVEN** an UNKNOWN route with no health-cache entry
- **AND** no registry entry exists for the route
- **WHEN** `EligibilityLadder._task_gate` is called
- **THEN** the route is rejected with `no_agentic_probe`

## ADDED Requirements

### Requirement: A non-zero probe charge SHALL write a permanent METERED override

When a liveness probe of any route returns HTTP 200 AND the
`x-omniroute-response-cost` response header carries a value greater than 0, the
system SHALL:

1. Write the route's canonical key (gateway prefix stripped) to
   `$VERDICT_HOME/discovery/metered.json` with
   `evidence="discovery_observed_charge"` and the observed cost in USD.
2. Skip the tool-probe stage for that route in the same refresh run.
3. Return `CapacityClass.METERED` with evidence `"discovery_observed_charge"`
   from `EligibilityLadder._capacity_class` on all future selections.

The override file has no TTL; a route written to it is never probed or admitted
again unless the file is manually edited.

#### Scenario: Charged probe writes metered.json

- **GIVEN** an UNKNOWN route whose liveness probe returns HTTP 200
- **AND** the response carries `x-omniroute-response-cost: 0.00042`
- **WHEN** `run_refresh` processes the probe result
- **THEN** `$VERDICT_HOME/discovery/metered.json` contains an entry for that
  route with `evidence="discovery_observed_charge"` and
  `observed_cost_usd=0.00042`

#### Scenario: Metered-override route classified METERED in admission

- **GIVEN** a route present in `$VERDICT_HOME/discovery/metered.json`
- **WHEN** `EligibilityLadder._capacity_class` is called for that route
- **THEN** `CapacityClass.METERED` is returned with evidence
  `"discovery_observed_charge"`
