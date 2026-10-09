# Spec Delta: Bounded model refresh and consent

## Purpose

Keep needed model health current through bounded prepaid refresh with wait-before-use behavior, shared cancellation, and explicit spend consent, while preserving exact launch admission and snapshot-only autocomplete.

## ADDED Requirements

### Requirement: Named consumers SHALL wait for needed refresh before using statuses

Opening the TUI or CLI verified view, picker preview/apply for involved ids, and selection before dispatch SHALL trigger automatic refresh of needed eligible non-fresh routes. Picker consumers SHALL supply exactly their involved ids; verified views SHALL need only the current filtered page, default 50, not the full inventory. Selection SHALL finish refresh before evaluating/using its result and SHALL retain existing exact-route confirmation before launch.

A consumer SHALL show live progress only while waiting, then reload evidence and render/use final statuses after bounded completion, including partial, cancelled, busy, or error completion. It SHALL NOT render a provisional model list. If all needed entries are fresh, the consumer SHALL return `reused_fresh` immediately with zero model calls, zero wait, and no job, before joining a lock. `VERDICT_AUTO_REFRESH` SHALL default to enabled; `VERDICT_AUTO_REFRESH=0` SHALL prevent new automatic jobs and return the labelled snapshot `auto_refresh_disabled`, not fabricate manual consent.

#### Scenario: Stale prepaid view waits then renders
- **WHEN** a verified-view page needs eligible stale prepaid routes
- **THEN** one bounded refresh runs or is joined, only progress is displayed during the wait, and final rows appear after completion and evidence reload

#### Scenario: Fresh page bypasses an unrelated job
- **WHEN** all needed entries are fresh while another refresh is active
- **THEN** the consumer returns reused_fresh immediately without model calls, waiting, or joining that job

#### Scenario: Auto-refresh switch prevents automatic spend
- **WHEN** VERDICT_AUTO_REFRESH=0 and a consumer needs stale prepaid evidence
- **THEN** it returns last-known snapshot statuses labelled auto_refresh_disabled and starts no automatic job

#### Scenario: Picker and selection need only their exact ids
- **WHEN** picker preview/apply or selection requests a subset of route ids
- **THEN** only those ids can enter its bounded plan, and selection still confirms the exact route before dispatch

### Requirement: Automatic refresh SHALL be limited to eligible prepaid routes

Automatic candidates SHALL be addressable, currently admitted FREE or SUBSCRIPTION routes that are STALE, UNVERIFIED, or eligible half-open, without active entitlement, cooldown, identity, or policy gates. Fresh entries, EXCLUDED and INVENTORY_ONLY entries, and active gated routes SHALL NOT be automatically probed. Inventory/capacity alone SHALL NOT authorize a probe. Metered and unknown routes SHALL NEVER auto-probe; they SHALL require explicit manual confirmation. The automatic authorization SHALL be a distinct bounded prepaid authorization, not a fabricated `confirmed=True` manual consent.

#### Scenario: Mixed capacity page does not spend metered quota automatically
- **WHEN** a page contains eligible stale FREE, SUBSCRIPTION, METERED, and UNKNOWN routes
- **THEN** only eligible FREE/SUBSCRIPTION candidates enter automatic refresh and the others retain last-known status with requires_confirmation

#### Scenario: Active scoped blocker prevents dispatch
- **WHEN** a stale subscription route has an active provider or pool authentication/cooldown gate
- **THEN** no automatic model request is made for that route and its applicable unavailable status remains visible

### Requirement: Candidate ordering SHALL be bounded and provider-fair

Only supplied needed ids or the filtered page SHALL enter a plan. Priority SHALL be explicit picker/selection ids, previously validated success now stale, half-open negatives, then never-verified admitted routes in canonical ladder rank order. Within each priority the system SHALL round-robin by provider with stable route-id ties and unknown rank last. Ranking SHALL be read-only and SHALL NOT select a route or mutate ladder state.

