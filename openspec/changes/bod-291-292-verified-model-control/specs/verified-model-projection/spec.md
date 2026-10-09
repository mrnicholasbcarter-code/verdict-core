# Spec Delta: Verified model projection and surfaces

## Purpose

Give operators one shared, unscoped view of current model identities and their validated health evidence, without treating inventory, cached hints, or display status as launch authority.

## ADDED Requirements

### Requirement: The verified model projection SHALL be pure and read-only

The system SHALL project a shared verified-model view from supplied inventory, connections, evidence snapshots, current read-only policy/admission/visibility facts, an explicit timezone-aware time, and a query. The projection SHALL normalize time to UTC and SHALL NOT read a clock, disk, or network; probe a model; select a route; confirm admission; refresh a registry; or write evidence, visibility, or a receipt. Cached positive evidence SHALL be display/scheduling evidence only, never launch permission. Lower-trust ladder, worker-cache, and receipt positives SHALL remain timestamped hints and SHALL NOT establish VERIFIED or STALE verification.

#### Scenario: Supplied snapshots produce a deterministic view
- **WHEN** the same snapshots, query, and aware time are projected twice
- **THEN** the rows, counts, ordering, and timestamps are identical, with no external reads, probes, selection, confirmation, or writes

#### Scenario: Positive hints cannot establish verification
- **WHEN** an admitted inventory route has only ladder, worker-cache, or receipt-positive hints
- **THEN** its status is UNVERIFIED, its hints retain their source timestamps, and the view grants no launch authority

### Requirement: Canonical identities and evidence SHALL be validated before use

The system SHALL remove only the leading `omniroute/` from a route identity, preserve the remaining exact model id, and resolve provider aliases without merging identical suffixes from different providers. It SHALL map worker selector identities and scoped provider/pool/account evidence only to their applicable identities. It SHALL emit one row per canonical inventory identity; contradictory duplicate rows SHALL fail closed with a source error and restriction rather than last-writer promotion. Evidence-only orphan identities SHALL NOT become current candidates.

At-rest evidence SHALL be accepted only from supported schema version `1`, with matching map key and route identity, literal booleans, known category/identity values, aware dates, and `checked_at` no later than the supplied time. Malformed or future-dated evidence SHALL NOT promote health. Independently valid blockers SHALL survive another source's validation failure. A missing at-rest file SHALL be normal missing proof, not a positive fallback.

#### Scenario: Provider identities remain distinct
- **WHEN** inventory contains `omniroute/cc/model-x` and `kr/model-x`
- **THEN** the view contains distinct `cc/model-x` and `kr/model-x` rows and does not transfer proof between them

#### Scenario: Contradictory inventory duplicates fail closed
- **WHEN** two inventory rows canonicalize to one id but disagree on facts needed for admission or capabilities
- **THEN** one restricted row and an explicit source error are emitted, and the later row cannot promote it to VERIFIED

#### Scenario: Invalid positive evidence cannot hide a valid blocker
- **WHEN** a positive entry has an unsupported schema, mismatched identity, malformed booleans, or future timestamp and an independently valid provider cooldown is active
- **THEN** the invalid positive is ignored and reported, and the route remains UNAVAILABLE under the cooldown

#### Scenario: Orphan evidence does not expand inventory
- **WHEN** a valid cache entry refers to an identity absent from current inventory
- **THEN** no current candidate or refreshable row is created for that identity

### Requirement: Each route SHALL have one conservative status

The view SHALL assign one of EXCLUDED, INVENTORY_ONLY, UNAVAILABLE, FAILED, VERIFIED, STALE, or UNVERIFIED, retaining secondary restrictions. Status selection SHALL follow this precedence:

