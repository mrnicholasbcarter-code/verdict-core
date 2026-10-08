# Design: Verified models and bounded refresh

## Context

See `proposal.md` for motivation. **KNOWN** labels facts read from code/local evidence; **INFERRED** labels the chosen contract, not current behavior. Baseline: `origin/main` `f1bf0eb4`, plus BOD-290 gateway normalization `dd497e7` (current worktree HEAD). No provider/model calls were made. Only this OpenSpec directory is changed.

### Existing evidence stores (KNOWN)

| Store / writer | Shape and expiry | What it proves today |
|---|---|---|
| `verdict/orchestration/health_cache.py`, default `~/.verdict/health-cache.json`, `VERDICT_HEALTH_CACHE` override; written by `prove_at_rest.Prober` | Envelope `schema_version="1"`, `routes`, `buckets`, `cursor`. Route entry: `route_id`, `category`, `checked_at`, `until`, `consecutive_failures`, `healthy`, `chat_ok`, `tool_ok`; optional `latency_ms`, `http_status`, `pool`, `capacity_evidence`, `identity`, `probe_class`, `agentic_ok`, `agentic_checked_at`. `identity` distinguishes `verified`, `not_reported`, mismatch/legacy unknown. Healthy fresh before checked+600s, stale before checked+1800s, then unprobed. Negative active before `until`; equality is half-open/unprobed. | Only store with chat/tool evidence. Liveness can be healthy with tool_ok=false. `lookup.healthy`/`healthy_routes` include stale, so cannot be used directly for VERIFIED. The loader does not validate schema version, map-key binding, future time, or recompute healthy. The file is absent on this machine. |
| `~/.verdict/orchestration-health.json`; `EligibilityLadder._record_health`, `record_failure`, `record_success` | `health[route]={healthy,category,checked_at}`, TTL300s; `cooldowns[route:<id> / provider:<name>]={category,until}`; `not_free_overrides`. Present locally: 76 health rows, 68 cooldown rows. | Ladder uses healthy rows as scheduling hints. `admission.evidence_from_ladder_state` deliberately omits positives, includes current negatives and active cooldowns. Cached ladder healthy is NOT launch authority. |
| `~/.verdict/subagent-health.json`; DIFFERENT `subagent_selection.HealthCache` | Flat selector keys (`omniroute/cc/x`) and `provider:cc` keys; `{healthy,category,status_code,observed_at,expires_at}`. Healthy TTL300s; expires_at<=now ignored. Provider-first usable check; negative TTL differs from at-rest cache. Present locally. | Positives have no two-step/tool/identity proof. `admission.evidence_from_health_cache` means this flat worker cache, NOT at-rest health-cache.json; it ignores positives. |
| `~/.verdict/admission-latest.json`; `AdmittedSet.write_receipt` | Envelope includes schema, generated_at, digest, sources, narrowing, admitted, candidates. Per-route: admitted, first_failed_stage, reason, health, confirmation_source, confirmed_at, observed_at, until/reset_at. Present locally: 6,811 candidates, 5,130 admitted. | Snapshot of gates/confirmation, possibly scoped. Not a current inventory or cache verification proof. An old receipt or a narrowed receipt cannot exclude unseen routes or mint current VERIFIED. |
| Inventory `/v1/models` plus connections `/api/providers`; `eligibility_report.build_selector` | Live catalog + active-account/capacity/capability information, not successful execution. Gateway normalized to origin after BOD-290. | `build_selector` writes admission receipt/refreshes harness visibility and constructs a live probe closure even for evaluate. New read adapter must NOT use it for a pure/read-only view. Inventory is NOT health evidence. |

**KNOWN:** `EligibilityLadder.evaluate/_assess` separates entitlement, health, availability and task fit; `RouteVerdict` exposes stage/reason, capabilities and cooldown scope, not this verification vocabulary. `routing_view` has stable JSON and `routing_render` filters/pages with `_MAX_PAGE_SIZE=200`. `home._render_action_result` truncates generic tables to 50 rows. `/probe` prompt paths parse commas but inline dispatch assigns the entire string to the first parameter; its consent flag is inaccessible in the TUI. Existing CLI eligibility lives in `verdict/orchestration/cli.py` and calls the shared registry action.

