# Proposal: Execution-path route requests use a timezone-aware clock

## Why

`POST /v1/route` with an `execution_path_request` returned HTTP 500 with `HealthCacheError('now must be a timezone-aware datetime')`. The cause was `IntelligenceService.admit_now` being `None` (the default) and passed directly to admission functions that require a timezone-aware `datetime` via `health_cache._aware`. Every other call site in `intelligence.py` fell back to `datetime.now(timezone.utc)` but the execution-path preparation path did not.

## What Changes

- `IntelligenceService` gains a `_now` property that returns `self.admit_now` when set, otherwise `datetime.now(timezone.utc)` (UTC, timezone-aware).
- All nine call sites in `_prepare_execution_path_request` and the legacy admit path now use `self._now` instead of `self.admit_now`.
- The single resolved clock per request ensures all admission gates see the same instant.
- An explicit `admit_now` injected by tests is preserved and used.
- The execution-path optimizer (`optimize_execution_path`) already uses a timezone-aware fallback and needs no change.

## Capabilities

### New Capabilities

- `execution-path-aware-clock`: Execution-path route requests are assessed with a timezone-aware clock; a request without an injected time uses the current UTC time; a server error is never the result of a missing clock.

### Modified Capabilities

## Impact

- `verdict/intelligence.py`: `_now` property and nine call-site updates.
- Unit tests in `tests/test_intelligence_service.py` verify the fix.
- API test in `tests/test_serve_path_cutover.py` verifies `POST /v1/route` with `execution_path_request` returns non-500.
- No API, gate, dependency, or install change.

## Evidence boundary

KNOWN: a live `/v1/route` request with `execution_path_request` and no injected `now` raised `HealthCacheError` and returned 500 on installed main. KNOWN: all other admission call sites in `intelligence.py` already fell back to `datetime.now(timezone.utc)`. INFERRED: resolving one aware clock per request and reusing it at every call site eliminates the 500 for the execution-path preparation path. NOT AVAILABLE: installed PyPI proof of the full MVP; local test output is not that proof.

## Acceptance criteria

- `POST /v1/route` with an `execution_path_request` and no injected time returns 200 (accepted) or 4xx (rejected with a named reason), never 500.
- A test-injected `admit_now` is preserved and used for all admission decisions in the execution path.
- All admission gates in one request see the same instant (single resolved clock).
- Both validators exit 0: `scripts/check_openspec_tasks.py --base origin/main` and `validate_openspec_change.py`.