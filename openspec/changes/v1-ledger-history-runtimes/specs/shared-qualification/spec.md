# Spec Delta

## Purpose

Adds the maintenance lane: both automatic probe producers (census, prove-at-rest chat phase) run through the shared `RefreshCoordinator` with scope-aware per-caller budgets, no partial-probe spend, and a 401/403 confirmation gate before any write.

## ADDED Requirements

### Requirement: Automatic maintenance producers share the coordinator with their own bounded config

Census SHALL probe through `RefreshCoordinator.refresh_for_consumer(consumer=CONSUMER_CENSUS)` at its existing maintenance size (4 routes / 16 requests, max 2 per provider), enforced by the coordinator regardless of census's own requested ask. Prove-at-rest's chat phase SHALL probe through the same coordinator with `consumer=CONSUMER_PROVE_AT_REST` and its own bounded config derived from the daemon's own flags: `max_routes = max(1, min(daemon_max_requests // 2, HARD_MAX_ROUTES=50))`, `max_requests = min(daemon_max_requests, HARD_MAX_REQUESTS=100)`, `wall_seconds = min(daemon_max_wall_seconds, HARD_MAX_WALL_SECONDS_MAINTENANCE=600)`, `max_per_provider=2`, `concurrency=4`, `timeout=30`. Neither producer SHALL exceed the coordinator's hard maxima.

#### Scenario: Census maintenance bound
- Proof: `tests/test_census_passports.py::test_census_probe_loop_is_owned_by_the_shared_coordinator`

#### Scenario: Prove-at-rest own bounded config
- Proof: a test asserting daemon flags 300/600 map to coordinator config 50 routes / 100 requests / 600 s.

### Requirement: Automatic maintenance cycles probe FREE and SUBSCRIPTION capacity only

An automatic coordinator cycle (census or prove-at-rest chat phase) SHALL probe only `PREPAID_CAPACITY_CLASSES` (FREE, SUBSCRIPTION). METERED and UNKNOWN routes SHALL remain reachable only through the consented manual plan (`authorized=True`); the daemon SHALL NEVER pass `authorized=True`. SUBSCRIPTION SHALL get a full chat-plus-tool probe (2 requests), not liveness-only.

#### Scenario: Scope restriction
- Proof: `test_prove_at_rest_agentic_budget.py::test_prove_at_rest_cycle_holds_the_shared_lease_and_obeys_maintenance_caps`

#### Scenario: Subscription full probe
- Proof: `tests/test_health_cache.py::test_subscription_gets_the_full_chat_and_tool_probe`

### Requirement: The coordinator reserves a whole probe before any dispatch

The coordinator SHALL reserve a probe's full request cost before dispatching any part of it. A remaining budget too small for one full probe SHALL make zero calls for that probe; a probe already in flight whose wall deadline is crossed mid-dispatch SHALL be a no-write (`REASON_DEADLINE`), leaving prior proof untouched.

#### Scenario: No partial spend
- Proof: `tests/test_auth_outage.py::test_gateway_confirmation_respects_request_cap` (requests == 0, not 1, on a budget below one probe)

#### Scenario: Deadline crossing is a no-write
- Proof: `test_save_after_the_wall_cap_is_not_a_lock_timeout` (entry stays `None` after a deadline-crossing probe; `stopped_reason` names the real wall-cap reason)

### Requirement: An auth_gate hook confirms a 401/403 before any write, and reports the real stop reason when it cannot confirm

`RefreshCoordinator.auth_gate(route_id, http_status, remaining_requests)` SHALL be called before any write on a 401/403 outcome and SHALL return `(True, spent)` for a confirmed outage (write nothing, stop the job with `REASON_AUTH_OUTAGE`), `(False, spent)` for a genuine per-route failure (write the real negative, stop that provider), or `(None, 0)` when the gate cannot afford its own confirmation call against `remaining_requests` (the coordinator's own `config.max_requests - requests_made` computed after the triggering probe's spend) — in which case nothing is written and the job stops with the real `wall_cap`/`request_cap` reason, never a claimed outage. The gate's own confirmation call, when made, SHALL be counted in the coordinator's `requests_made`.

#### Scenario: Confirmed outage
- Proof: `tests/test_verified_model_refresh.py::test_auth_gate_confirmed_outage_writes_nothing_and_stops_the_job`

#### Scenario: Denied outage writes the real failure
- Proof: `test_auth_gate_denied_outage_records_the_real_auth_failure`, `test_auth_gate_fires_on_403_permission_category_too`

#### Scenario: Unconfirmed reports the real budget cap
- Proof: `tests/test_prove_at_rest_agentic_budget.py::test_unconfirmed_auth_gate_reports_budget_stop_not_outage`

#### Scenario: The gate's own check is counted against the coordinator's budget
- Proof: `tests/test_prove_at_rest_agentic_budget.py::test_gateway_check_counts_against_the_coordinator_request_budget`, `test_gate_denies_its_own_check_when_the_triggering_probe_used_the_whole_cap`

#### Scenario: A denied outage excludes the route from the same cycle's agentic phase
- Proof: `tests/test_prove_at_rest_agentic_budget.py::test_auth_gate_denies_outage_excludes_route_from_same_cycle_agentic`

### Requirement: Provider/route stop never demotes a complete job to a capacity-exhaustion reason

When every candidate in a job was either probed or skipped by a provider/route stop, the job's own result SHALL be `complete`, not `bucket`/`capped`. Only a genuine bucket denial SHALL set `REASON_BUCKET`.

#### Scenario: Provider stop labeling
- Proof: `tests/test_verified_model_refresh.py` red test — a 401 on one route plus a skipped sibling route, remainder succeeding -> job `complete`, skipped outcome `provider_scope_stopped`.

### Requirement: The offline majority-pool auth-outage heuristic is removed from the chat phase

The chat-phase heuristic fallback for detecting a gateway auth outage without a live `gateway_auth_check` (`auth_outage_consecutive`, `auth_outage_distinct_pools`, `_auth_fail_pools`, buffer-then-flush) SHALL be deleted. Without a `gateway_auth_check`, a 401/403 SHALL write the real per-route negative and stop that provider; no outage SHALL be claimed. The agentic phase's own, separate heuristic and buffer SHALL remain unchanged (U6, out of scope for this change).

#### Scenario: No gateway check still writes real negatives
- Proof: `tests/test_credential_pools.py::test_no_gateway_check_writes_per_route_negatives_and_stops_each_provider`

### Requirement: A durable per-caller cursor survives across cycles

Each automatic producer SHALL keep a durable cursor (`cache.cursor`: `cycle_open`, `probed_ids`) updated from the coordinator's own outcomes (probed or provider-stopped), so a fleet larger than one cycle's route cap still rotates across cycles.

#### Scenario: Cursor resume
- Proof: `tests/test_health_cache.py::test_cursor_resumes_at_first_unprobed_route_after_cap`, `test_cursor_resume_survives_route_reorder_and_removal`
