# Spec Delta

## Purpose

Provides OpenJev/Codiv `/v1/systemone` adapter as optional DecisionSignalProvider. Maps HTTP/runtime failures using existing normalize_failure. Missing credentials → UNKNOWN (never blocks). Injectable transport for hermetic tests.

## ADDED Requirements

### Requirement: OpenJev SHALL POST to /v1/systemone endpoint

OpenJevSystemOneProvider SHALL POST to `{base_url}/v1/systemone` (NOT `/v1/chat/completions`).

#### Scenario: Endpoint verification
- **WHEN** OpenJevSystemOneProvider.signals() called with injectable transport
- **THEN** transport receives POST to path ending in "/v1/systemone"

### Requirement: OpenJev SHALL map HTTP failures with normalize_failure

OpenJevSystemOneProvider SHALL reuse existing normalize_failure for:
- HTTP 429 quota exhausted (code "insufficient_quota" | "quota_exceeded" | "quota_exhausted") → QUOTA
- HTTP 429 rate-limit → RATE_LIMIT + parse_retry_after cooldown
- HTTP 529 → OVERLOADED
- Timeout → TIMEOUT
- Network error → TRANSPORT
- Malformed JSON → INVALID_REQUEST
- Schema violation → INVALID_REQUEST

#### Scenario: 429 quota exhausted
- **WHEN** provider returns 429 with quota-exhausted code
- **THEN** DecisionSignalSetV1 has failure_class=QUOTA, signals=None

#### Scenario: 429 rate-limit with Retry-After
- **WHEN** provider returns 429 with Retry-After: 120
- **THEN** DecisionSignalSetV1 has failure_class=RATE_LIMIT, cooldown_seconds set

#### Scenario: 529 overload
- **WHEN** provider returns 529
- **THEN** DecisionSignalSetV1 has failure_class=OVERLOADED, signals=None

#### Scenario: Timeout
- **WHEN** transport raises timeout exception
- **THEN** DecisionSignalSetV1 has failure_class=TIMEOUT, signals=None

#### Scenario: Malformed JSON
- **WHEN** provider returns malformed JSON
- **THEN** DecisionSignalSetV1 has failure_class=INVALID_REQUEST, signals=None

#### Scenario: Schema violation
- **WHEN** provider returns JSON missing required field
- **THEN** DecisionSignalSetV1 has failure_class=INVALID_REQUEST, signals=None

### Requirement: Missing credentials SHALL return UNKNOWN

When OPENJEV_API_KEY or OPENJEV_BASE_URL missing, OpenJevSystemOneProvider SHALL return DecisionSignalSetV1 with failure_class=UNKNOWN, signals=None. SHALL NOT raise exception.

#### Scenario: Missing API key
- **WHEN** OPENJEV_API_KEY unset
- **AND** OpenJevSystemOneProvider.signals() called
- **THEN** DecisionSignalSetV1 has failure_class=UNKNOWN, signals=None, no exception

### Requirement: Tests SHALL use injectable transport

All OpenJevSystemOneProvider tests SHALL use injectable transport callable. SHALL NOT make network calls.

#### Scenario: Hermetic test with confident response
- **WHEN** test provides mock transport returning confident response
- **THEN** test validates DecisionSignalSetV1 without network

## MODIFIED Requirements

<!-- No existing requirements modified -->
