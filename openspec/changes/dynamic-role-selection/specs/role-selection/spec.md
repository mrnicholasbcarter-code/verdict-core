# Spec Delta

## Purpose

The orchestrator dynamically selects models for different roles (Planner, Implementer, Reviewer, Escalation, Controller) based on a pipeline of eligibility, capacity, quota, and measured strength.

## ADDED Requirements

### Requirement: Selection Pipeline Contract
The selection SHALL follow a four-stage pipeline:
1. **Inventory (S0)**: Start with live `/v1/models` rows (or a fixture labelled `fixture=true`). Output H0.
2. **Admission (S1)**: Apply the existing admission ladder and eligibility floors (`verdict/admission.py`, `eligibility.py`, `orchestration/eligibility.py`). Opaque `auto/*` and `combo/*` are removed here. Unknown runtime is never healthy. Output canonical AdmittedSet, H1. Dynamic selection never calls anything that adds to this set.
3. **Narrowing (S2)**: Remove candidates based on Capacity, Quota, Pool, Family, Measured Demotion, and Operator Override. Each removal carries a skip reason. Output H2 plus skip list `[{route_id, stage, reason}]`.
4. **Ordering (S3)**: Order the remaining candidates by `role_strength` signal through `advise_order`. Low confidence (stale or missing) leaves the legacy order. Output H3 ordered ladder.

The selection SHALL ensure that `H3 set == H2 set`, `H2 subset of H1`, and every route in H3 is present in AdmittedSet. An empty H3 gives `LadderResult(status=FAIL_CLOSED, reason=...)`; never a silent default and never a hang.

#### Scenario: Standard selection succeeds
- **WHEN** a node is dispatched with valid inventory
- **THEN** the pipeline returns H3 as a subset of H1 with routes ordered by role strength

#### Scenario: Empty pipeline fails closed
- **WHEN** no routes survive the narrowing stage
- **THEN** the system returns `FAIL_CLOSED` rather than hanging or defaulting silently

#### Scenario: Hash integrity verified at each boundary
- **WHEN** the pipeline computes H0, H1, H2, H3
- **THEN** each hash is a sha256 over the sorted canonical route IDs leaving that stage; any set mismatch fails with `candidate_set_violation`

### Requirement: Safety Invariants
- **Strength never admits**: A route MUST pass S1 and S2 before its strength can be considered for ordering in S3.
- **Narrowing only removes**: No stage after S1 SHALL add routes back to the candidate set.
- **Fail Closed**: If H3 is empty, the system SHALL return a `FAIL_CLOSED` status.
- **Unknowns**: Unknown runtimes SHALL NOT be healthy.
- **Opaque routes**: `auto/*` and `combo/*` routes SHALL NOT be candidates.
- **Fixtures**: Fixture metadata SHALL NOT satisfy live proof.
- **Economics**: Pay-per-token (`METERED`) capacity SHALL NOT be the default.

#### Scenario: Strength cannot bypass admission
- **WHEN** a route has the highest strength score but fails the admission ladder
- **THEN** it is excluded from H1 and never appears in H3

#### Scenario: Opaque routes are never admitted
- **WHEN** the inventory contains `auto/best-coding` and `combo/planner`
- **THEN** both are removed in S1 and never appear in the AdmittedSet

#### Scenario: Fixture inventory never passes live proof
- **WHEN** a fixture inventory is used
- **THEN** `live_proof=false` and `metadata_fixture=true` are set in the shadow event

### Requirement: Capacity Precedence (P1-P8) and Conflict Rules
Capacity class SHALL be evaluated per route, highest precedence first. Scope is connection unless stated. A lower row can never downgrade the class set by a higher row.

**P1**: Route-scoped billing evidence — current `billing_402_metered` health entry (`entry.until > now`) → METERED, confidence `observed`. Expiry: 24h after the last 402 (configurable); refreshed by every new 402. Never cleared by auth type or pricing.

**P2**: Connection-scoped billing evidence — `>= 1` HTTP 402 on any route of the connection within 24h (call logs) → METERED for every route of the connection, confidence `observed`. Same expiry rule as P1.

**P3**: Explicit positive pricing (any price > 0) → METERED, confidence `declared`. Never FREE.

**P4**: OpenRouter-family route outside the public free set → METERED; inside the free set → FREE, confidence `declared`. Anthropic API key → METERED.

