# Spec Delta: Admission reads discovery-registry proof (S3b)

## Context

Before this change, the `EligibilityLadder` agentic gate rejected any FREE
route that had no fresh `agentic_ok` proof in the health cache with the reason
`no_agentic_probe`.  The discovery registry (written by `verdict discovery
refresh`, added in the `discovery-prober-registry` change) records a fresh
`tool_ok` measurement for each probed route, but the admission ladder did not
consult it.  This caused approximately 3,000 free routes to be rejected for
`no_agentic_probe` even when the registry held a live `tool_ok=True` proof
(BOD-348 / issue #1168).

## ADDED Requirements

### Requirement: EligibilityLadder SHALL accept registry tool_ok proof for FREE routes

When the `EligibilityLadder` agentic gate would otherwise return
`no_agentic_probe` for a FREE route, it SHALL check the discovery registry for
a qualifying `tool_ok` proof before rejecting.

A registry proof qualifies only when ALL of the following hold:

1. The route's capacity class is `CapacityClass.FREE` (not UNKNOWN, not PRICED,
   not SUBSCRIPTION).
2. A `RegistryEntry` exists for the canonical route key (gateway prefix
   `omniroute/` stripped).
3. `entry.capacity_class == "free"`.
4. `entry.answered is True`.
5. `entry.tool_ok is True`.
6. `entry.served_model` is not `None` and equals the model segment of the
   canonical route key.
7. `entry.checked_at` is timezone-aware, non-future, and within
   `registry.ttl_s` seconds of the `now` passed to `evaluate()`.

A route that passes all seven checks is admitted as if it had a live agentic
probe.  Any failed check falls through to the original `no_agentic_probe`
rejection.

#### Scenario: Fresh registry proof admits a FREE route

- **GIVEN** a FREE route with no health-cache entry
- **AND** the discovery registry holds an entry for that route with
  `tool_ok=True`, `answered=True`, a matching `served_model`, and
  `checked_at` within the registry TTL
- **WHEN** `EligibilityLadder.evaluate()` is called
- **THEN** the route is ranked (not rejected with `no_agentic_probe`)

#### Scenario: Expired registry entry does not admit

- **GIVEN** a FREE route with no health-cache entry
- **AND** the registry entry's `checked_at` is more than `ttl_s` seconds
  before the evaluation `now`
- **WHEN** `EligibilityLadder.evaluate()` is called
- **THEN** the route is rejected with reason `no_agentic_probe`

#### Scenario: tool_ok=None (not probed) does not admit

- **GIVEN** a registry entry where `tool_ok` is `None`
- **WHEN** the agentic gate checks this entry
- **THEN** the route is rejected with reason `no_agentic_probe`
  (no measurement was made — the entry is a placeholder, not proof)

#### Scenario: tool_ok=False (measured fail) does not admit

- **GIVEN** a registry entry where `tool_ok` is `False`
- **WHEN** the agentic gate checks this entry
- **THEN** the route is rejected with reason `no_agentic_probe`

#### Scenario: answered=False does not admit

- **GIVEN** a registry entry where `answered` is `False`
- **WHEN** the agentic gate checks this entry
- **THEN** the route is rejected with reason `no_agentic_probe`

### Requirement: UNKNOWN routes SHALL NOT be admitted via registry proof

The registry `tool_ok` proof only licenses FREE routes.  An UNKNOWN route
MUST NOT be promoted to admitted based on a registry entry, regardless of
`tool_ok` value.  The admission invariant "unknown stays unknown" is
preserved.

#### Scenario: UNKNOWN route with registry proof stays blocked

- **GIVEN** a route with no capacity facts (classified `UNKNOWN`)
- **AND** the registry holds a `tool_ok=True` entry for that route
- **WHEN** `EligibilityLadder.evaluate()` is called
- **THEN** the route is rejected; `capacity_class` remains `UNKNOWN`

### Requirement: Gateway prefix SHALL be stripped before registry lookup

Route IDs in the ladder may carry an `omniroute/` gateway prefix; registry
`route_key` values never do.  The lookup MUST strip `omniroute/` before
searching the registry so the entry is found correctly.

#### Scenario: Prefixed route id finds unprefixed registry entry

- **GIVEN** a registry entry with `route_key = "free/model-a"`
- **WHEN** `tool_ok_proof("omniroute/free/model-a", registry, now)` is called
- **THEN** the entry is found and the result is `qualified=True`

### Requirement: tool_ok_proof SHALL be a pure function and SHALL NOT raise

`tool_ok_proof(route_id, registry, now)` is a pure, I/O-free function.  Any
corrupt or unexpected registry entry MUST return `{"qualified": False, ...}`
rather than propagating an exception.  A buggy entry never admits; it falls
back to `no_agentic_probe`.

#### Scenario: Absent registry returns qualified=False without raising

- **GIVEN** `tool_ok_proof` is called with `registry=None`
- **THEN** the return value is `{"qualified": False, "reason": "registry_absent", ...}`
  and no exception is raised

#### Scenario: Corrupt or unexpected entry returns qualified=False without raising

- **GIVEN** a registry entry with an invalid or malformed `checked_at` timestamp
- **WHEN** `tool_ok_proof` is called with that entry in the registry
- **THEN** `qualified` is `False` and no exception propagates to the caller

### Requirement: Funnel tool_ok stage SHALL report registry_proof count

When `build_funnel` is called with a non-`None` `registry` argument, the
returned `tool_ok` stage MUST include a `"registry_proof"` key whose value is
the count of registry entries with `capacity_class == "free"` and a qualifying
fresh `tool_ok=True` proof.  This count is advisory; it names routes eligible
for admission via registry proof even without a health-cache agentic probe.

#### Scenario: Funnel shows registry_proof count

- **GIVEN** `build_funnel` is called with a `registry` containing two FREE
  entries with fresh `tool_ok=True` and one entry with `tool_ok=None`
- **THEN** `result["stages"]["tool_ok"]["registry_proof"]` equals 2

#### Scenario: Funnel without registry omits registry_proof key

- **GIVEN** `build_funnel` is called without a `registry` argument
- **THEN** `result["stages"]["tool_ok"]` does not contain `"registry_proof"`
