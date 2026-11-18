# Proposal: Execution-path route requests use a timezone-aware clock

## Why

`POST /v1/route` with an `execution_path_request` returned HTTP 500 on the installed service. The `verdict-serve` journal shows the traceback: `IntelligenceService._prepare_execution_path_request` called `selector._assess_all(orch_reqs, self.admit_now)`; `admit_now` is `None` unless a test injects it; the eligibility ladder passed it to `HealthCache.lookup`, which raised `HealthCacheError: now must be a timezone-aware datetime`. Other sites on the same route path (`_route_capacity`, `_apply_candidate_pool`, the legacy free-tier admit path and the dispatch cooling check) each read `datetime.now(timezone.utc)` for themselves, so one request could see more than one instant.

## What Changes

- `IntelligenceService._now` is the only clock on the route path. It returns the injected `admit_now`, else the instant bound for the current request, else the current UTC time.
- `route()` and `_prepare_execution_path_request()` bind one wall-clock instant per call (a `ContextVar`, so concurrent requests on the shared service stay apart). Every admission and health-cache time check in that call reads it through `_now`.
- The four sites that read their own clock (`_route_capacity`, `_apply_candidate_pool`, the legacy admit cooling check, the dispatch cooling check) now use `_now`.
- `admit_now` is validated: a naive `datetime` raises `ValueError` at construction or assignment. It is never silently treated as UTC.

## Capabilities

### New Capabilities

- `execution-path-aware-clock`: Execution-path route requests are assessed with one timezone-aware instant per request. A request without an injected time uses the current UTC time. A naive injected clock is refused when it is set, not inside a request.

### Modified Capabilities

## Impact

- `verdict/intelligence.py`: `_now`, `_request_clock`, the `admit_now` property, and the route-path call sites.
- Tests drive the real `IntelligenceService` and the real FastAPI app down the serve branch, faking only the gateway reads (`tests/helpers/live_execution_path.py`).
- No API, gate, dependency, or install change.

## Evidence boundary

KNOWN: the `verdict-serve` journal on this host records three `POST /v1/route` 500s with the `HealthCacheError` traceback above. KNOWN: the new tests fail on origin/main's `verdict/intelligence.py` and pass on this branch. NOT KNOWN until after merge and install: the live service answering the same request without a 500; that is the post-merge task.

## Acceptance criteria

- `POST /v1/route` with an `execution_path_request` and no injected time returns a decision, never 500.
- A test-injected aware `admit_now` is the instant every health-cache check sees.
- Every health-cache time check in one request sees one instant.
- A naive `admit_now` raises `ValueError` when it is set.