**KNOWN:** Prober's full probe is chat then required tool use. Token buckets default to 10 requests/60s per provider or provider/pool. `ProverDaemon` gates live consent. `run_once` defaults to 300 requests/600s; its `concurrency=4` currently sets sequential batch size, not parallel dispatch. `order_cycle` chooses subscription/metered liveness, not always full probes. `_stop` does not stop the ordinary run_once loop; cursor is shared background-cycle state. Cache file replacement is locked/atomic, but read-modify-write and in-memory reservations are not multi-writer safe. An explicit bounded full-probe API is needed; merely calling run_once is incorrect.

**KNOWN:** `docs/guides/health-cache.md` describes the TTL/agentic intent but omits some identity fields and overstates concurrency/override behavior. Code governs this contract. `default_runtime_evidence` currently reads ladder + worker cache, not general at-rest positives/negatives. The ladder reads at-rest evidence only for the FREE agentic gate/receipt, not its general health status. Agentic proof has its own timestamp; a later liveness timestamp does not renew it.

## Goals / Non-Goals

**INFERRED:** One pure projection, one bounded refresh coordinator, and thin CLI/TUI adapters. No implicit scope or coding-only inventory gate. A row can be VERIFIED chat-only while `coding_ok=false`; no cached evidence is launch permission. Explicit policy/capability exclusions remain visible.

The operator's 2026-10-08 amendment overrides the story's explicit-refresh-only wording AND the earlier cache-first provisional rendering proposal. Consumers **wait then render/use**. The projection itself stays read-only. No provisional list while waiting. Autocomplete is snapshot-only and never waits. BOD-293 picker implementation and BOD-295 completion implementation are not owned here; provide their integration API. No new admission-positive authority, auto-agentic tests, catalog-wide sweep, cost-price guessing, or provider calls in tests.

## Decisions

### 1. Validate inputs and separate display evidence from launch authority

**INFERRED:** New `verdict/orchestration/verified_models.py` owns immutable dataclasses `EvidenceSnapshots`, `VerifiedModelRow`, `VerifiedModelsView`, `VerifiedModelQuery` and ONE public pure function:

```python
def project_verified_models(
    inventory_rows: Sequence[Mapping[str, Any]],
    connections: Sequence[Mapping[str, Any]] | None,
    evidence: EvidenceSnapshots,
    *, now: datetime, query: VerifiedModelQuery = VerifiedModelQuery(),
) -> VerifiedModelsView: ...
```

`now` is required, timezone-aware UTC-normalized. Snapshots contain parsed at-rest routes/buckets/scoped cooldowns, ladder health/cooldowns, worker health, legacy receipt hints, current unscoped admission/policy/visibility facts, and source errors. No disk/network/clock reads, writes, probes, selector.select, registry refresh, or admission confirmation in this function. Caller computes current canonical admission read-only from full inventory/connections and parsed negatives using `admit`; do not trust narrowed/stale receipt admission. No tools/context/coding floor by default. Missing policy/visibility facts remain explicit unknown restrictions, not invented exclusions.

Canonical route identity uses `admission.canonical_route_id` (strip only `omniroute/`, preserve the remaining exact model id); never collapse same suffix across providers. Resolve provider aliases using existing provider resolution. Map worker selector keys to that canonical id; provider keys apply only to that provider (and known pool/account bindings). Duplicate canonical inventory rows produce one row; contradictory duplicates fail closed with an explicit source error/restriction, not last-writer promotion. Evidence-only orphan routes are not current candidates.

Accept only supported schema1 at-rest entries with matching key/route identity, literal booleans, known category/identity fields, aware dates and checked_at<=now. Positive proof requires healthy=true, category=ok, chat_ok=true, identity=verified. Strict identity is intentional: `not_reported`/legacy missing identity becomes UNVERIFIED with `identity_not_verified`, even if HTTP200. Coding proof additionally requires tool_ok=true and no explicit tools=false capability. The bounded full-probe path must preserve aggregate identity validation across both phases, not only the chat echo. Malformed sources never make a row healthy; keep independently valid blockers and report source errors. A missing at-rest file is normal, not a verified fallback.

**Rationale / alternatives:** Ladder healthy and worker/receipt confirmations lack this proof and deliberately cannot mint admission authority. They are timestamped hints only, never VERIFIED or STALE verification. Allowing a lower-trust VERIFIED label would hide that distinction. Requiring reported identity is more conservative than existing liveness tolerance; providers that never echo it remain clearly unverified. The cache is local display evidence, not cryptographic/provider attestation.

### 2. One status per route, conservative precedence

