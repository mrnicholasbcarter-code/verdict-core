# generic-harness-contracts Specification

## Purpose

Verdict orchestration resolves worker harnesses through a versioned
`ExecutionTarget` / `HarnessAdapter` / `HarnessRegistry` contract layer so
Core no longer branches on Prime-specific names. Harness identity is
independent of transport/provider/model identity; adapters translate
Verdict decisions into launches and never rank models or broaden admission.

## Requirements

### Requirement: Versioned ExecutionTarget separates harness from route identity

Core SHALL provide a versioned `ExecutionTarget` (`verdict.harness/v1`) whose
`harness`, `transport`, `provider`, `model`, `capacity_class`,
`reasoning_effort` and `compatibility_profile` are disjoint fields. Harness
identity SHALL NOT imply provider or model identity.

#### Scenario: Disjoint identity fields

- **WHEN** an `ExecutionTarget` is constructed for a Prime worker
- **THEN** `harness`, `provider` and `model` are independent fields and an
  architecture test proves no production code derives provider/model from
  the harness name

### Requirement: Explicit capability contract

Every registered `HarnessAdapter` SHALL declare a `HarnessCapabilities`
report enumerating every `HarnessCapability` facet with a `CapabilityLevel`.
Registration SHALL reject an adapter that omits any facet. Unsupported
capabilities SHALL be explicit (`unsupported` / `not-installed`), never
inferred or silently emulated.

#### Scenario: Adapter missing a facet is rejected

- **WHEN** an adapter's `capabilities()` omits any `HarnessCapability`
- **THEN** `HarnessRegistry.register` raises `OmittedCapabilityError`

### Requirement: Registry resolves harnesses without Core branching

`HarnessRegistry` SHALL resolve adapters by name (and declared aliases)
without `if prime / elif claude` branching in Core. CLI executor selection,
eligibility visibility and receipt labels SHALL all resolve through the
registry.

#### Scenario: Prime dispatch through the registry

- **WHEN** `verdict orchestrate` runs with the default executor
- **THEN** the worker executor is built via
  `DEFAULT_REGISTRY.executor_for(ExecutionTarget(harness="prime-headless"))`
  and receipts carry `harness="prime-headless"` exactly as before

#### Scenario: Mixed executor map keeps strict visibility

- **WHEN** `--executor-map` routes any node to a different backend
- **THEN** the eligibility ladder uses the default adapter's strict
  visibility regardless of the `--executor` value, matching prior behavior

### Requirement: Registry is not Prime-specific

A non-Prime test adapter registered through `HarnessRegistry` SHALL be able
to drive a bounded node through the same contract, proving the registry and
adapter boundary do not depend on Prime.

#### Scenario: Fake harness drives a bounded node

- **WHEN** a `FakeHarnessAdapter` is registered and resolved
- **THEN** its worker executor runs a bounded node offline and the run
  completes without any Prime-specific code path
