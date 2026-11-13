# Spec Delta: autodev executor ladder

## ADDED Requirements

### Requirement: The executor ladder SHALL count admitted routes, not attempted routes
Unpinned `verdict autodev --allow-live` SHALL walk the task-eligible free or subscription routes in ladder rank order, probing one route at a time. It SHALL stop when three routes have passed their probe, when eight probes have been sent, or when no eligible route is left, whichever comes first. A route that fails its probe SHALL be recorded with its reason and SHALL NOT count toward the three. A model family SHALL be probed at most once in a selection. Metered routes, routes in an excluded pool and premium-reserved models SHALL never be probed.

#### Scenario: Higher-ranked routes fail their probe
- **WHEN** the ladder ranks four eligible routes from four model families and the first two fail their probe
- **THEN** the third and fourth are probed, both are candidates in rank order, and both failures are listed with their reasons

#### Scenario: The walk stops at three admitted routes
- **WHEN** five eligible routes from five model families would all pass their probe
- **THEN** only the first three by rank are probed and offered

#### Scenario: One probe per model family
- **WHEN** a route has been probed and a lower-ranked eligible route belongs to the same model family
- **THEN** the lower-ranked route is not probed, whether the first probe passed or failed

#### Scenario: The walk stops at eight probes
- **WHEN** ten eligible routes from ten model families all fail their probe
- **THEN** exactly eight are probed and the command fails before planning, naming each probed route with its reason

### Requirement: The executor probe SHALL accept the served model in either form the gateway reports
A probed route SHALL be accepted only when the gateway's served-model header equals the selected route id without its first segment, or equals the whole selected route id. A missing header or any other value SHALL drop the route with reason `identity`. A pinned `--executor-model` probe SHALL apply the same rule and SHALL refuse before planning when it does not hold.

#### Scenario: Provider-prefixed model header
- **WHEN** route `nvidia/nemotron-3.5-super-vl-120b-a12b` is probed and the served-model header is `nvidia/nemotron-3.5-super-vl-120b-a12b`
- **THEN** the route is a candidate

#### Scenario: Provider-local model header
- **WHEN** route `devin-desktop/swe-2-flash-medium` is probed and the served-model header is `swe-2-flash-medium`
- **THEN** the route is a candidate

#### Scenario: Another model served
- **WHEN** the served-model header names a different model
- **THEN** the route is dropped with reason `identity`

#### Scenario: No served-model header
- **WHEN** the response carries no served-model header
- **THEN** the route is dropped with reason `identity`

#### Scenario: Pinned probe
- **WHEN** a pinned route is probed and the served-model header names another model
- **THEN** the command fails before planning and launches nothing

### Requirement: Autodev selection SHALL exclude the reserved pools in its ladder requests
The requests unpinned selection sends to the eligibility ladder, for executor routes and for planner routes, SHALL name the `claude` and `codex` credential pools as excluded, so the ladder neither refreshes nor confirms a route that autodev can never use.

#### Scenario: Executor ladder request
- **WHEN** unpinned selection asks the ladder for executor routes
- **THEN** the request excludes the pools `claude` and `codex`

#### Scenario: Planner ladder request
- **WHEN** unpinned selection asks the ladder for planner routes
- **THEN** the request excludes the pools `claude` and `codex`
