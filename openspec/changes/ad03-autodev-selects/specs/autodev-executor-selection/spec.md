# Spec Delta: `verdict autodev` selects its own executor

## ADDED Requirements

### Requirement: Autodev SHALL select a free executor when no model is pinned
`verdict autodev <objective> --repo <path> --allow-live` without
`--executor-model` SHALL select its executor from the live eligibility ladder:
routes that reached TASK_ELIGIBLE or SELECTED and are FREE by account evidence
or by their `:free` route id, ordered by the ladder rank, at most the top three.
It SHALL probe each of those routes with the same served-identity, exact-content
and zero-response-cost checks the pinned path uses, and SHALL offer every route
that answered correctly to `verdict.execution_path.optimize_execution_path` as
its own candidate in ladder order, so the decision carries `why_selected` and
the losing candidates in `rejected`. The selected route SHALL become the
executor and the orchestrator model.

#### Scenario: Two free routes answer correctly
- **WHEN** the ladder ranks two task-eligible free routes and both probes return the exact served identity at zero cost
- **THEN** both are offered in rank order, the optimizer selects one, and the decision names the other in `rejected`

#### Scenario: A ranked route fails its probe
- **WHEN** the highest-ranked free route fails the content, identity or zero-cost check
- **THEN** that route is dropped with its reason and the next route that answered correctly is selected

#### Scenario: Provider names do not decide the order
- **WHEN** the ladder is built for selection
- **THEN** no provider family is preferred by name, so capacity class, capability slack, price and measured quota decide the rank

### Requirement: Autodev SHALL NOT fall back to a paid or frontier route
Without an eligible free route that answers its probe correctly, autodev SHALL
fail with an explicit error before planning and SHALL launch nothing. The error
SHALL NOT offer a paid or frontier route as an alternative, and when routes were
probed it SHALL name each dropped route with its reason.

#### Scenario: No eligible free route
- **WHEN** the ladder offers only subscription or metered routes, or every free route failed an earlier stage
- **THEN** the command raises an error stating there is no paid or frontier fallback, sends no probe and plans nothing

#### Scenario: Every probed free route fails
- **WHEN** all probed free routes fail the identity, content or zero-cost check
- **THEN** the command raises an error that lists each dropped route with its reason

### Requirement: The selection SHALL be visible in the report and the receipt
The autodev outcome receipt SHALL record `execution_path_why_selected`,
`execution_path_rejected` and `executor_probe_dropped` beside
`execution_path_selected_route`, unredacted. The `--output-json` report SHALL
include `executor_selection` holding `why_selected`, the selected route id, the
rejected candidates, the probed route ids and the routes dropped at probe time
with their reasons, and the human summary SHALL name the selected route. A route
that failed its probe SHALL stay visible when another route survives.

#### Scenario: Reading the receipt back
- **WHEN** an unpinned autodev run finishes a unit and the receipt store is reopened
- **THEN** the receipt holds a non-empty `execution_path_why_selected` and the dropped candidate id in `execution_path_rejected`, neither redacted

#### Scenario: Reading the JSON report
- **WHEN** an unpinned autodev run is reported with `--output-json`
- **THEN** `executor_selection.why_selected` is non-empty and `executor_selection.selected` is the route the optimizer chose

#### Scenario: A probe drop stays visible next to a survivor
- **WHEN** one ranked free route fails its probe and a later one is selected
- **THEN** the dropped route and its reason appear in `executor_selection.dropped` and in the receipt's `executor_probe_dropped`