**INFERRED:** Apply these rules in order; do not use newest-positive-wins across different stores. Preserve secondary restrictions even when another reason wins.

1. `EXCLUDED`: current explicit policy/controller exclusion, or current declared capability/context gate. `INVENTORY_ONLY`: opaque/unaddressable inventory identity or inventory with no admissible execution identity. Neither is eligible for refresh. No scope and no default coding gate means a chat-only route is NOT excluded simply for lacking tools.
2. `UNAVAILABLE`: current missing/inactive account, unavailable connections/required harness visibility, entitlement/auth/payment/permission denial, rate-limit/quota, or any applicable active route/provider/pool/account cooldown. All active scoped blockers beat positive evidence regardless age/source. Check at-rest, ladder, worker and current admission/connection facts. Expired or narrowed receipt-only blockers are not current facts. Auth/payment/permission/429 negatives map here even if only their route negative deadline is known. Active explicit cooldown + diagnostic failure => UNAVAILABLE, retaining failure category.
3. `FAILED`: active exact-route negative health without a winning availability blocker: timeout, upstream, not_found/gone/catalog_stale, model_mismatch, failed tool/response validation. Deadline comes from at-rest until, worker expires_at, or ladder checked_at+300s; equality is expired. Includes category/http_status/until. A transient at-rest retry deadline alone is not a separate availability cooldown. A ladder explicitly recorded cooldown does win rule2.
4. `VERIFIED`: validated fresh at-rest positive as above and no blocker. `coding_ok` is true only with fresh full/tool proof and declared tools!=false; chat-only is VERIFIED with coding_ok=false and `chat_only_not_coding_verified` restriction. Agentic freshness remains a separate fact/restriction, not implied by coding_ok.
5. `STALE`: validated at-rest positive with age>=600s. Age<1800s is usable stale; age>=1800s is expired success history (still STALE, never healthy). Expose expiry explicitly. Do not turn expired known success into healthy or erase its history.
6. `UNVERIFIED`: currently admissible route with no validated positive or active negative. Includes half-open expired negatives, absent cache, identity not reported, and ladder/worker/receipt-positive-only routes. Carry half_open or missing-proof reason and hints, never infer success time from inventory.
7. Remaining discovered but non-admissible/undetermined identity => `INVENTORY_ONLY` with reason. Missing connection evidence for an otherwise addressable route uses UNAVAILABLE, not inventory-as-entitlement.

When several blockers at one precedence level apply: provider/pool/account availability block before route availability block; category order authentication, payment_required, permission, quota_exhausted/rate_limited, other cooldown; then latest applicable deadline, newest observed time, stable source order at-rest > ladder > worker > current admission. Route diagnostic negatives choose latest checked_at (tie same source order). Invalid or future-dated data is not usable evidence. Positives from any other store cannot override even an older active negative. Only deliberate exact recovery can replace a same-route negative; provider-scope blocks persist until expiry or explicit recovery of that scope, never sibling success.

**Rationale / alternatives:** Seven statuses retain opaque inventory and explicit exclusions separately. Making all negative health unavailable would conceal diagnostic failures. STALE retains history past usable expiry, unlike cache lookup's unprobed; `freshness=expired` makes that distinction exact. Current gates beat cached historical success because operators must not read VERIFIED as currently usable while cooled down.

### 3. Stable JSON, filters and paging

**INFERRED:** `to_dict()` emits `schema="verdict.verified-models/v1"` with all keys, UTC ISO8601 `Z` timestamps or null, no secrets. Unknown is null, never zero/true. Envelope: generated_at, rows, counts_by_status (all seven over the full deduplicated inventory), filtered_counts_by_status, total_count, filtered_count, filters {status,provider,search}, page (one-based), page_size, page_count, source_errors. Paging size default50/max200 like routing_render; reject invalid page/size/filter before I/O; clamp over-cap size to200 and out-of-range page to last valid page. Empty result has page1/page_count0. Counts reconcile before paging; filtered_counts reconcile to filtered_count.

Every row has:

