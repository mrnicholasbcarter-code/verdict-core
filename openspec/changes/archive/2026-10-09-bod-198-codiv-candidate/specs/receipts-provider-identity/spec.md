# Spec Delta

## Purpose

Tracks intended vs executed provider/model identity in execution receipts, enabling
detection of fallback scenarios and provider identity verification.

## ADDED Requirements

### Requirement: Receipt SHALL record intended and executed provider/model identity

Execution receipts SHALL include fields for:
- `intended_provider`: Provider selected by routing logic
- `intended_model`: Model selected by routing logic
- `executed_provider`: Provider that actually handled the request
- `executed_model`: Model that actually handled the request

These fields enable verification that the intended route was executed and detection
of fallback scenarios.

#### Scenario: Receipt captures intended provider/model
- **WHEN** routing selects provider="codiv", model="diffusiongemma-26b-a4b-it"
- **THEN** receipt contains `intended_provider="codiv"` and `intended_model="diffusiongemma-26b-a4b-it"`

#### Scenario: Receipt captures executed provider/model
- **WHEN** request is handled by provider="codiv", model="diffusiongemma-26b-a4b-it"
- **THEN** receipt contains `executed_provider="codiv"` and `executed_model="diffusiongemma-26b-a4b-it"`

### Requirement: Receipt SHALL flag provider mismatch when fallback occurs

When the executed provider/model differs from the intended provider/model (fallback
scenario), the receipt SHALL set `provider_mismatch=True`.

#### Scenario: No fallback, no mismatch flag
- **WHEN** intended and executed provider/model match
- **THEN** receipt has `provider_mismatch=False` or field is absent

#### Scenario: Fallback triggers mismatch flag
- **WHEN** intended_provider="codiv" but executed_provider="openai" (fallback occurred)
- **THEN** receipt has `provider_mismatch=True`

### Requirement: Receipt fields SHALL be backward compatible

Adding intended/executed provider/model fields SHALL NOT break existing receipt
parsing or validation. Fields SHALL be optional with safe defaults.

#### Scenario: Legacy receipts without provider identity fields remain valid
- **WHEN** validating a receipt created before this change
- **THEN** validation passes (fields are optional)