**P5**: `:free` suffix (non-OpenRouter) or connection-level free signal or all-zero explicit pricing → FREE, confidence `declared`.

**P6**: OAuth with no paid evidence → SUBSCRIPTION, confidence `assumed` (`assumed_subscription_oauth`).

**P7**: No evidence at all → FREE, confidence `assumed` (`assumed_free_no_paid_evidence`, D7 unchanged).

**P8**: Connection blocked (auth/verification failure) → class unchanged, but availability UNAVAILABLE with reason. Never healthy, never a candidate until a fresh success.

Missing connection row → UNKNOWN. It fails entitlement.

**Conflicts and Eligibility per Role**:
- Billing vs OAuth: METERED wins. Excluded from every role unless existing metered consent applies. Pay-per-token is never used by default.
- Billing expired vs pricing positive: METERED (P3).
- Pricing zero vs observed 402: METERED (P1/P2 outrank P5).
- OAuth assumed SUBSCRIPTION: eligible for all roles once a fresh agentic passport or success exists. Without one: implementer low-risk only, ranked after observed/declared subscription.
- Assumed FREE (P7): implementer low-risk with fresh passport only. For planner, reviewer, controller, and escalation it is treated as UNKNOWN, ranked last, and never makes a ladder non-empty on its own.

**Freshness**: Evidence older than its TTL drops one confidence level (observed → assumed). Billing evidence expires only through the explicit 402 expiry. Name or vendor literals are not inputs.

**Single Enforcement Point**: `capacity_policy.role_capacity_allowed(role, capacity_class, capacity_confidence)` is the sole function that determines whether a capacity class is permitted for a role.

**D7 Rule (Pending Operator Confirmation)**: For critical roles (planner, reviewer, controller, escalation), an `assumed-free_no_paid_evidence` status SHALL be treated as UNKNOWN, regardless of `VERDICT_ALLOW_UNKNOWN_CAPACITY`. It ranks last and never makes a ladder non-empty on its own.

#### Scenario: METERED wins over SUBSCRIPTION
- **WHEN** a connection has both a 402 (P1) and OAuth subscription evidence (P6)
- **THEN** the route is METERED and excluded from all roles unless metered consent exists

#### Scenario: Assumed FREE treated as UNKNOWN for critical roles
- **WHEN** a keyless route (P7) is considered for planner, reviewer, controller, or escalation
- **THEN** it is treated as UNKNOWN, ranked last, and cannot be the sole route making H3 non-empty

#### Scenario: Fresh passport allows SUBSCRIPTION for all roles
- **WHEN** an OAuth route (P6) has a fresh agentic passport
- **THEN** it is eligible for planner, reviewer, controller, and escalation

#### Scenario: 402 expiry refreshes on new 402
- **WHEN** a route receives a new HTTP 402
- **THEN** its METERED classification extends 24h from that new 402

### Requirement: ReliabilityObservation v1
The system SHALL record one observation schema, `ReliabilityObservation v1`, mapped onto `strength_profiles.StrengthObservation`:
- `route_id` (canonical, alias-collapsed)
- `served_model` (from the call log or response headers)
- `pool`
- `attempt_id` (Verdict attempt id or gateway call-log id)
- `source` (`verdict_attempt` | `gateway_call_log`)
- `role` (`planner` | `implementer` | `reviewer` | `escalation` | `controller` | `unknown`)
- `outcome` (`success` | `task_failure` | `health_failure`)
- `failure_class` (`StrengthFailureClass`; auth/identity/config/transport/timeout/quota_429/billing_402 are `health_failure`)
- `started_at`, `finished_at`, `observed_at`
- `dedup_key` = sha256(`source_family` + `attempt_id`)

A gateway log matching a Verdict attempt by request id is merged, never counted twice.

**Denominator**: `success + task_failure` only. `task_failure` = completed HTTP response that the role checker rejects (no tool call, invalid plan, empty or skipped review, tests failed, worker gave up). Health failures go to health_cache only and never reduce strength.

**Served-identity validation**: If `served_model != requested route_id` (after alias collapse), the observation is credited to the served identity, and the requested route records `identity_mismatch` as a health failure. Unknown served identity → `role=unknown`, excluded from the denominator.

**Score**: Reliability = Wilson lower bound (z=1.96) of `success/(success+task_failure)` per (identity, role) over 14 days of attempts and the 24h call-log window. Fewer than `n_min=5` observations → reliability unknown (confidence penalty, not zero).