| Field | Type / meaning |
|---|---|
| route_id, provider, status | exact canonical route string; resolved provider string; status enum |
| coding_ok | bool, fresh unobstructed full/tool proof only; false for STALE/FAILED/UNAVAILABLE/UNVERIFIED |
| last_success_at | latest validated at-rest success timestamp if retained; null if overwritten/no valid history; never a hint's invented success |
| checked_at, evidence_source | winning observation time (nullable); one of health_cache, ladder_state, worker_health, admission, connections, inventory, policy, harness_visibility; never token/path with secret |
| fresh_until, expires_at, freshness | positive checked+600s / checked+1800s (not healthy entry until); negative usable deadline; freshness fresh/stale/expired/negative/none |
| capabilities | {context_window:int|null, tools:bool|null, structured:bool|null}; catalog facts, not proof |
| restriction, reason, restrictions | nullable primary restriction, sanitized stable reason, all applicable restriction codes |
| cooldown_until, cooldown_scope | nullable deadline and scoped key; retain known provider/pool/account binding without exposing credentials |
| failure_category, http_status, latency_ms | nullable canonical/cache category, int, float; negative metadata never omitted |
| identity, probe_class, agentic_ok, agentic_checked_at | explicit independent proof metadata, nullable when absent |
| capacity_class, refreshable, refresh_reason, hints | capacity free/subscription/metered/unknown; eligibility for bounded refresh, explanation, timestamped lower-trust hints |

`refresh_reason=requires_confirmation` for metered/unknown, not a replacement of a valid VERIFIED/STALE/FAILED status. Never discard valid negative or old success merely because spend is guarded. Never-probed metered/unknown routes remain UNVERIFIED with that note.

Sort: VERIFIED, STALE, FAILED, UNAVAILABLE, UNVERIFIED, INVENTORY_ONLY, EXCLUDED; within VERIFIED newest last_success_at first, lower known latency next (null last), route_id tie-break. Others use newest valid checked_at then route_id. Status enum filter, provider exact alias-resolved match, case-insensitive substring search over route_id/provider/reason; conjunctive filters, no regex or scope defaults. Projection cost O(n log n), indexed provider/evidence lookups, one bounded page rendered. Offline7,000-row fixture target <2s on supported CI hardware; render never creates a7,000-row Rich table.

### 4. Actions and compatibility

**INFERRED:** Register `models.verified` (read) in the shared action registry. `verdict/actions/verified_models.py` reads snapshots, optional inventory/connection GETs with normalized gateway origin, and invokes the pure projection. No model calls or evidence/receipt/visibility writes. `local_only=True` uses cached inventory/connection snapshot for autocomplete; never refreshes. Stored gateway reads remain inventory, never proof.

CLI `verdict eligibility --verified [--status STALE --provider cc --search sonnet --page 2 --page-size 50] [--json]` is least surprising because this is the existing health command. Existing `verdict eligibility --json` without verified remains unchanged (records, stage counts, selected, filters); only additive keys are permitted there. Reject --verified with --probe/scope/task-only selector flags rather than silently inheriting scope. No change to existing CLI default.

TUI `/eligibility` uses the new consumer controller and renderer, not generic first50 truncation. The controller executes `plan -> refresh (wait/progress) -> reload evidence -> models.verified -> render`. CLI verified mode uses the SAME sequence. JSON stdout contains one final verified envelope plus additive `refresh` summary; progress is stderr and is never JSON-line pollution. `models.verified` called alone stays pure read/snapshot semantics and does not trigger refresh; triggering belongs to the consumer controller. This distinction is necessary for autocomplete and zero-probe projection tests.

### 5. Triggered refresh: WAIT, do not render provisional statuses

**INFERRED:** New `verdict/orchestration/verified_refresh.py` coordinator exposes:

```python
def refresh_for_consumer(snapshot, *, consumer, needed_ids, query, now,
                         config, transport=None, on_progress=None,
                         cancel=None) -> RefreshOutcome: ...
```

A cancel-aware UI/CLI worker executes probes; the main event loop stays responsive and displays progress ONLY until the bounded job ends. Final row rendering/use is after refresh completion and snapshot reload. No provisional model list. For selection, refresh completes before evaluation/use and existing exact ladder confirmation still happens before dispatch. Picker preview/apply pass exactly their involved ids. Autocomplete never calls this API. If needed entries are already fresh, return reused_fresh immediately: zero calls, zero wait, no job.

Default limits:20 routes,40 HTTP requests (two per full probe),120s wall, concurrency4; hard maxima50 routes/100 requests/120s/concurrency4. Settings `VERDICT_AUTO_REFRESH` (default1;0 disables), `VERDICT_REFRESH_MAX_ROUTES`, `VERDICT_REFRESH_MAX_REQUESTS`, `VERDICT_REFRESH_WALL_SECONDS`, `VERDICT_REFRESH_CONCURRENCY` may lower/raise defaults only within hard maxima; invalid config fails before calls. Disabled auto-refresh returns the snapshot labelled `auto_refresh_disabled`, not an implicit manual consent.

