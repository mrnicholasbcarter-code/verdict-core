# Spec Delta: Gateway auth confirmation and provider pool isolation

## ADDED Requirements

### Requirement: The census SHALL confirm a gateway auth outage before stopping
On the first route auth failure in a cycle, the census SHALL check the gateway
key once with `/v1/models`, using the same key, no redirects and one bounded,
counted request. A gateway 401 or 403 SHALL stop the cycle without writing the
buffered route failures. Without a gateway check, it SHALL require the existing
failure thresholds and a majority of the distinct credential pools planned in
the cycle. An unknown network result SHALL NOT prove an auth outage.

#### Scenario: Dead provider keys, healthy gateway key
- **WHEN** routes from three provider pools fail auth and `/v1/models` accepts the gateway key
- **THEN** the census continues with the other pools and records the failures per route

#### Scenario: Gateway rejects the key
- **WHEN** `/v1/models` returns 401 or 403
- **THEN** the cycle stops and no buffered route failure is written to the cache

#### Scenario: Budget runs out before confirmation
- **WHEN** the probe budget is spent before the gateway check
- **THEN** the route cache is unchanged and the route stays pending

### Requirement: Pool cooldowns SHALL derive from health-cache negatives
Pool cooldowns SHALL be derived from active health-cache negatives with the
existing pool identity and cooldown categories, without another cooldown store.
Ordering MAY prefer pools with recent success or qualified sessions; ordering
evidence SHALL NOT grant admission, capacity or route health.

#### Scenario: A pool with an active negative
- **WHEN** the health cache holds an active auth negative for a pool
- **THEN** the census skips that pool's routes until the negative expires