#### Scenario: One provider cannot consume the first entire plan
- **WHEN** equal-priority stale candidates from several providers exceed the route cap
- **THEN** provider round-robin admits candidates fairly with stable ordering rather than exhausting one provider first

#### Scenario: View refresh does not sweep unseen inventory
- **WHEN** the default 50-row page is shown from an inventory of thousands of routes
- **THEN** only needed page identities can enter the capped plan and no catalog-wide sweep is started

### Requirement: Refresh budgets SHALL be enforced before every dispatch

Each trigger SHALL default to at most 20 routes, 40 HTTP model requests, 120 seconds wall time, and concurrency 4. Hard maxima SHALL be 50 routes, 100 requests, 120 seconds, and concurrency 4. `VERDICT_REFRESH_MAX_ROUTES`, `VERDICT_REFRESH_MAX_REQUESTS`, `VERDICT_REFRESH_WALL_SECONDS`, and `VERDICT_REFRESH_CONCURRENCY` SHALL accept settings only within valid positive bounds and hard maxima; invalid configuration SHALL fail before model calls.

Initial inventory/connection reads SHALL not count as model requests but SHALL share the overall consumer wait deadline and SHALL NOT become unbounded reads after the job starts. Full probes SHALL reserve two requests atomically before launch, honor provider/provider-pool token buckets, and release unused reservations after chat failure. Provider buckets SHALL preserve the existing default of 10 requests per 60 seconds. Actual request count SHALL never exceed the cap. Deadline/cancellation and reservations SHALL be checked before each chat and tool dispatch. Call timeout SHALL be the smaller of 15 seconds and remaining deadline; only deadline-respecting transports SHALL be supported. There SHALL be no automatic retries or automatic agentic calls, and at most four workers SHALL execute concurrently.

#### Scenario: Default job cannot exceed its budgets
- **WHEN** more than 20 eligible routes are available under default settings
- **THEN** no more than 20 routes and 40 model requests are dispatched, no more than four workers run at once, and the whole consumer wait is bounded to 120 seconds

#### Scenario: Invalid maxima do not start a job
- **WHEN** a setting requests 51 routes, 101 requests, more than 120 seconds, concurrency 5, or a nonpositive/invalid value
- **THEN** configuration is refused before model dispatch

#### Scenario: Chat failure releases unused tool reservation
- **WHEN** a full probe reserves two requests but its chat phase fails
- **THEN** only the actual chat request is counted as used and the unused tool reservation is released without exceeding the job cap

#### Scenario: Deadline is checked between phases
- **WHEN** a chat phase completes but cancellation or the wall deadline occurs before tool dispatch
- **THEN** the tool request is not sent and the route does not receive completed full-proof status

### Requirement: Concurrent consumers SHALL share one bounded single-flight job

There SHALL be one nonblocking cross-process active-job lock per configured health cache, held for the whole job, with one owner and serialized writes. Atomic progress/in-progress metadata SHALL include job id/digest, owner, exact ids, start/deadline, per-route outcomes, and monotonic sequence without secrets; metadata SHALL stay beside the configured cache. Other triggers SHALL JOIN and WAIT rather than queue a second sweep or append ids. Uncovered needed ids SHALL remain unrefreshed with `joined_job_not_covered`. A matching page/id trigger completed within 10 seconds SHALL debounce with no new model calls and a disclosed prior-result note; picker/selection freshness checks SHALL still occur. A stale crashed marker without a live lock SHALL be discarded and unfinished routes SHALL be NOT_TESTED, never healthy. All probe writers, including background proving, SHALL honor the same job/writer gate.

#### Scenario: Second trigger joins rather than doubles spend
- **WHEN** a second consumer requests refresh while one job owns the cache lock
- **THEN** it waits on that job, starts no second sweep, adds no routes, and labels any uncovered needed ids joined_job_not_covered