1. Current explicit policy/controller or declared capability/context exclusion produces EXCLUDED. Opaque, unaddressable inventory or inventory without an admissible execution identity produces INVENTORY_ONLY. Neither is refreshable. Absence of tools alone SHALL NOT exclude a chat-only route in the default unscoped view.
2. Current missing/inactive account, missing required connection/harness visibility, entitlement/auth/payment/permission denial, rate limit/quota, or applicable active route/provider/pool/account cooldown produces UNAVAILABLE. Active availability blockers SHALL defeat any positive evidence regardless of age or source. A narrowed or expired receipt alone SHALL NOT establish a current blocker.
3. Active exact-route diagnostic negative evidence produces FAILED when no availability blocker wins. Diagnostics include timeout, upstream, not-found/gone/catalog-stale, identity mismatch, and response/tool validation failure. A diagnostic retry deadline alone SHALL NOT become an availability cooldown; an explicitly recorded active cooldown SHALL take precedence.
4. A validated fresh at-rest positive without a winning blocker produces VERIFIED.
5. A validated at-rest success history at or after its fresh boundary produces STALE, including history beyond usable expiry.
6. A currently admissible route without validated positive or active negative proof produces UNVERIFIED, including absent proof, unverified identity, lower-trust positives, and half-open expired negatives.
7. Remaining non-admissible or undetermined discovered identities produce INVENTORY_ONLY with a reason. An otherwise addressable route missing connection evidence SHALL be UNAVAILABLE, not inventory-as-entitlement.

#### Scenario: Availability defeats successful proof
- **WHEN** a route has fresh valid success evidence and its provider has an active quota cooldown
- **THEN** the route is UNAVAILABLE with the scoped deadline and quota category, not VERIFIED

#### Scenario: Diagnostic failure remains distinct from unavailability
- **WHEN** an active exact-route timeout is present without an independent availability blocker
- **THEN** the route is FAILED with its category and deadline, not UNAVAILABLE

#### Scenario: Default view includes chat-only routes
- **WHEN** an addressable admitted route declares `tools=false` and has fresh validated chat success
- **THEN** it is VERIFIED with `coding_ok=false`, not EXCLUDED by an implicit coding gate

#### Scenario: Explicit exclusion is visible
- **WHEN** current policy excludes a route that also has fresh positive proof
- **THEN** the route is EXCLUDED and retains the other applicable restrictions without granting launch authority

### Requirement: Competing blockers SHALL have deterministic conservative precedence

Within availability blockers, provider/pool/account scope SHALL precede route scope. Category order SHALL be authentication, payment_required, permission, quota_exhausted/rate_limited, then other cooldown. Remaining ties SHALL use latest applicable deadline, newest observed time, then stable source order at-rest, ladder, worker, current admission. Exact-route diagnostic negatives SHALL use latest valid observation, with the same source tie-break. A positive from another store SHALL NOT override an older active negative. Sibling success SHALL NOT clear a provider/pool/account blocker; recovery SHALL be deliberate and apply to the exact recovered route or scope.

#### Scenario: Provider scope wins over a route blocker
- **WHEN** an active provider authentication block and a newer route rate-limit block both apply
- **THEN** the authentication provider block is primary and both restrictions remain visible

#### Scenario: Newer low-trust positive does not erase failure
- **WHEN** a worker or receipt positive is newer than an active exact-route at-rest failure
- **THEN** the view remains FAILED unless a higher-precedence availability or exclusion fact applies

### Requirement: Freshness SHALL use exact half-open boundaries

Validated at-rest success SHALL be fresh only before `checked_at + 600 seconds`, usable stale from that boundary until `checked_at + 1800 seconds`, and expired success history at or after the latter boundary. Expired known success SHALL remain STALE with `freshness=expired`, never healthy and never erased solely by age. Positive `fresh_until` and `expires_at` SHALL come from these boundaries, not a healthy entry's `until` field.

At-rest negative evidence SHALL expire at `until`, worker negatives at `expires_at`, and ladder negatives at `checked_at + 300 seconds`; equality SHALL be expired/half-open. The view SHALL expose applicable negative deadlines. `last_success_at` SHALL use validated retained at-rest history only and SHALL NOT be invented from inventory or hint timestamps. Agentic freshness SHALL use its own observation time; a later liveness timestamp SHALL NOT renew it.