Automatic candidates are addressable/admitted FREE or SUBSCRIPTION, currently STALE/UNVERIFIED or eligible half-open, without active entitlement/cooldown/identity/policy gates. Metered/unknown NEVER auto-probe; fresh at-rest entries NEVER re-probe. For verified views, needed ids are current filtered page (default50), not6,811 rows. Priority: explicit consumer ids (picker/selection) > previously validated success now stale > half-open negatives > never-verified admitted routes in canonical ladder rank order. Within each priority round-robin by provider, stable route tie-break, unknown rank last. Ranking is read-only; no selector.select. Only supplied ids/page can enter the capped plan. Inventory/capacity alone never makes a route eligible. Initial snapshot/inventory reads are outside model-request estimate but use the same overall consumer wait deadline; do not use unbounded routes_loader GETs after the job starts.

Single-flight: one nonblocking cross-process job lock beside the configured health-cache, held for the entire job, plus atomic progress/in-progress marker with job_id/digest, owner, ids, start/deadline, per-route outcomes, and monotonic sequence. Paths stay under the cache directory; no credentials. Second triggers JOIN and WAIT on the in-flight job; they do not queue a second sweep or append unbounded ids. Uncovered ids stay unrefreshed with `joined_job_not_covered`. A matching page/id trigger completed within10s is debounced (no calls) and returns prior result with a debounce note; picker/selection exact fresh checks still occur. Fresh-only bypass is checked before lock/join. Stale crashed markers with no live lock are discarded; unfinished routes are NOT_TESTED, not healthy.

The single owner coordinates max4 workers with atomic whole-probe2-request reservations, per-provider token reservations, deadline and cancellation checks BEFORE EACH chat/tool dispatch, and serialized result/cooldown/cache writes. Release unused request reservations after chat failure; actual request count never exceeds cap. Call timeout=min(15s, remaining deadline); only timeout-respecting live/injected transports are supported. No automatic retries or auto-agentic calls. Provider auth/payment/permission/rate-limit scope stops future dispatch for that scope for the cycle; already-in-flight calls may finish, never clear its blocker. 429 honors Retry-After and bucket zeroing; route-specific403/per-model quota stops that route only.

Progress: probed n/N, verified/failed/unavailable counts so far, actual/reserved request counts, elapsed, per-provider counts, last sanitized reason, job id. Bounded coordinator events feed the UI thread; no worker mutates UI/cache directly. Esc/Ctrl-C requests shared-job cancellation (including joined consumers), stops further dispatch, waits only for current calls within remaining deadline, then renders last-known evidence with `cancelled/last_known` banner. Completed trustworthy results remain persisted. All waiters see same cancellation. Cross-process event waiting is bounded/deadline-aware, not busy polling.

At route/request/wall/bucket cap, render final current evidence; unrefreshed rows stay STALE/UNVERIFIED (or their still-applicable negative/availability status) with exact `route_cap/request_cap/wall_cap/bucket/joined_job_not_covered` refresh reason. A partial chat before missing tool budget is NOT_TESTED full proof, leaves prior proof unchanged, never records a negative or renews coding health. Fresh rows not selected retain fresh proof. Refresh completion includes partial/cancelled/busy/error; it is not a guarantee every candidate was tested.

**Rationale / alternatives:** The operator chose bounded wait over provisional status rendering. Awaiting the entire6.8k inventory is rejected. Daemon run_once/liveness policy, global cursor and mutable multi-writer snapshots cannot implement this safe full-probe job. Use an isolated explicit Prober API, preserve daemon defaults, and route every Prober writer through the same job/writer gate.

### 6. Manual plan/execute and /probe consent

**INFERRED:** `verdict/actions/model_refresh.py` exports registry actions `models.refresh.plan` (read) and `models.refresh.execute` (mutation). Plan reads supplied/local inventory/evidence only; no provider GETs, transport construction, bucket consumption, locks, markers or writes. Default manual candidates: current shown filtered page STALE/UNVERIFIED/half-open; explicit wider coverage requires showing that bounded candidate list. No fresh bypass unless an explicitly disclosed manual force flag is confirmed. Same hard limits apply.