#### Scenario: Repeated matching trigger is debounced
- **WHEN** a matching page/id request arrives within 10 seconds after job completion
- **THEN** it reuses the prior result with a debounce note and sends no new model requests

#### Scenario: Abandoned job marker cannot mint success
- **WHEN** a progress marker remains after its owner dies and no live lock exists
- **THEN** it is discarded and unfinished identities are NOT_TESTED rather than recorded as healthy

### Requirement: Progress and cancellation SHALL keep consumers responsive

Waiting consumers SHALL remain responsive and receive bounded events containing probed n/N, verified/failed/unavailable counts so far, actual/reserved requests, elapsed time, per-provider counts, last sanitized reason, and job id. Workers SHALL NOT mutate UI directly. Cross-process waiting SHALL be bounded and deadline-aware rather than busy polling.

Esc/Ctrl-C SHALL request shared-job cancellation, including from a joined consumer. Cancellation SHALL stop future dispatch, wait only for current calls within the remaining deadline, and then render last-known evidence with a `cancelled/last_known` banner. All waiters SHALL see the same cancellation. Completed trustworthy results SHALL remain persisted; incomplete or obsolete post-cancel results SHALL NOT invent proof or renew prior health.

#### Scenario: Joined consumer cancels the shared job
- **WHEN** a joined TUI consumer presses Esc while calls are in flight
- **THEN** the shared job stops new dispatch, current calls remain deadline-bounded, all waiters observe cancellation, and final last-known evidence has the cancelled/last_known banner

#### Scenario: Progress contains no provisional list
- **WHEN** a refresh has completed some but not all routes
- **THEN** progress reports bounded aggregate/per-provider outcomes without rendering a provisional status table or granting intermediate launch authority

### Requirement: Incomplete work SHALL preserve prior proof without negative poisoning

At route, request, wall, bucket, or joined-coverage limits, final evidence SHALL retain the applicable STALE, UNVERIFIED, negative, or availability status of unrefreshed routes and disclose `route_cap`, `request_cap`, `wall_cap`, `bucket`, or `joined_job_not_covered`. Partial chat without a completed required tool phase SHALL be NOT_TESTED full proof, SHALL leave prior proof unchanged, and SHALL NOT record a negative or renew coding health. Skips from caps, buckets, or cancellation SHALL NOT be response-validation failures. Fresh unselected rows SHALL retain their existing freshness. Completion SHALL NOT imply every candidate was tested.

#### Scenario: Request limit prevents partial-proof promotion
- **WHEN** chat has completed but no permitted tool dispatch can occur because of budget, deadline, or cancellation
- **THEN** the full result is NOT_TESTED, no positive or negative route proof is written for that partial result, and prior proof timestamps remain unchanged

#### Scenario: Capped stale route remains stale
- **WHEN** a stale candidate is omitted by the route cap
- **THEN** final status remains STALE with route_cap rather than being reported as fresh or failed

### Requirement: Manual planning SHALL be a pure disclosed spend plan

`models.refresh.plan` SHALL read only supplied/local snapshots and SHALL NOT perform provider GETs, construct a live transport, consume buckets, acquire locks, create markers, or write files. Default manual coverage SHALL be the shown filtered page's STALE/UNVERIFIED/half-open candidates; wider bounded coverage SHALL disclose its exact list. Fresh re-probing SHALL require an explicitly disclosed, confirmed force flag. Manual plans SHALL obey the same hard maxima.

The plan SHALL contain `schema="verdict.model-refresh-plan/v1"`, consumer, normalized gateway origin, evidence generation, creation and 120-second validity deadline, filters/page, needed ids, pre-cap candidate count, exact chosen routes with provider/capacity/probe kind, selected/omitted counts, caps, `estimated_requests=2*selected_count`, requires-confirmation, and plan digest. The spend note SHALL disclose: `SUBSCRIPTION and METERED probes may consume quota; FREE can also have provider limits; UNKNOWN may incur charges; currency estimate unavailable`. The digest SHALL be SHA256 over canonical JSON binding endpoint, exact ids, filters, caps/kind/capacity, evidence generation, and dates without a token; it SHALL NOT be treated as authentication.