#### Scenario: Exact positive boundaries do not extend health
- **WHEN** valid positive evidence is evaluated at ages 599, 600, 1799, and 1800 seconds
- **THEN** it is respectively VERIFIED/fresh, STALE/stale, STALE/stale, and STALE/expired with the fixed success boundaries

#### Scenario: Exact negative boundary becomes half-open
- **WHEN** an admitted route has only negative evidence whose deadline equals the supplied time
- **THEN** the negative is inactive and the route is UNVERIFIED with a half-open reason, not FAILED or VERIFIED

#### Scenario: Liveness does not renew agentic proof
- **WHEN** recent liveness proof accompanies an older expired agentic observation
- **THEN** liveness freshness and agentic metadata remain separate, and the older agentic proof is not renewed

### Requirement: Verified and coding labels SHALL require distinct proof

VERIFIED SHALL require a validated at-rest entry with `healthy=true`, `category=ok`, `chat_ok=true`, and `identity=verified`. Missing, legacy, or `not_reported` identity SHALL NOT establish verification, even after HTTP 200, and SHALL carry `identity_not_verified`. `coding_ok=true` SHALL additionally require fresh unobstructed full/tool proof with `tool_ok=true` and no explicit `tools=false` declaration. Chat-only VERIFIED rows SHALL have `coding_ok=false` and `chat_only_not_coding_verified`. STALE, FAILED, UNAVAILABLE, and UNVERIFIED rows SHALL have `coding_ok=false`. Coding proof SHALL NOT imply independent agentic proof or exact launch confirmation.

#### Scenario: HTTP success without reported identity is unverified
- **WHEN** an admitted route has successful chat evidence but `identity=not_reported` and no winning negative
- **THEN** it is UNVERIFIED with `identity_not_verified` and `coding_ok=false`

#### Scenario: Fresh full proof establishes coding evidence only
- **WHEN** fresh chat/tool proof has verified identity, no blockers, and declared tools are true or unknown
- **THEN** the row is VERIFIED with `coding_ok=true`, while agentic proof and runtime launch admission remain separate

### Requirement: Verified JSON SHALL expose a stable complete schema

The view SHALL serialize with `schema="verdict.verified-models/v1"`, all defined keys, UTC ISO8601 `Z` timestamps or null, and null for unknown facts rather than invented zero/true values. The envelope SHALL contain `generated_at`, `rows`, `counts_by_status`, `filtered_counts_by_status`, `total_count`, `filtered_count`, `filters` with `status/provider/search`, one-based `page`, `page_size`, `page_count`, and `source_errors`. Both count maps SHALL include all seven statuses. Full counts SHALL reconcile to full deduplicated inventory, and filtered counts SHALL reconcile before paging.

Each row SHALL expose the following fields, including null-valued metadata rather than omitting it:

| Fields | Contract |
|---|---|
| `route_id`, `provider`, `status` | Exact canonical id, resolved provider, and one defined status |
| `coding_ok`, `last_success_at` | Boolean coding proof and validated retained success time or null |
| `checked_at`, `evidence_source` | Winning observation or null; source is `health_cache`, `ladder_state`, `worker_health`, `admission`, `connections`, `inventory`, `policy`, or `harness_visibility` |
| `fresh_until`, `expires_at`, `freshness` | Defined freshness boundaries/deadline or null; freshness is `fresh`, `stale`, `expired`, `negative`, or `none` |
| `capabilities` | `context_window` integer or null; `tools` and `structured` boolean or null; declared facts, not proof |
| `restriction`, `reason`, `restrictions` | Nullable primary code, sanitized stable reason, and all applicable restriction codes |
| `cooldown_until`, `cooldown_scope` | Nullable deadline and applicable scoped binding without credentials |
| `failure_category`, `http_status`, `latency_ms` | Nullable failure category, integer HTTP status, and numeric latency |
| `identity`, `probe_class`, `agentic_ok`, `agentic_checked_at` | Independent proof metadata or null |
| `capacity_class`, `refreshable`, `refresh_reason`, `hints` | `free/subscription/metered/unknown`, bounded-refresh eligibility and reason, and timestamped lower-trust hints |