**Demotion**: `>= 20` role attempts with Wilson upper bound `< 0.5` → skip reason `measured_non_completion` at S2 for that role only.

**Cold-start exploration**: Unknown-reliability admitted routes get bounded exploration. Implementer low-risk nodes only, at most 1 exploratory node in 10 and at most 3 per run per route. Never planner, reviewer, controller, escalation, or high-risk nodes. Never ahead of a known-good route (reliability lower bound `>= 0.8`) on a critical node. Every exploration is labelled `exploration=true` in the ladder event.

#### Scenario: Health failures excluded from strength denominator
- **WHEN** an attempt fails with timeout, quota_429, or billing_402
- **THEN** it is recorded in health_cache but does not reduce the Wilson reliability score

#### Scenario: Served identity mismatch credited to served model
- **WHEN** the call log shows a different served model than requested
- **THEN** the observation credits the served identity; the requested route gets `identity_mismatch`

#### Scenario: Demotion triggers at 20 attempts with upper bound < 0.5
- **WHEN** a route has 20+ attempts for a role and Wilson upper bound < 0.5
- **THEN** it receives skip reason `measured_non_completion` at S2 for that role

#### Scenario: Cold-start exploration bounded and labelled
- **WHEN** an implementer low-risk node has no known-good route
- **THEN** at most 1 in 10 nodes and 3 per route per run may explore, labelled `exploration=true`

### Requirement: Quota Sharing Contract
The system SHALL implement the `QuotaView` protocol (frozen in T3): `state(pool, now)` → `{status: open|pressure|cooldown|exhausted, until, reason}`. In T3-T5 it SHALL be a no-op that always returns `open`.

**Store**: One JSON file under `VERDICT_HOME`, `fcntl LOCK_EX` read-modify-write, atomic `os.replace` (same pattern as `orchestration/health_cache.py`). Every update is atomic across processes (orchestrator, escalation, and worker harnesses share it).

**Events**: 429, 'credentials cooling down', `Retry-After`, quota headers from call logs or attempts. Dedup by `dedup_key`, so a repeated event never extends state twice.

**Cooldown Expiry**: `until = max(existing until, now + max(Retry-After, local cooldown))`. It only moves forward until it expires. `Retry-After` is honoured when present and larger. Local cooldown defaults to 60s, doubling per repeat event within 15 min, capped at 30 min.

**Hysteresis**: 
- `open` → `pressure` at `>= 2 x 429 in 15 min`
- `pressure` → `cooldown` on an explicit cooldown message or `Retry-After`
- `cooldown` → `pressure` on expiry
- `pressure` → `open` only after a minimum hold of 10 min with 0 new events

**Config** (precedence: env > `~/.config/verdict/verdict.yaml` key under `quota_share:` > default; read once per ladder build; an out-of-bounds or non-integer value fails closed at load with reason `quota_config_invalid`, never clamped silently):

| Config Field | Env Name | YAML Key | Default | Bounds |
|---|---|---|---|---|
| pressure_429_count | VERDICT_QUOTA_PRESSURE_429_COUNT | quota_share.pressure_429_count | 2 | 1..100 |
| pressure_window_s | VERDICT_QUOTA_PRESSURE_WINDOW_S | quota_share.pressure_window_s | 900 | 60..86400 |
| min_hold_s | VERDICT_QUOTA_MIN_HOLD_S | quota_share.min_hold_s | 600 | 0..86400 |
| cooldown_base_s | VERDICT_QUOTA_COOLDOWN_BASE_S | quota_share.cooldown_base_s | 60 | 1..3600 |
| cooldown_multiplier | VERDICT_QUOTA_COOLDOWN_MULTIPLIER | quota_share.cooldown_multiplier | 2 | 1..10 |
| cooldown_max_s | VERDICT_QUOTA_COOLDOWN_MAX_S | quota_share.cooldown_max_s | 1800 | cooldown_base_s..86400 |
| retry_after_policy | VERDICT_QUOTA_RETRY_AFTER_POLICY | quota_share.retry_after_policy | max | {max} |

(Retry-After wins when larger; no option lets a local value shorten a provider Retry-After.)

**Reserve (by role priority under pressure/cooldown)**: 
- Implementer and reviewer drop the pool first
- Then escalation
- Controller last

