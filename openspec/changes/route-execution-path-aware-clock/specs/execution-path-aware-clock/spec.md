# Spec Delta

## Purpose

Execution-path route requests are assessed with one timezone-aware instant per request. A request without an injected time uses the current UTC time. A server error is never the result of a missing or naive clock.

## ADDED Requirements

### Requirement: Execution-path admission uses a timezone-aware clock
`IntelligenceService` SHALL pass a timezone-aware `datetime` to every admission and health-cache time check on the route path, including `selector._assess_all`, `HealthCache.lookup`, `billing_capacity_override`, `gate_capability`, `gate_admit_prove_confirm`, `apply_best_of_admitted` and `lookup_omniroute_id`. When `admit_now` is `None`, the current UTC time SHALL be used.

#### Scenario: Live /v1/route request with execution_path_request and no injected time
- **WHEN** a POST /v1/route request carries an `execution_path_request` and the service has no injected `admit_now`
- **THEN** the eligibility ladder reads the health cache with an aware UTC instant
- **THEN** the response is a routing decision, never HTTP 500
- **TEST** `tests/test_serve_path_cutover.py::test_api_route_execution_path_request_without_clock_returns_decision`, `tests/test_intelligence_service.py::test_live_execution_path_route_without_admit_now_reaches_a_decision`

#### Scenario: Test-injected time is preserved and used
- **WHEN** the service is constructed with an aware `admit_now`
- **THEN** every health-cache lookup in the execution-path request uses exactly that instant
- **TEST** `tests/test_intelligence_service.py::test_live_execution_path_route_uses_injected_admit_now`

### Requirement: Single resolved clock per request
`IntelligenceService.route` and `IntelligenceService._prepare_execution_path_request` SHALL bind one instant at entry and every admission and health-cache time check in that call SHALL read it through `IntelligenceService._now`. No call site on the route path SHALL read `datetime.now` for itself. The bound instant SHALL be request-local, so concurrent requests on one service do not share it.

#### Scenario: Ladder and candidate-pool checks in one request see the same instant
- **WHEN** one execution-path request runs the ladder's `HealthCache.lookup` and the candidate pool's `billing_capacity_override`
- **THEN** every call receives the same aware instant
- **TEST** `tests/test_intelligence_service.py::test_live_execution_path_route_reads_one_instant_for_every_health_check`

### Requirement: A naive injected clock is refused when it is set
Setting `IntelligenceService.admit_now` to a naive `datetime`, at construction or by assignment, SHALL raise `ValueError`. It SHALL NOT be treated as UTC.

#### Scenario: Naive admit_now
- **WHEN** `IntelligenceService(..., admit_now=datetime(2026, 11, 17, 12, 0))` is constructed, or `service.admit_now` is assigned a naive `datetime`
- **THEN** `ValueError` is raised and the previous value is kept
- **TEST** `tests/test_intelligence_service.py::test_naive_admit_now_is_rejected`