#### Scenario: Metered plan is inspectable without spend
- **WHEN** the operator requests a manual plan for metered or unknown candidates
- **THEN** the exact selected list, count, omissions, caps, two-request estimate, quota/spend warning, and validity/digest are returned with no provider access, probes, locks, or writes

#### Scenario: Wider coverage cannot be hidden
- **WHEN** manual coverage extends beyond the shown page or includes forced fresh re-probes
- **THEN** the bounded selected identities and force behavior are disclosed in the plan before consent

### Requirement: Manual execution SHALL require exact affirmative plan consent

`models.refresh.execute` SHALL require `confirmed=True`, the exact plan and matching digest, an unexpired plan, and unchanged gateway, selected identities, capacity, and relevant gates. Missing/false consent, digest mismatch, expiry, or changed plan facts SHALL refuse execution with zero model calls or writes and no live transport construction. Gates SHALL be re-read before dispatch; a newly blocked candidate SHALL be skipped, and the approved set SHALL never expand or increase spend without a new plan and consent. Valid execution SHALL use the same single-flight, budgets, persistence, and cancellation path as automatic refresh.

#### Scenario: Missing or stale consent cannot create transport
- **WHEN** consent is absent/false, a digest is wrong, a plan has expired, or its gateway/identity/capacity facts have changed
- **THEN** execution is refused without constructing live transport, sending model requests, or writing cache/job state

#### Scenario: New blocker narrows but never expands consent
- **WHEN** a validly confirmed route becomes blocked before dispatch
- **THEN** it is skipped and no substitute route or increased request budget is added without replanning and new consent

### Requirement: Full refresh proof SHALL validate both phases and exact identity

Refresh SHALL use the existing two-step chat then required tool-use proof, not a background liveness cycle or arbitrary successful HTTP response. Aggregate identity validation SHALL cover both phases; an actual model mismatch SHALL defeat nominal success. A successful full probe SHALL update coding proof only when both phases and exact identity validate. Automatic refresh SHALL NOT run agentic tests, and coding proof SHALL NOT renew independent agentic timestamps. Manual `/probe` liveness SHALL remain distinct from automatic/full coding verification.

#### Scenario: Tool-phase identity mismatch defeats chat success
- **WHEN** chat reports the requested model but the tool phase reports a different model
- **THEN** the result is a model_mismatch failure, never fresh verified/coding proof

#### Scenario: Coding proof does not satisfy an expired agentic gate
- **WHEN** full chat/tool proof succeeds while separate agentic proof is expired
- **THEN** coding metadata can be fresh, but the independent FREE agentic launch gate remains unsatisfied until its existing confirmation requirements are met

### Requirement: Failures SHALL preserve canonical category, scope, and deadline policy

Refresh SHALL use canonical failure scope/action, existing at-rest cache categories, and the cache's negative TTL policy. Structured HTTP status SHALL take precedence over loose text, except actual identity mismatch SHALL override nominal success. Model-specific permission/quota detail SHALL be preserved safely. Cache retry deadlines SHALL NOT be silently replaced with runtime cooldown defaults. Outcomes SHALL follow this contract:

