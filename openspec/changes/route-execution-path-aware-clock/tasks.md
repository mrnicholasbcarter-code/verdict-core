# Tasks

## 1. Local implementation

- [x] 1.1 Reproduce: the `verdict-serve` journal shows `POST /v1/route` 500 with `HealthCacheError: now must be a timezone-aware datetime` from `selector._assess_all(orch_reqs, self.admit_now)`; the new tests fail on origin/main's `verdict/intelligence.py`.
- [x] 1.2 Add `_now` and the request-local `_request_clock` to `IntelligenceService`; bind it in `route()` and `_prepare_execution_path_request()`.
- [x] 1.3 Use `_now` at every route-path time check, including `_route_capacity`, `_apply_candidate_pool`, the legacy admit cooling check and the dispatch cooling check.
- [x] 1.4 Make `admit_now` a validated property: a naive `datetime` raises `ValueError`.
- [x] 1.5 Replace the mocked tests with tests that drive the real service and the real FastAPI app down the serve branch (`tests/helpers/live_execution_path.py`).
- [x] 1.6 Run the focused tests, the full suite, ruff, mypy --strict, doc links, reachability, `check_openspec_tasks.py` and strict OpenSpec validation.

## 2. Post-merge verification

- [ ] (post-merge) Install from merged main, restart nothing by hand (operator restarts verdict-serve), then POST the same execution-path request to 127.0.0.1:8000/v1/route and record a non-500 response.
