# Spec Delta: generic-harness-contracts

## MODIFIED Requirements

### Requirement: Registry resolves harnesses without Core branching

`HarnessRegistry` SHALL resolve adapters by name (and declared aliases)
without `if prime / elif claude` branching in Core. CLI executor selection,
eligibility visibility and receipt labels SHALL all resolve through the
registry. The registry default worker executor SHALL be `direct-gateway`;
Prime Agent (`prime-headless`, alias `prime`) SHALL be an optional harness
selected explicitly. Eligibility visibility SHALL follow the selected executor,
and when none is given, the registry default.

#### Scenario: Default dispatch through the registry

- **WHEN** `verdict orchestrate` runs without `--executor`
- **THEN** the worker executor is built via
  `DEFAULT_REGISTRY.executor_for(ExecutionTarget(harness="direct-gateway"))`
- **TEST** `tests/test_orch_executors.py::test_default_executor_builds_direct_gateway_not_prime`

#### Scenario: Prime dispatch through the registry

- **WHEN** `verdict orchestrate` runs with `--executor prime-headless` or `--executor prime`
- **THEN** the worker executor is built via
  `DEFAULT_REGISTRY.executor_for(ExecutionTarget(harness="prime-headless"))` and receipts carry `harness="prime-headless"` exactly as before
- **TEST** `tests/test_orch_executors.py::test_explicit_prime_executor_still_builds_prime`

#### Scenario: Default visibility reads the live catalog

- **WHEN** a route is in the live gateway catalog but not in Prime's registry and no executor is named
- **THEN** the route is harness-visible; with `prime-headless` selected it is refused as `not_harness_visible`
- **TEST** `tests/test_certification_executor_visibility.py::test_default_visibility_sees_live_routes_missing_from_the_prime_registry`

#### Scenario: Mixed executor map keeps strict visibility

- **WHEN** `--executor-map` or `--executor` sends any node to Prime
- **THEN** the eligibility ladder uses Prime's strict visibility for the whole run; a map with no Prime backend uses the `--executor` visibility
- **TEST** `tests/test_certification_executor_visibility.py::test_selector_visibility_executor_preserves_mixed_map_rule`

## ADDED Requirements

### Requirement: Live acceptance treats Prime as optional

The hourly live acceptance job SHALL report the `harness-prime` check as
skipped, with the reason `Prime Agent not installed (optional harness)`, when
the Prime Agent binary is absent or its Verdict harness is not enabled. A
skipped check SHALL NOT count as a failure and SHALL NOT be reported as
passed. When Prime is installed and enabled, the check SHALL run as before and
MAY fail.

#### Scenario: Prime absent

- **WHEN** no Prime Agent binary is on PATH
- **THEN** `harness-prime` is skipped with that reason, `passed` is false, and the run can be green
- **TEST** `tests/test_acceptance.py::test_prime_absent_is_skipped_never_passed_and_keeps_acceptance_green`,
  `::test_prime_installed_but_not_enabled_is_skipped`,
  `::test_red_doctor_issue_never_names_a_skipped_optional_check`

#### Scenario: Prime installed and enabled

- **WHEN** Prime is installed and enabled but not pointed at `verdict serve`
- **THEN** `harness-prime` fails and the run is red
- **TEST** `tests/test_acceptance.py::test_prime_installed_and_enabled_still_runs_and_can_fail`
