# Spec Delta: Certification rehearsal FREE-only agentic preflight

## ADDED Requirements

### Requirement: Rehearsals SHALL qualify FREE routes with a bounded agentic preflight before orchestration
Before any rehearsal orchestration, the certification rehearsal SHALL run one
consented agentic screen, `verdict identify --screen --allow-live-probe
--capacity free`, with explicit route-per-provider and route caps and a
wall-time bound, in the same environment and `VERDICT_HOME` as the
orchestration. The screen SHALL write genuine probe results to the health
cache the eligibility ladder reads. The rehearsal SHALL NOT copy, import or
synthesize agentic proof, and SHALL NOT treat the connections snapshot as
proof.

#### Scenario: Preflight passes
- **WHEN** the preflight screen exits 0 and reports `routes_checked` and `passed` both above zero
- **THEN** the clean and chaos orchestrations run, each with `--capacity free`

#### Scenario: Preflight is FREE-only and bounded
- **WHEN** the rehearsal builds the preflight command
- **THEN** the command has `--capacity free`, `--allow-live-probe`, a positive `--max-per-provider` and a positive `--max-routes`, and no subscription, metered or unknown capacity

### Requirement: Rehearsals SHALL fail closed when the preflight qualifies no FREE route
When the preflight screen exits non-zero, prints invalid JSON, or reports zero
`routes_checked` or zero `passed`, the rehearsal SHALL stop with a named
`RehearsalConfigurationError` before any orchestration starts. The error
message SHALL NOT contain credentials or the gateway URL.

#### Scenario: Zero passing routes
- **WHEN** the preflight reports `routes_checked` 0 and `passed` 0
- **THEN** the rehearsal raises `RehearsalConfigurationError` naming "no qualified FREE routes" and no orchestrate command runs
