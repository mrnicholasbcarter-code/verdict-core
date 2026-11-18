## ADDED Requirements

### Requirement: Ladder health probe rejects an in-band credit refusal

The ladder's one-token health probe SHALL report an HTTP 200 response whose body is an in-band
credit refusal as unhealthy with category `payment_required`. The ladder SHALL scope that result
to the probed route: it SHALL NOT write a pool or provider cooldown or a pool not-free override.

#### Scenario: Captured refusal

- **WHEN** the probe receives HTTP 200 with zero completion tokens, no tool call and the text "doesn't have enough credits"
- **THEN** the result is unhealthy with category `payment_required`

#### Scenario: Sibling route stays selectable

- **WHEN** one route of a provider returns that refusal and a sibling route of the same provider answers
- **THEN** the ladder selects the sibling, and only the refusing route has a cooldown

### Requirement: Forced agentic reprobe

`verdict prove-at-rest once --reprobe ROUTE` SHALL be repeatable and SHALL put each named route
into the agentic phase even when its agentic proof is fresh or it has an active negative. A
forced route SHALL be an admitted FREE chat route and SHALL count against `--agentic-max-routes`
and `--max-requests`. The output SHALL list the forced routes that were queued and, for each
route that was not, the reason.

#### Scenario: Fresh proof is probed again

- **WHEN** a route has agentic proof five minutes old and is named with `--reprobe`
- **THEN** the cycle runs the agentic probe for it

#### Scenario: Bounds hold

- **WHEN** four routes are named, `--agentic-max-routes` is 1, one route is metered and one is not admitted
- **THEN** one route is probed and the others are reported as `budget`, `capacity:metered` and `not_admitted`

### Requirement: A billing refusal invalidates agentic proof

A route-scoped billing negative with capacity evidence `billing_402_metered` SHALL clear the
route's agentic proof and its agentic timestamp. Timeout, upstream, rate-limit and
authentication negatives SHALL keep the proof as before.

#### Scenario: Relay refusal

- **WHEN** the relay records a 402 or an in-band refusal for a route with fresh agentic proof
- **THEN** the route's entry has no agentic proof and no agentic timestamp

#### Scenario: Timeout keeps the proof

- **WHEN** a timeout is recorded for a route with fresh agentic proof
- **THEN** the proof and its timestamp are unchanged

### Requirement: Lease-held selection has its own reason

When selection chooses no route and at least one candidate was skipped with reason `lease_held`,
`verdict route` and `verdict choose` SHALL report reason `lease_held` and exit with status 75.
Otherwise a selection with no route SHALL keep reason `no_eligible_route` and exit status 1.

#### Scenario: Lease held

- **WHEN** no route is chosen and a candidate verdict has reason `lease_held`
- **THEN** the reason is `lease_held` and the exit status is 75

#### Scenario: No eligible route

- **WHEN** no route is chosen and no candidate verdict has reason `lease_held`
- **THEN** the reason is `no_eligible_route` and the exit status is 1

### Requirement: Acceptance treats a held lease as transient

When a command check fails with `lease_held`, acceptance SHALL wait for
`verified-refresh.lock` to be free for at most 10 minutes, run the command once more, and record
both attempts. The second result SHALL decide the check. A check that fails with
`no_eligible_route` SHALL NOT be retried for this reason.

#### Scenario: Lease frees

- **WHEN** the first attempt reports `lease_held` and the second selects a route
- **THEN** the check passes and its detail holds two attempts and the wait in seconds

#### Scenario: Lease stays held

- **WHEN** both attempts report `lease_held`
- **THEN** the check fails after exactly two attempts

### Requirement: Identify screen does not start on the hour

`verdict-identify-screen.timer` SHALL use `OnCalendar=*-*-* 00/6:20:00`.

#### Scenario: Rendered timer

- **WHEN** the identify units are rendered
- **THEN** the screen timer contains `OnCalendar=*-*-* 00/6:20:00`

### Requirement: Setup refreshes a stale documentation index

When the docs-refresh setup action is applied and the read-only documentation preflight is not
ready, setup SHALL start `verdict-docs-refresh.service` once without waiting for it. The action
SHALL stay planned while the preflight is not ready. The doctor repair for a documentation
preflight failure and for a failed refresh run SHALL be
`verdict memory docs --fix --repo-root <root>`.

#### Scenario: Stale index

- **WHEN** the action is applied and the preflight is not ready
- **THEN** `systemctl --user start --no-block verdict-docs-refresh.service` is issued once

#### Scenario: Ready index

- **WHEN** the action is applied and the preflight is ready
- **THEN** no start is issued

### Requirement: Relay failover after a refusal needs no idempotency key

After an attempt that ended as an in-band credit refusal, the relay SHALL be allowed to send the
request to the next admitted candidate without a caller idempotency key. For every other failed
attempt the idempotency rule SHALL be unchanged.

#### Scenario: Refusal then answer

- **WHEN** the first of two admitted free candidates refuses in-band, the second answers, and the request has no idempotency key
- **THEN** the response is HTTP 200 from the second candidate

#### Scenario: Server error without a key

- **WHEN** the first candidate answers HTTP 503 and the request has no idempotency key
- **THEN** no second candidate is tried

### Requirement: A free suffix is not free evidence

Capacity classification SHALL NOT return FREE because a route id ends with `:free` or contains
"free". FREE SHALL need evidence: public OpenRouter 0/0 membership (for `openrouter/` routes and
for `:free` routes on prefixes that share the OpenRouter free pool), a connection free signal,
or all-zero inventory pricing. A `:free` route on a credit provider without such evidence and
without positive pricing SHALL be UNKNOWN with evidence `free_suffix_unverified`.

#### Scenario: Credit provider

- **WHEN** a `pol/` route ends with `:free` and has no pricing and no connection free signal
- **THEN** its capacity class is UNKNOWN and its evidence is `free_suffix_unverified`

#### Scenario: Alias of a public free route

- **WHEN** a `kc/` route ends with `:free` and the same model id is in the public OpenRouter 0/0 set
- **THEN** its capacity class is FREE and its evidence is `openrouter_public_zero_alias`

#### Scenario: Suffix on a subscription connection

- **WHEN** a `kc/` route ends with `:free`, is not in the public 0/0 set and its connection is OAuth
- **THEN** its capacity class is SUBSCRIPTION