| Outcome | Cache category and projected active status | Scope and deadline |
|---|---|---|
| Timeout or missing response | `timeout`; FAILED absent independent availability block | Route retry, 60 seconds doubling to 900 |
| HTTP 401 | `authentication`; UNAVAILABLE | Provider, 6 hours doubling to 24 hours |
| HTTP 402 | `payment_required`; UNAVAILABLE | Provider, 6 hours doubling to 24 hours |
| HTTP 403 | `permission`; UNAVAILABLE | Provider, or explicit model-denial route, 6 to 24 hours |
| HTTP 404 | `not_found`; FAILED | Route retry, 6 to 24 hours; runtime category model_unavailable |
| HTTP 410 | `gone`; FAILED | Explicit route handling, 6 to 24 hours, not unknown fallback |
| HTTP 429 | `rate_limited` with canonical quota detail; UNAVAILABLE | Provider or explicit per-model quota route; positive Retry-After, otherwise 60 seconds; affected provider/pool bucket zeroed |
| HTTP 5xx | `upstream`; FAILED | Route retry, 60 seconds doubling to 900; runtime upstream_temporary |
| Identity mismatch | `model_mismatch`; FAILED | Route retry, current cache 60 to 900 seconds, separate from runtime default |
| Catalog ghost | `catalog_stale`; FAILED | Route retry, 6 to 24 hours |
| Wrong chat/tool or malformed proof | Existing canonical response-failure category, never ok; FAILED | Transient route retry; cap/bucket/cancel skips excluded |
| Context overflow or gateway busy | No route-health write | Request/infra skip reason, no route/provider poisoning |

Provider auth/payment/permission/rate-limit failure SHALL stop later dispatch for the affected scope during the cycle. Already-in-flight calls SHALL remain deadline-bounded and SHALL NOT clear that blocker. Route-specific 403 or per-model quota SHALL stop that route only.

#### Scenario: Structured failure cannot be hidden by text
- **WHEN** HTTP 401 accompanies loose success-like text
- **THEN** provider authentication unavailability is persisted with cache TTL policy and later provider dispatch stops

#### Scenario: Provider failure survives sibling completion
- **WHEN** one route yields provider HTTP 429 while a sibling call is already in flight
- **THEN** Retry-After or the 60-second fallback applies, the affected bucket is zeroed, no new scoped calls start, and sibling success cannot clear the block

#### Scenario: Model-specific permission does not poison its siblings
- **WHEN** structured HTTP 403 explicitly denies only the requested model
- **THEN** that route becomes UNAVAILABLE with route scope and sibling routes are not assigned a provider-wide denial

#### Scenario: Gone route is a diagnostic failure
- **WHEN** a full probe returns HTTP 410
- **THEN** it records gone with a route retry deadline and projects FAILED, not an unknown-category or provider availability failure

#### Scenario: Request and infrastructure failures do not poison health
- **WHEN** an attempt reports context_length_exceeded or gateway_busy
- **THEN** prior route health remains unchanged and the refresh records a request/infra skip reason without route or provider negative proof

### Requirement: Persistence SHALL be additive, serialized, and health-cache only

Only the at-rest health cache SHALL store refresh proof; refresh SHALL NOT write ladder state, worker cache, or admission receipts. Its schema-version-1 envelope SHALL remain backward compatible, adding optional scoped cooldown metadata and per-route retained `last_success_at`/failure-scope/probe-outcome metadata without requiring migration. Missing optional fields SHALL remain valid. Serialized reload/merge/write SHALL preserve concurrent evidence under the single writer gate.

Availability-scope blockers SHALL be persisted even if a route proof already failed, so sibling positive rows cannot remain VERIFIED under an active provider block. Diagnostic route retry gates SHALL remain negative entries, not availability cooldowns. Route `checked_at` SHALL remain last attempt; failure SHALL retain earlier validated success where available without inventing legacy history. Rereading evidence SHALL NOT extend TTL. Recovery SHALL replace only the exact resolved negative/scope; sibling success SHALL NOT remove a provider cooldown.

#### Scenario: Failure retains known history without renewing it
- **WHEN** a previously validated route later fails a complete probe
- **THEN** its last attempt and negative metadata are persisted, earlier validated last_success_at is retained if available, and the old success's freshness is not renewed

#### Scenario: Old cache remains readable
- **WHEN** a schema-version-1 cache lacks the new optional cooldown and success-history fields
- **THEN** the view accepts independently valid legacy fields conservatively without migrating or rewriting other evidence stores