During cooldown the controller and escalation try the pool only after its `until` time. Escalation during cooldown takes the next pool in its ladder (one per vendor). It does not wait.

**All pools exhausted for a role** → `LadderResult FAIL_CLOSED` (reason `quota_exhausted_all_pools`) and the runtime ends BLOCKED. Never a wait loop.

#### Scenario: Hysteresis prevents flapping
- **WHEN** a pool enters pressure state
- **THEN** it requires 10 min with 0 new events before returning to open

#### Scenario: Config validation fails closed
- **WHEN** a quota config value is out of bounds or non-integer
- **THEN** the ladder load fails with `quota_config_invalid`, never silently clamps

#### Scenario: Controller retains pool priority under pressure
- **WHEN** quota pressure occurs
- **THEN** implementer and reviewer drop first, escalation second, controller last

#### Scenario: All pools exhausted triggers FAIL_CLOSED
- **WHEN** every pool for a role is exhausted
- **THEN** the ladder returns `FAIL_CLOSED` with reason `quota_exhausted_all_pools`

### Requirement: role_ladder.shadow v1 Event
The `events.jsonl` record type `role_ladder.shadow`, `schema_version 1` SHALL contain:
- `role`
- `node_id`
- `run_id`
- `correlation_id` (joins the later dispatch outcome)
- `mode`
- `H0`, `H1`, `H2`, `H3` (sha256 hashes of sorted canonical route IDs)
- `admitted_set` (full sorted list)
- `dynamic_candidates` (full ordered list with per-route score parts: `external_strength`, `strength_source`, `strength_age_s`, `reliability_lb`, `n`, `confidence`, `capacity_class`, `capacity_confidence`, `capacity_evidence`, `pool`, `quota_status`, `exploration`)
- `skips` `[{route_id, stage, reason}]`
- `legacy_pick`
- `dynamic_pick`
- `agree` (bool)
- `signals_digest`
- `metadata_fixture` (bool)

**Side-effect free guarantees**: No HTTP calls beyond the reads already made for dispatch, no `health_cache` or quota writes, no change to the dispatched route, attempts, or permits. Deterministic for fixed inputs (sorted keys, injected clock).

#### Scenario: Shadow event determinism
- **WHEN** the same inputs are provided with an injected clock
- **THEN** the shadow event is byte-identical across runs

#### Scenario: Shadow event side-effect freedom
- **WHEN** shadow mode runs
- **THEN** no health_cache writes, no quota state changes, no dispatch modifications occur

#### Scenario: Shadow event joins to dispatch outcome
- **WHEN** a node is dispatched after shadow evaluation
- **THEN** the `correlation_id` in the shadow event matches the dispatch record

### Requirement: Flag Modes (Off/Shadow/On) Per Entry Point
The system SHALL support the flag `VERDICT_DYNAMIC_SELECTION=off|shadow|on`, defaulting to `off`.

**Off**: Permanent fail-safe and rollback path. Uses legacy selection algorithm and defaults identically. The one declared exception is the BOD-342 D22 lift (operator 2026-11-17), which is a policy change in every mode. Off is never removed; T12 may delete a legacy constant only when off no longer reads it.

**Shadow**: Legacy dispatch plus a side-effect-free dynamic computation recorded as a `role_ladder.shadow` v1 event.

**On**: Dynamic dispatch. Fails closed on a missing or empty ladder.

**Entry Points**:
- **In scope** (affected by the flag):
  - `verdict orchestrate` (planner, implementer, reviewer, escalation per node)
  - `verdict autodev` (planner via decomposer, executor)
  - Worker spawns through `subagent_selection`/`worker_runtime` (implementer, controller fence)
  - `verdict ladder` CLI (read-only in all modes)
- **Out of scope** (unchanged in all modes):
  - `POST /v1/route`
  - `POST /v1/chat/completions` relay
  - `POST /v1/responses` relay
  - `verdict route` CLI Gate

#### Scenario: Off mode preserves legacy behavior
- **WHEN** `VERDICT_DYNAMIC_SELECTION=off`
- **THEN** the selection matches origin/main exactly, except for the D22 policy lift

#### Scenario: Shadow mode dispatches legacy but records dynamic
- **WHEN** `VERDICT_DYNAMIC_SELECTION=shadow`
- **THEN** the dispatched route matches the legacy selection, and a `role_ladder.shadow` event is written