Plan fields: schema=`verdict.model-refresh-plan/v1`, consumer, gateway_origin, evidence_generation, created_at, valid_until (120s), filters/page, needed_ids, candidate_count (before cap), routes (exact chosen list/provider/capacity/probe kind), selected_count, omitted_count, caps, estimated_requests=2*selected_count, spend_note (`SUBSCRIPTION and METERED probes may consume quota; FREE can also have provider limits; UNKNOWN may incur charges; currency estimate unavailable`), requires_confirmation, plan_digest. Digest SHA256 of canonical JSON including endpoint, exact ids, filters, cap/kind/capacity, generation, dates; never includes a token. It is accidental-change binding, not authentication.

Execute requires `confirmed=True`, the exact plan and matching digest, unexpired plan, and unchanged gateway/selected identities/capacity/gates. Missing/False/invalid/expired/changed plan returns refusal with zero calls/writes. Re-read gates before dispatch; narrowing due new blockers can skip candidates, never expand or increase spend without replan. No live transport is created before confirmation. Auto-plan is internal separately typed prepaid authorization, not `confirmed=True` fabricated by the TUI; it proves trigger kind, capacity guard and caps. All execute paths use the same single-flight bounded API and persistence.

TUI `/eligibility stale`, `/eligibility provider=cc`, `/eligibility search=sonnet`, `/eligibility page=2` parse independent arguments; no scope prompt. `/eligibility refresh` shows plan + count/cap/estimated requests/warning, asks `[y/N]`; blank/n/EOF/Esc/Ctrl-C cancels without transport calls. On y show live progress then final counts and per-route failure/skip reasons. `/probe cc/x,kr/y` accepts comma and whitespace separated exact model ids as list[str], deduplicates in order, rejects empty lists, preserves ids containing `/` or `:`. Inline, prompted and palette paths use the same parser/consent controller. Show selected list/count and quota warning, ask `[y/N]`, then explicitly set the existing allow_live_probe gate; do not ask the operator for an unreachable flag. CLI's existing --allow-live-probe remains intact. /probe remains its existing bounded liveness action, not automatic coding proof.

### 7. Canonical classification, persistence, later admission

**INFERRED:** Reuse `FailureIntelligence` for canonical scope/action, `prove_at_rest.category_for`/`health_cache.CATEGORY_*` for stored category, and `health_cache.negative_seconds` for deadlines. This adapter is explicit because existing classifiers differ. Structured HTTP status beats loose text; actual identity mismatch overrides nominal success. Preserve sanitized error detail for model-scoped403 and per-model quota. Add structured410 route handling where canonical classifier currently falls through; preserve request/infra-only context overflow/gateway-busy semantics (no health poisoning). Cache TTL policy is NOT the same as canonical runtime cooldown default; do not silently replace one with the other.

| Result | Stored cache category | Projection while active | Cooldown scope / deadline policy |
|---|---|---|---|
| timeout / missing response | CATEGORY_TIMEOUT (`timeout`) | FAILED absent independent cooldown | route retry gate;60s doubling to900s |
| 401 | CATEGORY_AUTH (`authentication`) | UNAVAILABLE | provider;6h doubling to24h |
| 402 | CATEGORY_PAYMENT (`payment_required`) | UNAVAILABLE | provider;6h doubling to24h |
| 403 | CATEGORY_PERMISSION (`permission`) | UNAVAILABLE | provider, explicit model denial route;6h..24h |
| 404 | CATEGORY_NOT_FOUND (`not_found`) | FAILED | route retry gate;6h..24h; runtime category model_unavailable |
| 410 | CATEGORY_GONE (`gone`) | FAILED | route retry gate;6h..24h (explicit extension, not existing unknown fallback) |
| 429 | CATEGORY_RATE_LIMITED (`rate_limited`), canonical quota detail retained | UNAVAILABLE | provider or explicit per-model quota route; positive Retry-After else60s; affected provider/pool bucket zeroed |
| 5xx | CATEGORY_UPSTREAM (`upstream`) | FAILED | route retry gate;60s doubling to900s; runtime upstream_temporary |
| model identity mismatch | CATEGORY_MODEL_MISMATCH (`model_mismatch`) | FAILED | route retry gate;current cache60s..900s (runtime classifier default3600 is separate) |
| catalog ghost | CATEGORY_CATALOG_STALE (`catalog_stale`) | FAILED | route retry gate;6h..24h |
| wrong chat/tool/malformed proof | canonical existing response category; never ok | FAILED | route retry gate;cache transient policy; a bucket/cap/cancel skip is NOT this failure |
| context_length_exceeded / gateway_busy | no route health write | previous status + refresh skip reason | request/infra scope none; no provider/route poison |

