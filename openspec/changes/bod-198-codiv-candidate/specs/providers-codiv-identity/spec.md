# Spec Delta

## Purpose

Provides exact provider-prefix-based identification for Codiv routes through OmniRoute,
preventing cross-provider confusion when the same model name appears under multiple providers.

## ADDED Requirements

### Requirement: Codiv routes SHALL be identified by exact provider prefix

Codiv route identification SHALL use exact provider prefix matching (default `"codiv"`)
rather than model name matching. A route is Codiv if and only if its provider field
matches the configured Codiv provider prefix.

Same model name under a different provider (e.g., `nvidia/diffusiongemma-26b-a4b-it`)
SHALL NOT be treated as Codiv.

#### Scenario: Exact prefix match identifies Codiv route
- **WHEN** inventory contains route with provider="codiv" and model="diffusiongemma-26b-a4b-it"
- **THEN** route is identified as Codiv

#### Scenario: Different provider with same model name is not Codiv
- **WHEN** inventory contains route with provider="nvidia" and model="diffusiongemma-26b-a4b-it"
- **THEN** route is NOT identified as Codiv

#### Scenario: Configurable provider prefix
- **WHEN** Codiv provider prefix is configured to non-default value
- **THEN** only routes with that exact prefix are identified as Codiv

### Requirement: Codiv candidates SHALL pass through standard eligibility gates

Codiv routes SHALL pass through the same eligibility gates as all other routes:
DISCOVERED → ENTITLED → HEALTHY → AVAILABLE → TASK_ELIGIBLE.

No bypass, no special priority for "free" routes.

#### Scenario: Codiv route with missing credentials is ineligible
- **WHEN** Codiv route present in inventory but CODIV_API_KEY missing
- **THEN** route is ineligible with reason "missing credentials"

#### Scenario: Protected work does not force Codiv selection
- **WHEN** protected workload with Codiv route + one paid healthy route
- **THEN** protected work is not forced onto Codiv without positive availability evidence
