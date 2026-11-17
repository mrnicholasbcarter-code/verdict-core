# Tasks

## 1. Local implementation

- [x] 1.1 Add `_now` property to `IntelligenceService` returning timezone-aware datetime.
- [x] 1.2 Update nine call sites in `verdict/intelligence.py` to use `self._now` instead of `self.admit_now`.
- [x] 1.3 Add unit test `test_prepare_execution_path_request_uses_aware_utc_clock_when_admit_now_is_none` in `tests/test_intelligence_service.py` to verify the fix.
- [x] 1.4 Add unit test `test_prepare_execution_path_request_uses_injected_admit_now` in `tests/test_intelligence_service.py` to verify test-injected time is used.
- [x] 1.5 Add unit test `test_prepare_execution_path_request_single_clock_per_request` in `tests/test_intelligence_service.py` to verify single clock per request.
- [x] 1.6 Add API test `test_api_route_execution_path_request_returns_non_500` in `tests/test_serve_path_cutover.py` to verify the endpoint returns non-500.
- [x] 1.7 Run all affected tests, ruff, mypy, doc-links, reachability, and vgate to verify no regressions.

## 2. Post-merge verification

Post-merge test: operator installs from merged main, restarts verdict-serve, then POSTs an execution-path request to 127.0.0.1:8000/v1/route and records a non-500 response in the evidence file.

- [ ] (post-merge) Install from merged main, restart nothing by hand (operator restarts verdict-serve), then POST the same execution-path request to 127.0.0.1:8000/v1/route and record a non-500 response.