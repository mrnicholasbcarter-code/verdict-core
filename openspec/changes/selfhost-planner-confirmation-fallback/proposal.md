# Bounded planner confirmation fallback

## Why
Cold FREE catalog aliases can spend every exact-confirmation slot before fresh
prepaid alternatives are reached. Planner failover then blocks after one route.

## What changes
- Confirm fresh healthy routes before cold routes within each capacity class.
- Reserve the last existing confirmation slot for a fresh SUBSCRIPTION fallback
  when earlier candidates would exhaust the budget. Confirmed FREE still wins.
- Keep admission, capability floors, cooldown scopes and probe limits unchanged.
- Record the reserved-slot selection reason in planner events.
- Normalize only non-empty acceptance strings to singleton lists.
- Retain bounded, redacted invalid planner outputs in the run directory.

## Proof
Offline focused regressions use fake probes and temporary state. No provider
calls, full suite, live proof or host state changes are part of this change.