#### Scenario: Empty inventory still has a complete envelope
- **WHEN** the supplied inventory is empty
- **THEN** all envelope keys are present, both seven-status count maps contain zeros, rows are empty, page is 1, and page_count is 0

#### Scenario: Unknown capability is not fabricated
- **WHEN** a route has no declared context, tools, or structured capability and no latency observation
- **THEN** those values are null, while all row keys remain present

#### Scenario: Filter counts do not depend on page
- **WHEN** a filter matches 75 of 100 deduplicated routes and a 50-row page is requested
- **THEN** full counts reconcile to 100, filtered counts reconcile to 75, and pagination returns at most 50 rows without changing either total

### Requirement: Spend guarding SHALL NOT erase health evidence

The view SHALL expose `capacity_class`, `refreshable`, and `refresh_reason` separately from status. Metered or unknown capacity SHALL carry `refresh_reason=requires_confirmation` when refresh is requested; that guard SHALL NOT replace a valid VERIFIED, STALE, FAILED, or UNAVAILABLE status. Never-probed metered/unknown routes SHALL remain UNVERIFIED with the confirmation note. Inventory capacity alone SHALL NOT establish entitlement or proof.

#### Scenario: Metered stale success retains its status
- **WHEN** an admitted metered route has expired validated success history
- **THEN** it remains STALE with `freshness=expired` and a requires-confirmation refresh reason, not an invented success or generic unverified replacement

### Requirement: Filters, sorting, and paging SHALL be bounded and unscoped

The default query SHALL have no scope, task, tools, context, or coding floor. Status enum, exact alias-resolved provider, and case-insensitive substring search over route id/provider/reason SHALL combine conjunctively without regex evaluation. Invalid page, page size, or filter SHALL be rejected before I/O. Page size SHALL default to 50 and clamp above 200 to 200; an out-of-range positive page SHALL clamp to the last valid page. Empty results SHALL use page 1 and page_count 0.

Status order SHALL be VERIFIED, STALE, FAILED, UNAVAILABLE, UNVERIFIED, INVENTORY_ONLY, EXCLUDED. VERIFIED ties SHALL use newest `last_success_at`, then lower known latency with null last, then route id. Other statuses SHALL use newest valid `checked_at`, then route id. Projection SHALL use indexed evidence/provider lookups and O(n log n) sorting. A 7,000-row offline fixture SHALL project in under two seconds on supported CI hardware, and rendering SHALL construct only a bounded page rather than a full-inventory table.

#### Scenario: Independent filters select one bounded page
- **WHEN** status STALE, provider alias `cc`, search `SONNET`, page 2, and page size 500 are requested
- **THEN** all filters apply together, search is case-insensitive, page size is 200, and only the selected bounded page is rendered

#### Scenario: Invalid paging does not trigger a read or refresh
- **WHEN** page zero, a nonpositive page size, or an unknown status is supplied
- **THEN** the request is rejected before inventory I/O or model refresh

#### Scenario: Large inventory does not create an unbounded table
- **WHEN** an offline 7,000-route inventory is projected and its default page is rendered
- **THEN** projection meets the two-second fixture target and the renderer creates at most 50 model rows

### Requirement: Current gates SHALL NOT be inferred from a narrowed receipt

The shared view SHALL derive current read-only admission facts from full current inventory, connections, policy/visibility, and valid negatives. An old or narrowed admission receipt SHALL NOT exclude unseen routes or establish current verification. Missing policy/visibility facts SHALL remain explicit unknown restrictions rather than invented exclusions. Current declared gates SHALL stay visible without creating an implicit task scope.

#### Scenario: Narrowed historical receipt does not hide inventory
- **WHEN** a receipt contains only a past six-route selection but current inventory contains additional addressable identities
- **THEN** the additional identities remain in the unscoped view and are assessed from current facts, not excluded by receipt omission

### Requirement: Public evidence output SHALL contain no secrets

Rows, source errors, reasons, hints, scoped bindings, and rendered output SHALL sanitize provider error detail, secret-like tokens/URLs, control characters, and labels. They SHALL NOT expose credentials, authorization headers, account email, raw response bodies, or secret paths. Evidence-source labels SHALL be stable source names, not credential-bearing paths. Sanitization SHALL retain enough nonsecret category/scope detail to explain restrictions.

