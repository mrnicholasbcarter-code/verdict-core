## ADDED Requirements

### Requirement: Credit provider capacity classification

Routes served by credit providers (Pollinations, `pol`, `g4f-pollinations`, or configured via `VERDICT_CREDIT_PROVIDERS`) without explicit free evidence SHALL NOT be assumed free. When pricing is unknown and no connection free signal or all-zero pricing is present (a `:free` suffix is not evidence), the capacity class SHALL be `CapacityClass.UNKNOWN` (`credit_provider_unknown_pricing`).

#### Scenario: Credit provider route with unknown pricing
- **WHEN** a route belongs to a credit provider, has no pricing, no `:free` suffix and no free connection signal
- **THEN** its capacity class is `UNKNOWN` and evidence is `credit_provider_unknown_pricing`

#### Scenario: Credit provider route with free suffix
- **WHEN** a credit provider route carries a `:free` community suffix and has no pricing and no free connection signal
- **THEN** its capacity class is `UNKNOWN` and evidence is `free_suffix_unverified`

#### Scenario: Credit provider route with positive pricing
- **WHEN** a credit provider route has positive pricing
- **THEN** its capacity class is `METERED` and evidence is `apikey_positive_pricing`

### Requirement: Configurable credit providers via environment variable

The set of credit providers SHALL be configurable via the `VERDICT_CREDIT_PROVIDERS` environment variable (comma-separated, case-insensitive, replacing the default set; an empty string means no credit providers).

#### Scenario: Overriding credit providers
- **WHEN** `VERDICT_CREDIT_PROVIDERS` is set to `custom-credit,other-credit`
- **THEN** only `custom-credit` and `other-credit` are treated as credit providers
