# execution-path-aware-clock

## Purpose
Execution-path route requests are assessed with a timezone-aware clock. A request without an injected time uses the current UTC time. A server error is never the result of a missing clock.

## ADDED Requirements

### Requirement: Execution-path admission uses a timezone-aware clock
When `IntelligenceService._prepare_execution_path_request` evaluates execution-path candidates, all time-dependent admission functions SHALL receive a timezone-aware `datetime` (UTC). If `IntelligenceService.admit_now` is `None`, the current UTC time SHALL be used. No code path SHALL pass `None` or a naive `datetime` to `health_cache._aware`, `selector._assess_all`, `gate_capability`, `confirm_live_candidates`, `apply_best_of_admitted`, `lookup_omniroute_id`, or any function that validates timezone awareness.

#### Scenario: Live /v1/route request with execution_path_request and no injected time
- **WHEN** a POST /v1/route request carries an `execution_path_request` with `now: null` (or omitted)
- **THEN** the request is assessed using the current UTC time
- **THEN** the response status is 200 (accepted) or 4xx (rejected with a named reason), never 500
- **TEST** `tests/test_execution_path.py::test_execution_path_request_uses_aware_utc_clock_when_admit_now_is_none`

#### Scenario: Test-injected time is preserved and used
- **WHEN** a test constructs `IntelligenceService` with an explicit `admit_now` (timezone-aware)
- **THEN** that exact time is used for all admission decisions in the execution path
- **TEST** `tests/test_intelligence_service.py::test_prepare_execution_path_request_uses_injected_admit_now`

### Requirement: Single resolved clock per request
`IntelligenceService._prepare_execution_path_request` SHALL resolve `now = self.admit_now or datetime.now(timezone.utc)` once at entry and reuse that resolved value for every subsequent call in the execution-path preparation flow. No call site SHALL independently fall back to `datetime.now(timezone.utc)`.

#### Scenario: Multiple admission gates in one request see the same instant
- **WHEN** `_prepare_execution_path_request` calls `gate_capability`, `build_selector`, `selector._assess_all`, `confirm_live_candidates`, and `apply_best_of_admitted`
- **THEN** all calls receive the same `now` value
- **TEST** `tests/test_intelligence_service.py::test_prepare_execution_path_request_single_clock_per_request`

## MODIFIED Requirements

### Requirement: Execution-path optimizer uses timezone-aware time
**From:** `verdict/execution_path.py` `optimize_execution_path` uses `request.now or datetime.now(timezone.utc)` at line 507.
**To:** No change required — `optimize_execution_path` already uses a timezone-aware fallback. This requirement documents the existing correct behavior as a cross-reference.

#### Scenario: ExecutionPathRequest with now=None
- **WHEN** `optimize_execution_path` receives a request with `now=None`
- **THEN** it uses `datetime.now(timezone.utc)` (UTC, timezone-aware)
- **TEST** Covered by existing `tests/test_execution_path.py` tests