#### Scenario: Serialized writers do not lose a provider block
- **WHEN** refresh and a background probe complete near the same time
- **THEN** they use the same writer gate and merged cache retains applicable scope blockers and trustworthy route results rather than last-writer overwrite

### Requirement: Refreshed evidence SHALL remain negative-only runtime input

Runtime admission SHALL consult applicable unexpired at-rest negatives and scoped cooldowns through a distinctly named negative-only source, in addition to existing negative sources. It SHALL omit every at-rest positive as launch authority. Refresh SHALL not launder positive proof through a new receipt, extend TTL by reread, or bypass exact-route confirmation. Selection SHALL apply current negative gates before confirmation/dispatch and retain the separate FREE agentic launch requirement.

#### Scenario: Fresh cache does not launch without confirmation
- **WHEN** a route has fresh successful full proof and is selected for dispatch
- **THEN** existing exact-route confirmation is still required and cached positivity alone cannot authorize launch

#### Scenario: At-rest provider negative prevents dispatch
- **WHEN** an unexpired at-rest provider authentication cooldown applies to the selected route
- **THEN** runtime admission rejects that route before confirmation/dispatch even if another store contains positive hints

### Requirement: TUI manual refresh and probe SHALL use default-No consent

`/eligibility refresh` SHALL show the exact plan, selected count, cap, estimated requests, and quota/spend warning and ask `[y/N]`. Blank, n, EOF, Esc, or Ctrl-C SHALL cancel with no live transport calls; only affirmative y SHALL execute the digest-bound plan. After consent the TUI SHALL show live progress, then final counts and per-route failure/skip reasons after bounded completion.

`/probe` inline, prompted, and palette paths SHALL share one model-list parser and consent controller. Comma- and whitespace-separated exact ids SHALL become `list[str]`, be deduplicated in order, reject empty lists, and preserve `/` and `:` within ids. The controller SHALL show the chosen list/count and quota warning, ask `[y/N]`, and only after affirmative consent set the existing live-probe gate. It SHALL NOT require an unreachable TUI flag. Existing CLI `--allow-live-probe` behavior SHALL remain intact. `/probe` SHALL remain its existing bounded liveness action rather than automatic coding proof.

#### Scenario: Default-No manual refresh sends no requests
- **WHEN** `/eligibility refresh` shows a plan and the operator answers blank/n, ends input, or cancels
- **THEN** the action makes no live transport calls and retains last-known evidence

#### Scenario: Probe paths parse identical exact lists
- **WHEN** inline, prompted, or palette input supplies `cc/x, kr/y cc/x provider:model-z`
- **THEN** each path produces `cc/x`, `kr/y`, `provider:model-z` in that order and asks for explicit y/N consent before setting the live gate

#### Scenario: Empty probe list cannot launch
- **WHEN** probe input contains only separators or whitespace
- **THEN** it is rejected and no live-probe gate or model call is created

#### Scenario: Probe affirmation enables existing bounded liveness
- **WHEN** the operator affirms y after the selected list/count and warning
- **THEN** the existing live-probe gate is set explicitly and the bounded liveness action runs without claiming new full coding proof

### Requirement: Autocomplete SHALL never probe or wait for refresh

Autocomplete SHALL use only the local snapshot projection. It SHALL NOT perform gateway reads, start/join a refresh job, wait for proof, acquire refresh locks, or send model calls, even with stale/unverified routes, enabled auto-refresh, or another job in flight. Picker preview/apply and selection SHALL use the explicit wait/refresh API instead; their refresh authorization SHALL not leak into completion.

#### Scenario: Stale autocomplete remains immediate and local
- **WHEN** autocomplete reads stale local routes while auto-refresh is enabled and another refresh is active
- **THEN** it returns the local snapshot without network reads, probing, lock/join, or waiting
