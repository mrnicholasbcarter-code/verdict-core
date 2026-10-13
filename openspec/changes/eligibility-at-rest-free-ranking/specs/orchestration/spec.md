# Orchestration eligibility

## ADDED Requirements

### Requirement: Proven FREE routes stay eligible at rest
The ladder SHALL list a FREE route as eligible when it has an agentic proof no
older than 24 hours, the gateway's synced live catalog serves it, and no pool,
provider, route or rate-limit cooldown is active. Its reason SHALL be
`liveness_at_select` until a liveness check younger than 15 minutes exists.
Listing SHALL NOT probe.

#### Scenario: Proven route without recent liveness
- **WHEN** a FREE route passed the agentic probe 2 hours ago and the synced catalog serves it
- **THEN** `verdict eligibility --json` shows it at TASK_ELIGIBLE with reason `liveness_at_select`
- **AND** no probe is sent

#### Scenario: Catalog ghost
- **WHEN** the same route is not in the synced catalog
- **THEN** it is not eligible until a select-time check passes

### Requirement: The selected route is rank 0
Selection SHALL confirm candidates in rank order with a just-in-time liveness
check and SHALL select the first that passes. Routes that fail the check SHALL
leave the ranking. After three failed checks in one selection, the remaining
routes of that provider SHALL be tried after every other provider's routes.

#### Scenario: Fresh low-ranked route of the top provider
- **WHEN** the rank-0 route has no recent liveness and a rank-2 route of the same provider is fresh
- **THEN** the rank-0 route is checked first and, when live, selected with rank 0

### Requirement: Account-level probe failures cool the pool
The prober SHALL record a failure scope on every failure. An authentication,
permission, payment or rate-limit failure whose provider message names the
account, key limit, API key, credits or billing SHALL write a `pool:<id>`
cooldown, and the prober SHALL send no further request to that pool in the
cycle. A failure that names the model, follows an answer in the same probe, or
occurs while another route of the pool succeeded within 15 minutes SHALL stay
on the route.

#### Scenario: OpenRouter key total limit
- **WHEN** one route returns 403 "Key limit exceeded (total limit)"
- **THEN** `pool:openrouter` is cooled and no other openrouter route is probed in that cycle

#### Scenario: Harness-only model
- **WHEN** one `:free` route returns 403 "... is only available on agentic harnesses"
- **THEN** only that route is negative and the other routes of its pool are still probed

### Requirement: Free-first ranking with failing routes last
For implementation work, the ladder SHALL rank routes with failing session
evidence (at least 3 outcomes and a score under 0.5) last, then by capacity
class with FREE first, then by session score. For frontier_worthy work it SHALL
rank proven routes, then routes without evidence, then failing routes, before
capacity class. Inside FREE capacity, the gateway's measured success rate
(requests >= 5, coarse bucket), capability metadata, context window and probe
latency SHALL follow. Quota SHALL NOT rank FREE routes.

#### Scenario: Unknown FREE route and a subscription route with one pass
- **WHEN** a FREE route has no session outcomes and a subscription route has 1 of 1 passed
- **THEN** the FREE route ranks first for implementation work

#### Scenario: Failing FREE route
- **WHEN** a FREE route has 0 of 3 passed and a subscription route has 3 of 3
- **THEN** the subscription route ranks first

#### Scenario: Frontier work
- **WHEN** the same two routes rank for planning and the subscription route is proven
- **THEN** the subscription route ranks first

#### Scenario: Large coding model and small vision model
- **WHEN** both FREE routes passed the agentic probe and have no session outcomes
- **THEN** the large coding model ranks above the small vision model

### Requirement: A gateway overload is not route evidence
A 503 or 429 whose body is the gateway's own overload message (resource or heap
pressure, chat admission busy) SHALL NOT write route health, a route or pool
cooldown, or a failure count. The prober SHALL end its cycle after 5
consecutive sheds. A select-time check that the gateway sheds SHALL move to the
next candidate. A provider-originated 5xx SHALL still count against the route.

#### Scenario: Probes under gateway load
- **WHEN** every probe returns 503 "resource pressure"
- **THEN** the cache is unchanged and the cycle stops with `gateway_overload` after 5 probes

#### Scenario: Provider 503
- **WHEN** a probe returns 503 with the provider's own error
- **THEN** the route is recorded as `upstream`

### Requirement: Availability failures do not revoke capability
A probe failure SHALL revoke a route's agentic proof only when the model
answered and failed the task (wrong or missing tool use, model mismatch). A
timeout, 5xx, 429 or account error SHALL keep the proof; the route's own
negative blocks it at HEALTHY until it expires. The prober SHALL retry the
agentic probe of a route whose last failure was a timeout or 5xx once that
negative expires.

#### Scenario: 503 on a proven route
- **WHEN** a route with an agentic proof from 2 hours ago gets a provider 503
- **THEN** the route is negative for the backoff window and keeps its proof
- **AND** it is eligible again once the negative expires
