# BOD-297 qualification producer audit

Audited 2026-11-11 against `main` at `a12e5dda`, in worktree `wt/t_98fef7d0`.
Linear: [BOD-297](https://linear.app/bodanglin/issue/BOD-297). This is an
audit and a set of red-first tests. It changes no production code.

The rule BOD-297 sets: every qualification producer (manual, identify,
confirm, maintenance) goes through the one existing coordinator, which owns
scope, consent, budgets, leases, crash accounting and receipts. No new
source, index or coordinator subsystem.

Red tests: [`tests/test_bod297_qualification_producers.py`](../../tests/test_bod297_qualification_producers.py).
Each gap is a `strict` xfail that must fail on an `AssertionError`, so a fix
makes it XPASS, which fails the run until the marker is removed. Two guard
tests pass today and must keep passing.

## 1. The coordinator that exists

`verdict/orchestration/verified_refresh.py` `RefreshCoordinator.refresh_for_consumer`
is the coordinator. What it has today:

| Mechanism | Where | State |
|---|---|---|
| One owner per job | `_JobLock` (`fcntl.flock`, nonblocking) beside the health cache | Present. A second caller joins or gets `lock_timeout`. |
| Join, debounce, shared cancel | `_Marker` (`verified-refresh.json`), `_join_and_wait`, `_debounce_match` | Present. Scope-bound: another scope never reuses outcomes. |
| Stable scope separate from evidence revision | `RefreshSnapshot.scope` / `effective_scope` | Present. |
| Caps before screening | `select_candidates` (route cap, `max_per_provider`), request cap checked before each admit | Present. `HARD_MAX_*` and `maintenance_from_env()` (4 routes, 16 requests, 2 per provider, 600 s, concurrency 4). |
| Durable bucket reservation | `HealthCache.reserve_bucket` / `release_bucket` under `merge_and_save` | Present, per provider/pool token bucket. Unused spend is refunded; a raised probe keeps the whole charge. |
| Lanes | at most one probe per pool/provider lane; unknown pool runs alone | Present. |
| Gateway-key outage breaker | `auth_gate` (BOD-338) | Present. |
| Fenced RESERVED / MAYBE_SENT / SETTLED attempts | none for qualification | **Missing.** `PERMIT_STATE_MAYBE_SENT` exists only for dispatch permits (`HealthCache.commit_dispatch`, BOD-433). A crash between send and write leaves a bucket charge and no attempt record. |
| Evidence epochs | `HealthCache.record_revocation` per dispatch scope | Present for dispatch permits only. The coordinator neither reads nor raises them. |
| Durable fairness cursor | `HealthCache.cursor` | Used only by `Prober._run_chat_phase`. The coordinator has none, so outcomes that write nothing (`gateway_busy`, `context_length`, cancelled) send the same rank-ordered head every job. |
| Qualification receipt | none | **Missing.** The only record is the marker plus cache entries. |

Existing tests kept as they are:
`tests/test_verified_model_refresh.py::test_cross_producer_qualification_budget_scope_crash_and_fairness`,
`tests/test_census_passports.py::test_census_probe_loop_is_owned_by_the_shared_coordinator`,
`tests/test_prove_at_rest_agentic_budget.py::test_prove_at_rest_cycle_holds_the_shared_lease_and_obeys_maintenance_caps`,
`tests/test_health_cache.py::test_crash_mid_cycle_keeps_partial_results`.

## 2. Producers

| # | Producer (entry point) | Current path | Uses coordinator? | Gap | Required change | Red test |
|---|---|---|---|---|---|---|
| 1 | Manual refresh: `verdict models refresh`, actions `models.refresh.plan/execute` (`verdict/actions/model_refresh.py`) | `refresh_for_consumer(authorized=True)` with a digest-bound plan | Yes | Shares every coordinator gap below (no attempt states, no cursor, alias rows probed separately, identity not required). | None of its own. | via coordinator tests |
| 2 | Verified view / picker / TUI (`verdict/actions/verified_models.py`, `routing_view`) | `refresh_for_consumer` for page ids | Yes | Same as 1. | None of its own. | via coordinator tests |
| 3 | Selection refresh hook (`EligibilityLadder(refresh_hook=...)`, `orchestration/selection.py`) | `refresh_for_consumer` before ranking | Yes | Same as 1. | None of its own. | via coordinator tests |
| 4 | Selection confirmation probe (`EligibilityLadder.select`, used by `verdict choose`, `verdict serve` `/v1/route`, `verdict orchestrate`, `verdict eligibility`) | `self._probe(route_id)` = `openai_health_probe` | **No** | Up to `max_probes_per_select=8` per select, its own budget, outside the flock and bucket; sends during another producer's job. | Run confirmation as a coordinator job (exact ids, explicit) or reserve through it. | `test_selection_confirm_probe_joins_the_shared_coordinator_lease` |
| 5 | Census (`verdict census`, `verdict-census.timer` 240 s; `census_passports.run_census`) | Coordinator with `maintenance_from_env()`; census pre-caps per family | Yes for chat+tool | `run_census` multiplies `max_per_provider` by family count for its own budget. The pipeline needle pass (`run_context_probes`) runs outside the coordinator. There is no consent flag: the timer sends live requests. | Drop the local per-family budget and route needle probes through the coordinator. Require explicit consent for live probing. | `test_census_command_requires_explicit_live_probe_consent` |
| 6 | Prove-at-rest chat phase (`verdict prove-at-rest daemon/once`, `prove_at_rest_service` timer 300 s) | `_run_chat_phase` builds rows, then calls the coordinator | Yes | `order_cycle` adds an epsilon slice that re-probes a route with an active negative every cycle, so a 404 route is hit every 5 minutes. | Never order a route whose negative is still active (BOD-350 owns the replacement). | `test_maintenance_never_reprobes_an_active_negative` |
| 7 | Prove-at-rest agentic phase (`Prober._run_agentic_phase`) | Own thread pool, `agentic_max_routes` budget | **No** | A second request budget on top of `max_requests` (16 requested → 40 sent). Bucket tokens are taken with in-memory `cache.consume`, not a durable reservation, so another process sees them as free. | Charge agentic turns to the same coordinator budget and reserve durably before dispatch. | `test_prove_at_rest_agentic_phase_shares_the_request_budget`, `test_agentic_phase_persists_its_bucket_reservation_before_dispatch` |
| 8 | Prove-at-rest gateway screen (`screen_transport`, `POST /api/models/test-all`) | `Prober` screen phase with `cache.consume` | **No** | Same in-memory bucket use. Out of the identify path. | Same as 7. | covered by 7 |
| 9 | Identify screen (`verdict identify --screen --allow-live-probe`, `identify.screen_models`) | `Prober.screen_route` per route, its own `max_routes`/`max_per_provider` | **No** | No flock, so it sends during another producer's job. It skips scoped provider cooldowns. Admission is checked once per three-turn conversation, not before each request. The candidate set is traffic-proven routes only, so reachable configured supply with no traffic is never considered. It orders by capacity class first, so a free weak route screens before a stronger one. | Run as a coordinator consumer; admit (cooldown, bucket, request cap) before each turn; candidates from all reachable configured supply; strongest task rating first, unknown last. | `test_identify_screen_joins_the_shared_coordinator_lease`, `test_identify_screen_honours_scoped_provider_cooldown`, `test_identify_screen_admits_before_each_request`, `test_identify_screen_considers_configured_supply_without_traffic`, `test_identify_screen_orders_strongest_task_rating_first` |
| 10 | Live admit confirm (`gate_admit_prove_confirm(live=True, consented=True)`, via `intelligence._resolve_confirm_transport` and autodev) | `ProbeRunner` per candidate with its own `ProbeBudget` | **No** | No flock and no shared bucket: it sends while the provider bucket is empty. | Reserve through the coordinator (lease plus bucket) before each request. | `test_live_confirm_joins_the_shared_coordinator_lease`, `test_live_confirm_checks_the_shared_pool_bucket` |
| 11 | Passport qualification API (`POST /v1/models/qualify`, `model_passports.run_qualification`) | `ProbeRunner`, gated by `LLMGATE_ALLOW_LIVE_PROBES` and `LLMGATE_PROBE_CONSENTED` | **No** | `tool_support=True` comes from catalog capability with no tool call observed. | Route through the coordinator; mint `tool_support` only from an observed tool receipt. | `test_passport_tool_support_requires_an_observed_tool_call` |
| 12 | Availability probe router (`verdict/availability.py` `ProbeRouter`, `api.py`/`cli.py`/`subagent_models.py`) | `ProbeRunner.run`, off by default | **No** | Separate budget when enabled. | Route through the coordinator or delete it (BOD-350/BOD-259). | none (disabled by default) |
| 13 | Acceptance tool check (`verdict/acceptance_toolcheck.py`) | Direct `POST` per route inside the acceptance job | **No** | Acceptance evidence, not qualification; writes no health evidence. | Out of scope; listed so no one mistakes it for a producer. | none |
| 14 | Passive identify A3.1 (`verdict identify --passive`) | Seven `GET`s, no writes | n/a | None found: GET-only, 0 model calls, HOME unchanged. | Keep it that way (BOD-345). | guard `test_passive_identify_is_get_only_with_no_model_calls_or_writes` |
| 15 | Passive health refresh (`identify --passive --refresh-health-cache`, `identify_service` timer) | `refresh_health_from_traffic` writes chat-only liveness | n/a (no requests) | Writes by design, only with the flag; never mints tool proof. | None. | none |

## 3. Cross-cutting gaps

| Gap | Where | Red test |
|---|---|---|
| Sparse quota unknown is read as exhaustion. A successful `/api/v1/me/status` row with `available:false` and reason `not_available`, `fetch_failed` or `connection_lookup_failed` excludes the provider as `quota_exhausted`. Only `not_supported` is spared. In OmniRoute these mean "no quota data", not "out of quota". | `free_tier_admit._quota_blocks` | `test_sparse_account_quota_row_is_unknown_not_exhausted` |
| Whole-source failure fails open. A malformed `me/status` payload records only a `shape_unknown` diagnostic and admission proceeds as if quota were known. | `free_tier_admit.snapshot_from_payloads`, `admit_free_tier_active` | `test_whole_account_status_source_failure_fails_closed` |
| Real exhaustion still blocks (guard). | `_quota_blocks` | `test_reported_quota_exhaustion_still_blocks` |
| Alias families are probed separately. `agy/x` and `antigravity/x` are the same backend model (`credential_pools.backend_model`) but the coordinator probes both and records two proofs. `order_cycle` collapses them; the coordinator does not. | `verified_refresh.select_candidates` | `test_coordinator_probes_one_alias_family_member` |
| Effort mapping mints a stronger capability. `identify_pools._proof_routes` credits a `no-think/` proof to the thinking default route because `backend_model` strips the effort prefix. | `identify_pools._proof_routes` | `test_effort_variant_proof_does_not_qualify_a_different_effort` |
| Tool strength without a served identity. A probe with no served-model echo (`identity="not_reported"`) is written `tool_ok=True` and counted verified. The agentic path already requires the echo. | `probe_full`, `_persist_and_classify`, `HealthCache.record` | `test_coordinator_requires_served_model_identity_for_tool_strength` |
| Late completion overwrites a newer negative. `_persist_and_classify` writes with `checked_at=self.clock()` at completion time, so a success that started before a newer negative lands after it and wins, and its observed time is lost. `screen_route` already guards this. | `verified_refresh._persist_and_classify` | `test_late_success_keeps_newer_negative_and_observed_time` |
| Fresh repeats re-probe. The coordinator trusts the caller's row status and never re-reads the shared cache, so a route another producer proved after the snapshot is probed again. | `verified_refresh.select_candidates` / `_run_owned_job` | `test_fresh_repeated_run_rechecks_shared_cache_and_sends_nothing` |
| No durable attempt states. | coordinator | `test_coordinator_records_durable_attempt_states` |
| No durable cursor. | coordinator | `test_coordinator_cursor_screens_backlog_within_n_cycles` |

## 4. Follow-up cards (children of this task)

- `t_1ba576e6` — route producers 4, 5, 7, 9, 10 and 11 through the coordinator; sparse quota; supply membership; attempt states; cursor.
- `t_06c98638` — alias/effort, strict tool receipt, late completion, fresh-repeat reuse, passive guard.
- `t_bbe75ee3` — consent, per-request admission, ordering, epsilon re-probe.
- `t_0de47e14` — the BOD-297 story card.

Numeric caps stay where they are today (`maintenance_from_env`, `HARD_MAX_*`,
CLI flags). This audit approves no new cap or cadence.

## 5. Test command

```sh
TMPDIR=/tmp/t98 env -u VERDICT_HOME -u VERDICT_OMNIROUTE_API_KEY -u OMNIROUTE_API_KEY \
  PYTHONPATH="$PWD" uv run --no-sync python -m pytest -q -p no:cacheprovider -o addopts="" -rxX \
  tests/test_bod297_qualification_producers.py
```

Expected today: `2 passed, 24 xfailed`. To see each red test fail on its own
assertion, add `--runxfail`: `24 failed, 2 passed`.