#### Scenario: Provider detail is safe to display
- **WHEN** an evidence error contains an authorization token, account email, control characters, and a model-scoped denial
- **THEN** JSON and text omit the secrets/control characters while retaining the canonical denial category and safe route scope

### Requirement: The shared read action SHALL remain separate from refresh triggers

The shared read action `models.verified` SHALL read snapshots and optionally inventory/connection metadata using the normalized gateway origin, then return the shared projection. It SHALL NOT make model calls or write evidence, admission receipts, or harness visibility. Its `local_only=True` mode SHALL use cached inventory/connection snapshots without network refresh. Calling the read action alone SHALL NOT start or wait for a refresh job; consumer-triggered refresh SHALL remain separate.

#### Scenario: Snapshot read is not an implicit probe
- **WHEN** `models.verified` is called alone with stale evidence
- **THEN** it returns the snapshot projection with zero model calls, refresh jobs, or evidence/receipt/visibility writes

#### Scenario: Local-only read stays local
- **WHEN** local_only is requested with stale local inventory and proof
- **THEN** cached snapshots are projected without gateway GETs, probes, or refresh waiting

### Requirement: TUI eligibility SHALL use the unscoped shared consumer view

TUI `/eligibility` SHALL open the verified view without a scope prompt or default scope. `/eligibility stale`, `/eligibility provider=cc`, `/eligibility search=sonnet`, and `/eligibility page=2` SHALL parse independent arguments through one controller, with combinable status/provider/search/page/page-size filters. The view SHALL use bounded paging rather than generic first-50 truncation. The controller SHALL plan needed refresh, wait with progress only, reload evidence after completion, then project and render final rows. It SHALL NOT render a provisional model list while a refresh is running.

#### Scenario: Bare eligibility has no hidden scope
- **WHEN** the operator enters `/eligibility`
- **THEN** the unscoped verified view opens without asking for a task or scope and shows final statuses only after any needed bounded refresh completes

#### Scenario: TUI arguments preserve paging
- **WHEN** the operator enters `/eligibility stale provider=cc search=sonnet page=2`
- **THEN** the independent filters select the second bounded page without scope prompting or generic table truncation

#### Scenario: Progress precedes final rows
- **WHEN** the TUI waits on a non-fresh prepaid page refresh
- **THEN** only progress appears during the job, and final rows appear after completion and evidence reload

### Requirement: CLI verified mode SHALL preserve legacy eligibility behavior

`verdict eligibility --verified` SHALL use the same consumer sequence and shared projection as the TUI and accept `--status`, `--provider`, `--search`, `--page`, and `--page-size`, with optional `--json`. Without `--verified`, existing CLI defaults and `verdict eligibility --json` behavior SHALL remain unchanged, including `records`, stage counts, `selected`, and `filters`; only additive legacy keys SHALL be permitted. Combining `--verified` with `--probe`, scope, or task-only selector flags SHALL be rejected rather than inheriting a hidden scope.

Verified JSON stdout SHALL contain one final verified envelope and an additive `refresh` summary. Progress SHALL use stderr, never pollute JSON stdout, and final projection data SHALL match the TUI for the same inputs, apart from presentation and separately disclosed refresh metadata.

#### Scenario: Legacy JSON contract remains intact
- **WHEN** an existing caller runs `verdict eligibility --json` without --verified
- **THEN** legacy keys, values, selection behavior, and defaults remain compatible rather than switching to the verified schema

#### Scenario: Verified JSON is one final document
- **WHEN** `verdict eligibility --verified --json` refreshes a stale prepaid page
- **THEN** progress goes to stderr and stdout contains only one final `verdict.verified-models/v1` envelope with its refresh summary after evidence reload

#### Scenario: Incompatible selectors fail before refresh
- **WHEN** --verified is combined with --probe or a scope/task-only selector flag
- **THEN** the CLI rejects the combination without silently restricting the verified inventory or making refresh calls
