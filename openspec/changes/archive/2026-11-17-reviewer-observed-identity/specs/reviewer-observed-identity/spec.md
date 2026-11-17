## Purpose

Require a usable OCR-reported reviewer identity before a review can pass or an
independence claim can be verified. The reported model is not a verified
gateway-served identity.

## ADDED Requirements

### Requirement: Positive coverage without a usable reported model is not a pass
When an OCR payload has positive coverage and `manifest.execution.model` is missing,
blank, not a string, or an opaque alias, interpretation SHALL return `ERROR` with a
detail that starts `identity_unobserved`. It SHALL NOT stringify a mapping, list,
number, or boolean into an identity. The error SHALL be reviewer-scoped and SHALL
reselect another reviewer inside the existing attempt budget. Coverage vetoes SHALL NOT
reselect.

#### Scenario: No execution model
- **WHEN** a coverage-positive payload has no `manifest.execution.model`
- **THEN** the review result is `ERROR` and the detail names `identity_unobserved`

#### Scenario: Unusable reported model
- **WHEN** the reported model is blank, a non-string, or an opaque alias
- **THEN** the review result is `ERROR` and the value is not used as an identity

#### Scenario: Reselect
- **WHEN** the first reviewer reports no usable model and a later reviewer reports a matching model
- **THEN** review returns `PASS` on the later reviewer and records the first attempt as route-scoped

### Requirement: Independence uses the observed reviewer identity
The independence claim SHALL return `NOT_OBSERVED` when the review event's
`observed_model` is missing, blank, not a string, or opaque, including streams that
have no such key. It SHALL include a usable observed identity with the selected route
and review-attempt routes. Shared identity SHALL be `CONTRADICTED`. Shared family or
aliased pool SHALL be `NOT_OBSERVED`. A genuinely independent observed reviewer SHALL
remain `VERIFIED`. Derivation SHALL NOT rewrite stored receipt bytes.

#### Scenario: Missing observed model
- **WHEN** a passing review event has no usable `observed_model`
- **THEN** the independence claim is `NOT_OBSERVED`

#### Scenario: Observed identity matches a worker
- **WHEN** the observed reviewer identity equals a worker executed identity and the selected reviewer route does not
- **THEN** the independence claim is `CONTRADICTED`

#### Scenario: Shared family or pool
- **WHEN** the observed reviewer shares a family or an aliased pool with a worker
- **THEN** the independence claim is `NOT_OBSERVED`
