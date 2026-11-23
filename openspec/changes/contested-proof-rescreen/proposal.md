# Proposal

## Why

Two bugs let fresh-proof routes escape re-testing even when their real quality
is contested, and prevent a named reprobe from running when the chat phase runs
first:

**Bug 1 — contested fresh proofs never re-tested.**
When the hourly tool check (`verdict acceptance run`) finds that a listed FREE
route fails both check attempts, it records the failure but never asks the
six-hourly screen to re-probe that route. And even if a rescreen hint were
already in the cache, `verdict identify --screen` would still skip the route
with `skipped_fresh` because it has recent agentic proof — the hint is queued
but ignored.

**Bug 2 — reprobe starved by the chat phase.**
`prove-at-rest once --reprobe` builds a full budget for the chat
`RefreshCoordinator` before reserving anything for the named agentic reprobe.
If the coordinator uses all its requests, the named reprobe finds
`remaining_requests = 0` and silently skips every route.

Together these two bugs make it possible for a route to pass the hourly tool
check by luck on attempt 1, receive no rescreen hint, and drift silently into a
degraded state between acceptance runs. The fix closes the loop so that a route
flagged by acceptance is probed at the next scheduled screen, and so that a
named forced reprobe always gets its three turns.

## What Changes

- **`verdict/acceptance_toolcheck.py` — `measure()` return dict**: a new
  `failed_all_listed` key containing the route IDs of listed routes whose final
  outcome after retry was `"fail"`.

- **`verdict/acceptance.py` — `eligibility_check`**: after `measure()`, if
  `failed_all_listed` is non-empty, calls
  `HealthCache.request_rescreen(failed_all_listed, now=...)` inside a
  `try/except`. This is a scheduling hint only — no health, proof or capability
  is written. The result is recorded in the quality dict for the audit log.

- **`verdict/identify.py` — `decide()` inner function**: the `skipped_fresh`
  guard is extended with `route_id not in requested`. A route that has a pending
  rescreen request bypasses the fresh-proof skip and is probed; it still
  respects the negative state, cooldown, caps, pool limit and the lease.

- **`verdict/prove_at_rest.py` — `Prober`**: a new private field
  `_reprobe_reserved` is set in `run_once()` before the chat phase starts, to
  `min(admitted_free_reprobe_count * AGENTIC_TURNS, max_requests)`. The chat
  coordinator receives `max_requests - _reprobe_reserved` as its budget. When
  that budget is zero or less the chat phase returns early without probing,
  leaving the full budget for the named reprobe.

## Capabilities

### Modified Capabilities
- `acceptance-eligibility-check`: `measure()` now returns `failed_all_listed`;
  `eligibility_check` queues a rescreen hint for routes that fail both attempts.
- `identify-screen`: routes with a pending rescreen request bypass the
  `skipped_fresh` guard and are always probed.
- `prove-at-rest-once-reprobe`: the chat phase is budget-capped before the
  named reprobe routes are reserved, so the reprobe always gets its turns.

## Impact

No new package dependency. No new service or timer.

Production files modified:
- `verdict/acceptance_toolcheck.py` — new `failed_all_listed` key in return dict
- `verdict/acceptance.py` — rescreen hint queued after tool check
- `verdict/identify.py` — one-line guard change in `decide()`
- `verdict/prove_at_rest.py` — `_reprobe_reserved` field and budget split

## Evidence boundary

KNOWN: the three code-path bugs are confirmed by reading the sources.

INFERRED: a rescreen hint queued by acceptance will be consumed at the next
six-hourly screen because the same `HealthCache` instance is used for both
the acceptance run and the identify screen job.

NOT AVAILABLE: a live end-to-end trace showing a tool-check failure producing
a rescreen hint that is consumed by the next scheduled screen. That is a
post-merge operator verification.

## Acceptance criteria

1. `measure()` returns a non-empty `failed_all_listed` only for routes whose
   final listed-attempt outcome is `"fail"`.
2. `eligibility_check` calls `request_rescreen` for every route in
   `failed_all_listed` and writes no health or proof entry.
3. A route with a pending rescreen request is probed by `screen_models` even
   when it has fresh agentic proof; an unrequested fresh route is still
   `skipped_fresh`.
4. With a request budget that fits one reprobe plus some chat probes,
   `run_once()` reserves the reprobe budget first; the chat phase receives a
   reduced budget; the named route is probed and the total stays within the cap.
