# Spec Delta

## Purpose
Route admission proofs on the execution path to the request clock and reject offset-less tzinfo values for admit_now.

## ADDED Requirements
### Requirement: Admission proofs use the request clock
`IntelligenceService` SHALL pass `IntelligenceService._now` as `now` to `verdict.relay.admission_proof` on the execution-path route.

#### Scenario: Injected admit_now reaches admission_proof
- **WHEN** the service has an injected aware `admit_now` and an execution-path request has a dispatch gateway origin
- **THEN** every `admission_proof` call receives exactly that instant
- **TEST** `tests/test_route_clock_followups.py::test_admission_proof_uses_injected_admit_now`

### Requirement: An offset-less tzinfo is refused
Setting `IntelligenceService.admit_now` to a `datetime` whose `utcoffset()` is `None` SHALL raise `ValueError`.

#### Scenario: tzinfo without an offset
- **WHEN** `service.admit_now` is assigned a `datetime` whose tzinfo returns `None` from `utcoffset()`
- **THEN** `ValueError` is raised and the previous value is kept
- **TEST** `tests/test_route_clock_followups.py::test_admit_now_with_offsetless_tzinfo_is_rejected`
