# Spec Delta

## Purpose

Provides typed, versioned decision signal abstraction for Verdict intelligence signals. DecisionSignalProvider protocol never raises; failures return as signals with failure_class set.

## ADDED Requirements

### Requirement: DecisionSignalSetV1 SHALL validate all constraints

DecisionSignalSetV1 SHALL reject:
- Unknown fields
- NaN or inf values
- Probabilities outside [0,1]
- Wrong schema_version (not "decision-signals/v1")
- Missing required fields

SHALL raise typed error on validation failure.

#### Scenario: Valid signal set round-trips
- **WHEN** valid DecisionSignalSetV1 is converted to dict
- **THEN** from_dict(to_dict(signal_set)) == signal_set

#### Scenario: Probability outside [0,1]
- **WHEN** DecisionSignalSetV1.from_dict called with complexity=1.5
- **THEN** raises validation error

#### Scenario: NaN or inf value
- **WHEN** DecisionSignalSetV1.from_dict called with confidence=float('nan')
- **THEN** raises validation error

#### Scenario: Unknown field
- **WHEN** DecisionSignalSetV1.from_dict called with extra_field=123
- **THEN** raises validation error

#### Scenario: Wrong schema_version
- **WHEN** DecisionSignalSetV1.from_dict called with schema_version="wrong"
- **THEN** raises validation error

### Requirement: DecisionSignalProvider SHALL never raise

DecisionSignalProvider.signals(question, *, now) SHALL return DecisionSignalSetV1 for all inputs, including:
- Confident response
- Uncertain response (low confidence)
- Provider timeout
- Provider unavailable
- Malformed response
- Key missing

Failures SHALL return DecisionSignalSetV1 with failure_class set and signals=None.

#### Scenario: Confident provider response
- **WHEN** provider returns confident signals
- **THEN** DecisionSignalSetV1 has signals populated, failure_class=None

#### Scenario: Provider timeout
- **WHEN** provider times out
- **THEN** DecisionSignalSetV1 has failure_class=TIMEOUT, signals=None

#### Scenario: Missing API key
- **WHEN** provider instantiated without API key
- **AND** signals() called
- **THEN** DecisionSignalSetV1 has failure_class=UNKNOWN, signals=None, no exception raised

### Requirement: JSON Schema SHALL match Python dataclass

JSON Schema at schemas/decision-signals-v1.json SHALL have parity with DecisionSignalSetV1 Python dataclass.

#### Scenario: JSON Schema parity test
- **WHEN** valid DecisionSignalSetV1 dict is validated against JSON Schema
- **THEN** validation passes
- **WHEN** invalid dict is validated against JSON Schema
- **THEN** validation fails with same error as from_dict
