# Spec Delta: autodev pinned METERED executor

## ADDED Requirements

### Requirement: Selection sweeps SHALL exclude fact-METERED routes; UNKNOWN routes SHALL pass

The `build_selector` capacity filter SHALL narrow out routes whose capacity class
is `METERED` (fact-proven pay-per-token) from the admission set before the
selection sweep. Routes whose capacity class is `UNKNOWN` (no billing facts)
SHALL pass through (ADR-049 `assume_free`). The narrowing SHALL use a no-probe
capacity classifier that reads billing facts without issuing a probe, consistent
with the admission-narrowing contract (ADR-001).

#### Scenario: Fact-METERED route in the admitted set
- **WHEN** a route has a declared or observed billing fact that makes it `METERED`
- **THEN** `build_selector` narrows it out of the admitted set at POLICY stage with reason `outside_capacity_filter` before any probe is issued in the selection sweep

#### Scenario: UNKNOWN route in the admitted set
- **WHEN** a route has no billing facts and its capacity class is `UNKNOWN`
- **THEN** it passes the capacity filter and may be probed in the selection sweep (assume_free, ADR-049)

#### Scenario: METERED route is never probed in unpinned selection
- **WHEN** unpinned `verdict autodev --allow-live` runs and the ladder contains a fact-METERED route alongside eligible free or subscription routes
- **THEN** the METERED route is not probed, and zero `x-omniroute-*` probe headers are recorded for it

### Requirement: A METERED executor pinned by the operator SHALL be allowed, probed, and recorded

When `verdict autodev --allow-live --executor-model <route>` is called and the
named route is fact-METERED, autodev SHALL NOT fail closed. It SHALL probe the
route, bypass the zero-cost check (pay-per-token routes report non-zero cost by
definition), record `x-verdict-route-capacity: metered` in the returned probe
headers, and record `pinned_route_capacity_class: "metered"` in the receipt and
in `AutodevReport`.

This is an explicit operator choice; the operator takes responsibility for the
spend. CP1 (#1244) adds the spend-limit policy and the automatic tiered fallback
ladder (free → subscription → paid); those are out of scope here.

#### Scenario: METERED pinned executor probe
- **WHEN** `verdict autodev --allow-live --executor-model <metered-route>` is called
- **THEN** the route is probed; a non-zero `x-omniroute-response-cost` does not drop it; `x-verdict-route-capacity: metered` appears in the returned headers

#### Scenario: pinned_route_capacity_class in receipt and report
- **WHEN** the METERED pinned route's probe succeeds and a run completes
- **THEN** the receipt payload includes `pinned_route_capacity_class: "metered"` and `AutodevReport.pinned_route_capacity_class` equals `"metered"` in both `to_dict()` and `summary()`

#### Scenario: probe_headers in receipt are gateway-only
- **WHEN** the probe succeeds and the candidate's probe_headers field is written to the receipt
- **THEN** it contains only real gateway `x-omniroute-*` headers; the synthetic `x-verdict-route-capacity` key is NOT present in the receipt's probe_headers

### Requirement: The planner path SHALL continue to exclude METERED routes

`_live_planner_models` SHALL return only FREE or SUBSCRIPTION routes, as before.
An explicit `--orchestrator-model` that is fact-METERED SHALL be refused with a
named error before planning and SHALL NOT trigger a probe.

#### Scenario: METERED --orchestrator-model
- **WHEN** `verdict autodev --dry-run --orchestrator-model <metered-route>` is called
- **THEN** autodev refuses with a named error including `capacity=metered` and sends nothing

#### Scenario: Planner ladder excludes METERED
- **WHEN** `_live_planner_models` runs and the ladder contains only METERED routes with reasoning+coding+frontier_worthy capabilities
- **THEN** no planner is returned and planning fails closed with a named error