Only health-cache.json stores probe evidence; no refresh writes to ladder/worker cache or admission receipt. Add OPTIONAL schema1 envelope `cooldowns` for scoped availability blockers (key->category,canonical_category,checked_at,until,scope bindings) and optional per-route `last_success_at` plus failure scope/probe outcome metadata; old readers ignore additions, new readers accept absent keys. Diagnostic route retry gates remain route negative entries, not availability cooldowns. Serialize under single writer; on provider failure persist its scope even if route proof already failed, so sibling rows cannot remain VERIFIED. A sibling success does not delete provider cooldown. Replace only same-route resolved negatives after deliberate half-open recovery. At-rest route entry checked_at remains last attempt; last_success_at preserves earlier valid success on failure where available, but never invent legacy history. Existing entries need no migration; schema1 envelope stays backward compatible.

Add a distinctly named `admission.evidence_from_at_rest_health_cache` negative-only adapter (NOT the worker adapter). `default_runtime_evidence` merges applicable unexpired at-rest negatives/scoped cooldowns and omits ALL positives. Ladder uses those gates before confirm/dispatch; it may schedule refresh but still calls existing exact confirmation, not cache launch. No provider positives, refreshed receipt laundering, or TTL-extension by reread. Agentic_ok uses its own checked time and existing FREE agentic launch gate; two-step coding_ok alone does not satisfy it. No auto agentic test is added.

### 8. Ownership and dependencies (non-overlapping files)

**INFERRED:** Three parallel units; each owns complete files. New interfaces are agreed here; no cross-owner edits. Unit2 wires imports/registrations/hooks from units1/3 once available. Unit3 implements all consent/wait behavior in its new controller, NOT by editing home or registry.

**Unit1 — projection + tests (BOD-291):**
- `verdict/orchestration/verified_models.py` (new)
- `tests/test_verified_models_projection.py` (new)

No dependencies. Own pure schema, precedence, validation, filtering, paging and performance fixtures.

**Unit2 — actions + CLI + TUI rendering/wiring (BOD-291):**
- `verdict/actions/verified_models.py` (new; read-only snapshot adapter)
- `verdict/actions/registry.py` (sole registrations incl unit3 exports)
- `verdict/orchestration/cli.py` (verified flags/controller entry)
- `verdict/orchestration/verified_models_render.py` (new)
- `verdict/home.py` (sole palette/dispatch/render hook owner)
- `tests/test_verified_models_surfaces.py` (new)
- `tests/test_eligibility_gateway_contract.py` (regression if needed)
- `tests/test_orchestration_eligibility_output.py` (legacy compatibility)

Depends on unit1 schema; finalize integration after unit3. Home delegates verified-view waiting and /probe consent/parsing to unit3 controller. Register models.refresh.plan/execute from unit3 action module; do not implement duplicate refresh logic.

**Unit3 — auto/manual refresh + wait/consent + /probe fix (BOD-292):**
- `verdict/orchestration/verified_refresh.py` (new coordinator)
- `verdict/actions/model_refresh.py` (new plan/execute exports)
- `verdict/tui_verified_controls.py` (new injected reader/progress/cancel/argument/model-list controller; consumed by home)
- `verdict/prove_at_rest.py` (bounded full-probe API + same writer gate)
- `verdict/orchestration/health_cache.py` (optional metadata/scoped blockers/serialized reservations)
- `verdict/admission.py` (negative-only at-rest adapter)
- `verdict/orchestration/eligibility.py` (selection-before-dispatch coordinator hook; preserve confirmation)
- `verdict/orchestration/recovery.py` (structured410 extension only; canonical scopes)
- `tests/test_verified_model_refresh.py` (new)
- `tests/test_verified_refresh_consumers.py` (new ordering/cancel/consent)
- `tests/test_prove_at_rest.py`, `tests/test_health_cache.py`, `tests/test_authoritative_eligibility.py`, `tests/test_orch_eligibility.py` (owned regressions)

