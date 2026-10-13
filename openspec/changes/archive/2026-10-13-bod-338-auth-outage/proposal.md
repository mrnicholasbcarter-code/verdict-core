# BOD-338: Gateway auth confirmation and provider pool isolation

## Why
Five auth failures from three dead provider credentials can stop a large census
before healthy pools get refreshed. Provider credentials are not the gateway key.

## What Changes
- Confirm the gateway key on the first route auth failure using `/v1/models`.
- Use the same key, no redirects, one bounded and counted request per cycle.
- Stop on a gateway 401/403 without writing the buffered route failures.
- Without a gateway check, require the existing failure thresholds and a majority
  of the distinct canonical credential pools planned in the cycle.
- Derive pool cooldowns from active health-cache negatives using the existing
  pool identity and cooldown categories. Do not add another cooldown store.
- Prefer recent-success or qualified-session pools within existing ordering passes.

## Safety
Unknown gateway network results never prove an auth outage. Budget exhaustion
before confirmation leaves the route cache unchanged and the route pending.
Ordering evidence never grants admission, capacity, or route health.
Tests use injected transports and temporary state, without live provider calls.