#### Scenario: On mode fails closed on empty ladder
- **WHEN** `VERDICT_DYNAMIC_SELECTION=on` and H3 is empty
- **THEN** the dispatch fails with `FAIL_CLOSED`, never hangs or defaults

#### Scenario: Relay endpoints unchanged
- **WHEN** any mode is active
- **THEN** `/v1/chat/completions` and `/v1/responses` relay behavior is identical to off

### Requirement: Reviewer Independence
Reviewer independence SHALL be a hard constraint for protected or high-risk work. If no route from a distinct family AND a distinct pool exists, the review SHALL fail closed with `reviewer_independence_unavailable`. For low/medium risk it SHALL fall back to a distinct pool of the same family, with receipt reason `reviewer_same_family_fallback`. A same-pool reviewer SHALL never be allowed.

The served author model family is checked from call logs.

#### Scenario: Hard independence for protected work
- **WHEN** a protected/high-risk node requires review
- **THEN** the reviewer must be from a different family AND different pool; otherwise `FAIL_CLOSED`

#### Scenario: Fallback for low-risk work
- **WHEN** a low/medium risk node requires review and no distinct-family route exists
- **THEN** a distinct-pool same-family route may be used with receipt reason `reviewer_same_family_fallback`

#### Scenario: Same-pool reviewer never allowed
- **WHEN** the only available reviewer shares the author's pool
- **THEN** the review fails closed with `reviewer_independence_unavailable`

### Requirement: Controller Role
The controller SHALL be a first-class role. Its ladder SHALL decide which route the harness root runs. It SHALL also act as the fence: worker, reviewer, and escalation ladders SHALL exclude the controller's served route and, under quota pressure, give way to it. `CONTROLLER_MODEL(S)` constants become legacy-only (retained for off mode). The fence reads the served controller identity from the controller role (same source as `subagent_selection`) in shadow/on; the literal set stays for off mode only.

#### Scenario: Controller fence excludes controller route from workers
- **WHEN** the controller ladder selects a route
- **THEN** worker, reviewer, and escalation ladders exclude that route

#### Scenario: Controller retains pool under quota pressure
- **WHEN** quota pressure occurs
- **THEN** the controller is the last role to drop the pool

#### Scenario: Controller identity source is the controller ladder
- **WHEN** in shadow/on mode
- **THEN** the fence uses the served controller identity from the controller role ladder

### Requirement: Inventory Dispositions
Every hardcoded model reference in the codebase SHALL carry a disposition:
- **DR** (Dynamically Replaced): Replaced by the named task's ladder; legacy value kept for the permanent off fail-safe.
- **RS** (Retained Sentinel): Kept as a setup placeholder or input, labelled unverified where appropriate.
- **RT** (Retained Taxonomy): Capability taxonomy retained for advisory/eligibility input, re-scoped.
- **FX** (Fixture Only): Labelled fixture, never live proof.
- **OO** (Operator Override): Host file, operator-owned; Verdict never writes it.
- **DO** (Display Only): Example or docstring, frozen historical.

#### Scenario: DR constants replaced by ladders
- **WHEN** a DR constant is read in on/shadow mode
- **THEN** the ladder value is used; the constant remains for off mode

#### Scenario: OO files never written by Verdict
- **WHEN** T11 runs
- **THEN** operator files are read but never modified by Verdict

### Requirement: T12 Cleanup Limits
After L1-L5 have passed on main for 14 consecutive days with no rollback, a new OpenSpec change `dynamic-role-selection-legacy-cleanup` MAY remove constants and branches that no path still reads. `VERDICT_DYNAMIC_SELECTION=off` SHALL be kept permanently as the emergency fail-safe and the T11 rollback, and its legacy selection path SHALL keep working. The change SHALL remove only constants and branches that no path still reads. Any constant that off needs SHALL stay. The task MAY be cut if nothing qualifies.

**Never remove**: Flag value `off` or its legacy selection path.

#### Scenario: Off mode tests pass after cleanup
- **WHEN** T12 removes legacy constants
- **THEN** all off-mode tests from T8/T9 pass unchanged

#### Scenario: Off flag and path are permanent
- **WHEN** any cleanup is proposed
- **THEN** `VERDICT_DYNAMIC_SELECTION=off` and its legacy selection path are never removed