Depends on unit1 snapshot/row types; can build coordinator/Prober with fixtures before unit2. Unit2 depends on unit3 public controller/actions for final integration. BOD-293 integrator calls refresh_for_consumer with its ids; BOD-295 integrator calls local_only projection only. No unit owns these OpenSpec artifacts during implementation; doc amendments go through design owner.

### 9. Offline validation

**INFERRED:** Inject clock, monotonic deadline, inventory/connections, cancellation, line/key readers, transport, event sink, lock/job store and cache paths. Never call real gateways/models or touch ~/.verdict in tests. Cover all7 statuses,600s/1800s/negative exact boundaries, false/null capabilities, chat-only, tools, identity mismatch/missing, future/malformed/schema/key errors, positive conflicts, provider/pool/model scope, receipt narrowing and out-of-inventory ids. Assert counts/page stability and7k rows<2s. CLI and TUI final JSON are identical except presentation and separately disclosed refresh metadata; legacy JSON fields/goldens remain unchanged.

Refresh tests prove pure action/plan zero I/O/probes/writes; cached fresh zero calls AND no wait; each view trigger bounded prepaid stale/unverified calls; no auto metered/unknown calls; round-robin priority; four-worker atomic budgets; provider stop and in-flight completion handling; cross-process single-flight join/debounce; shared cancel stops further dispatch; render strictly AFTER completion (progress before completion only); no stale answer treated as fresh at cap; deadline-respecting transport and no post-cancel obsolete writes. Test manual missing/mismatched/expired/changed digest, defaultNo/EOF cancellation zero calls, confirmed<=2*selected routes and<=caps, every table failure, partial proof NOT_TESTED, cache persistence/reload, negative-only admission and runtime reconfirm. /probe inline/prompt/palette parse identical lists and require explicit y/N. Picker/selection contract fixtures use involved ids; autocomplete proves no wait/refresh even with stale local rows.

## Risks / Trade-offs

- [Prepaid quota can still be consumed] -> disclose progress/quota note, bounded caps/buckets, auto disable switch, never auto metered/unknown.
- [Waiting can cost up to120s] -> live progress/cancel, fresh fast path, bounded I/O/deadline, final incomplete labels. Do not hide this with provisional rows.
- [Strict identity leaves some HTTP-success providers unverified] -> explicit identity_not_verified; do not invent exact model proof.
- [Cross-process cache overwrites/duplicate daemon probes] -> all Prober writers share single-flight job lock, serialized reload/merge/write and reservations; conflict tests.
- [A provider block arrives while siblings run] -> stop new provider calls; persist scope first, sibling results cannot erase it.
- [Schema1 cache is writable local state] -> display only; negative fail-closed adapter; exact launch confirmation remains separate.
- [Stale receipt/unknown inventory] -> read-only current unscoped admission; source errors and UNKNOWN capability facts, no silent success fallback.

## Security / trust implications

**INFERRED:** Sanitize provider errors, secret-like tokens/URLs/control characters and plan labels before display. Never emit credential, authorization header, account email, response body or secret path. Digest/job marker is not authorization for arbitrary API execution. Manual transport only after consent; auto authorization only prepaid validated bounded candidates. Preserve explicit scoped-denial bindings without public account secrets. Cached proof never broadens admission.

## Routing / context / memory implications

**INFERRED:** No routing policy/rank changes except bounded refresh before need/dispatch and negative-cache consultation. Context window and tools/structured are declared capabilities, not health or task suitability. FREE agentic requirement retains separate expiry. No agent/context memory is stored by the view or progress channel; progress is presentation, not orchestration event authority.

## Migration Plan

1. Land unit1 contract/tests, then units2/3 with agreed hooks; integrate without overlapping edits.
2. Keep cache schema1; optional fields default absent. Do not migrate/overwrite ladder/worker positives into health cache. Validate legacy evidence conservatively.
3. Enable prepaid auto-refresh by default at the named consumer triggers with cap/debounce controls. Existing plain CLI eligibility stays unchanged. Picker/autocomplete integration uses documented API boundary.
4. Rollback/disable: VERDICT_AUTO_REFRESH=0 immediately stops new automatic jobs; active jobs respect cancellation. Revert consumer/refresh hooks to snapshot view without converting stale into healthy. Optional cache fields can remain for old readers. No launch-authority migration to reverse.

## ADR impact

**INFERRED:** No existing ADR is replaced. This design records the read-only projection boundary, wait-before-use operator decision, and cache-versus-launch trust split. No unresolved contract decision is deferred to an implementer.
