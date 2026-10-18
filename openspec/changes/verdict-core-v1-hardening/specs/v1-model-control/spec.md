# Spec Delta

## Purpose

Defines the foundation behavior required for Verdict V1 across canonical credential and gateway binding and scheduled metadata maintenance with source provenance.

## ADDED Requirements

### Requirement: Canonical credential and gateway binding

The system SHALL resolve the gateway URL and credentials using explicit precedence: configured URL overrides environment URL, which overrides the safe default `http://127.0.0.1:20128/v1`. Canonical stored credentials SHALL bind both discovery and transport callers identically. Credential resolution, validation, and transport errors SHALL NOT leak secret keys or sensitive tokens into logs or error messages.

#### Scenario: Configured URL overrides default
- **WHEN** an explicit gateway URL is provided in configuration or options
- **THEN** the system SHALL bind discovery and transport to that explicit URL instead of the default `http://127.0.0.1:20128/v1`

#### Scenario: Secret-free error reporting
- **WHEN** credential resolution, authentication, or transport fails
- **THEN** the error output and logs SHALL report the failure without exposing raw secret keys or tokens

#### Scenario: Identical caller binding
- **WHEN** canonical credentials are resolved for a gateway session
- **THEN** both discovery and transport callers SHALL receive the identical credential values and gateway base URL

### Requirement: Metadata maintenance has one visible scheduled provenance

The system SHALL provide an optional six-hour metadata refresh timer with a five-minute boot delay, installed only upon explicit setup opt-in. Setup dry-run and declined options SHALL NOT perform persistent writes or system modifications. Scheduled executions SHALL acquire an exclusive lock to prevent concurrency. Refresh results SHALL record source provenance including identity, fetch timestamp, method, and scale. When an individual source fails, the system SHALL retain valid last-good facts within their original deadlines and SHALL NOT fabricate missing facts.

#### Scenario: Partial scheduled refresh
- **WHEN** a scheduled refresh encounters a partial or malformed source result
- **THEN** valid fresh facts and valid last-good facts SHALL remain distinguishable by source provenance and age, and missing facts SHALL NOT be fabricated

#### Scenario: Metadata maintenance was not selected
- **WHEN** the optional metadata timer is absent because the operator has not opted in
- **THEN** doctor SHALL report an explicit opt-in notice without treating the absent feature as healthy or failing unrelated checks

#### Scenario: Timer triggered without a completed refresh
- **WHEN** an installed metadata timer has triggered but has no valid service-completion evidence
- **THEN** doctor SHALL report an incomplete refresh even if the service execution state reports active

#### Scenario: Setup dry-run performs no mutations
- **WHEN** setup is executed in dry-run mode
- **THEN** the system SHALL preview metadata timer configuration without modifying disk files or systemd state

### Requirement: Foundation acceptance distinguishes implementation verification from live proof

The system SHALL distinguish pre-merge code implementation and automated test verification from live host acceptance. Marking pre-merge implementation tasks complete SHALL NOT claim full story DoD, Done status, or change archive, which remain contingent on live operator gateway verification from merged main.

#### Scenario: Pre-merge test verification completed
- **WHEN** all automated tests for foundation stories S01 and S08 pass in the branch environment
- **THEN** the pre-merge tasks SHALL record verified code state while the stories and OpenSpec change remain open pending live merged-main proof
