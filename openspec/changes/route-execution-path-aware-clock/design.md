# Design

## Context

On the serve branch (no fixture `admit_snapshot`, a configured OmniRoute endpoint), `IntelligenceService._prepare_execution_path_request` builds the eligibility ladder and calls `selector._assess_all(orch_reqs, self.admit_now)`. `admit_now` is `None` unless a test injects it. The ladder passes it to `HealthCache.lookup` and `agentic_capability`, which need an aware `datetime`, so the request failed with `HealthCacheError` (or `TypeError` once a route has an agentic entry) and the app answered 500. Other sites on the route path read `datetime.now(timezone.utc)` for themselves: `_route_capacity`, `_apply_candidate_pool`, the legacy admit cooling check and the dispatch cooling check.

## Goals / Non-Goals

**Goals:**
- One aware instant per request, read through one accessor at every admission and health-cache time check on the route path.
- An injected aware `admit_now` stays authoritative.
- A naive injected clock fails when it is set, not inside a request.

**Non-Goals:**
- Change `optimize_execution_path`; it already falls back to an aware UTC time.
- Change any API contract, gate, dependency, or install behaviour.
- Thread the instant through modules outside `verdict/intelligence.py`.

## Decisions

- `_now` returns `admit_now`, else the instant in the module `ContextVar` `_REQUEST_NOW`, else `datetime.now(timezone.utc)`. Reason: every call site reads one accessor, so none can pick its own clock.
- `_request_clock()` binds `_REQUEST_NOW` once per `route()` and per `_prepare_execution_path_request()` call. A nested call reuses the outer instant. Reason: the service is shared across concurrent requests, so the instant must be request-local. A `ContextVar` is per asyncio task and per thread; an instance attribute would leak one request's instant into another.
- `admit_now` is a property whose setter raises `ValueError` for a naive `datetime`. Reason: treating naive as UTC would hide a caller bug, and the failure belongs where the value is set. Existing callers already pass aware values.

## Risks / Trade-offs

- Code outside `route()` or `_prepare_execution_path_request()` that reads `_now` gets a fresh wall-clock time on each read. That matches the old behaviour for those paths.
- The setter rejects a naive value assigned by any caller. A repository search finds only aware values passed or assigned.

## Security / trust implications

A clock bug no longer turns an admission into a server error. Admission outcomes are unchanged: unknown stays unknown, and a stale proof is still stale.

## Routing / context / memory implications

No routing policy, context pipeline, or memory store change.

## Migration / rollback

No data migration. Reverting the change restores the 500 on execution-path requests without an injected time.

## ADR impact

ADR 0001 (control plane invariants) and ADR-035 (authorized selected-route dispatch) remain governing. No ADR file changes.
