# ADR-042: Dynamic Role-Based Model Selection

**Status:** Accepted (operator, 2026-11-17)
**Date:** 2026-11-17
**Deciders:** Operator
**Amends:** ADR-036 (Planner and Reviewer selection)
**Related:** ADR-0001 (Control-plane invariants), ADR-032 (Core model metadata store)

## Context

Verdict currently uses a fixed or manual model selection for its orchestration roles. While the eligibility ladder in ADR-036 ensures basic qualification, it does not dynamically optimize for the specific requirements of different roles (Planner, Implementer, Reviewer, Escalation, Controller) based on live measured strength, capacity, and quota. To maximize efficiency and quality, Verdict needs a role-aware selection pipeline that leverages the Core metadata store and measured performance.

## Decision

Verdict implements a dynamic model selection pipeline for orchestration roles. The selection process is governed by a strict multi-stage pipeline contract.

### 1. Pipeline Contract (Four Stages, Hash-Bounded)

- **S0: Inventory** — Fetches live `/v1/models` rows (or a fixture labelled `fixture=true`). Output: **H0** (sha256 of sorted canonical route IDs).
- **S1: Admission** — Applies the existing admission ladder and eligibility floors (`verdict/admission.py`, `eligibility.py`, `orchestration/eligibility.py`). Opaque `auto/*` and `combo/*` routes are removed. Unknown runtimes are never healthy. Output: Canonical **AdmittedSet (H1)**. Dynamic selection never adds to this set.
- **S2: Role Narrowing (Remove-Only)** — Iteratively removes candidates. Each removal carries a skip reason:
  - **Capacity**: Role policy over capacity class + confidence (single enforcement point: `capacity_policy.role_capacity_allowed`).
  - **Quota**: `QuotaView` check for cooldown/reserve (hysteresis, monotonic cooldowns, role reserve order).
  - **Pool**: Keeps at most one route, the best-ordered, per quota pool in every ladder. The quota pool is `provider_catalog.backend_pool(route_id)` with aliases resolved by `aliased_pools_for`; routes in one backend pool share quota.
  - **Family**: Enforces reviewer independence, controller fence, implementer exclusion.
  - **Measured Demotion**: Removes routes with role-scoped non-completion (Wilson upper bound < 0.5 at ≥ 20 attempts).
  - **Operator Override**: Applies explicit exclusions from the operator override file.
  Output: **H2** plus skip list `[{route_id, stage, reason}]`.
- **S3: Advisory Order** — Applies the `role_strength` signal through `advise_order`. Low confidence (stale or missing) leaves the legacy order. Output: Ordered ladder **H3**.

**Assertions (invariant, enforced in T3 and re-tested by every later task):**
- `H3 set == H2 set`
- `H2 subset of H1`
- Every route in `H3` present in `AdmittedSet`
- An empty `H3` results in `LadderResult(status=FAIL_CLOSED, reason=...)` — never a silent default, never a hang.

### 2. Capacity Precedence (P1–P8) and Conflict Rules

Evaluated per route, highest precedence first. Scope is connection unless stated. A lower row never downgrades the class set by a higher row.

| Row | Evidence | Class | Confidence | Notes |
|-----|----------|-------|------------|-------|
| P1 | Route-scoped billing (`billing_402_metered` entry.until > now) | METERED | observed | 24h expiry after last 402; refreshed by new 402 |
| P2 | Connection-scoped ≥1 HTTP 402 within 24h (call logs) | METERED | observed | Applies to every route of connection; same expiry |
| P3 | Explicit positive pricing (price > 0) | METERED | declared | Never FREE |
| P4 | OpenRouter outside public free set | METERED | declared | Inside free set → FREE (declared); Anthropic API key → METERED |
| P5 | `:free` suffix / connection free signal / all-zero pricing | FREE | declared | Non-OpenRouter |
| P6 | OAuth with no paid evidence | SUBSCRIPTION | assumed (`assumed_subscription_oauth`) | |
| P7 | No evidence at all | FREE | assumed (`assumed_free_no_paid_evidence`) | D7 unchanged for legacy callers |
| P8 | Connection blocked (auth/verification failure) | unchanged | — | Availability UNAVAILABLE; never healthy until fresh success |

Missing connection row → UNKNOWN (fails entitlement).

**Conflicts and Role Eligibility:**
- Billing vs OAuth: METERED wins. Excluded from every role unless metered consent applies. **Pay-per-token is never used by default.**
- Billing expired vs pricing positive: METERED (P3).
- Pricing zero vs observed 402: METERED (P1/P2 outrank P5).
- OAuth assumed SUBSCRIPTION: eligible for all roles once a fresh agentic passport or success exists. Without one: implementer low-risk only, ranked after observed/declared subscription.
- Assumed FREE (P7): implementer low-risk with fresh passport only. For **planner, reviewer, controller, and escalation** it is treated as **UNKNOWN**, ranked last, and never makes a ladder non-empty on its own (**Decided, operator, 2026-11-17** — D7).

**Freshness:** Evidence older than its TTL drops one confidence level (observed → assumed). Billing evidence expires only through explicit 402 expiry. Name or vendor literals are not inputs.

**Single Enforcement Point:** `capacity_policy.role_capacity_allowed(role, capacity_class, capacity_confidence)` is the sole function determining whether a capacity class is permitted for a role.

### 3. Reliability and Quota Contract

**ReliabilityObservation v1** (mapped onto `strength_profiles.StrengthObservation`):
- Fields: `route_id` (canonical, alias-collapsed), `served_model` (call log/response headers), `pool`, `attempt_id`, `source` (`verdict_attempt` | `gateway_call_log`), `role` (planner|implementer|reviewer|escalation|controller|unknown), `outcome` (success|task_failure|health_failure), `failure_class` (`StrengthFailureClass`; auth/identity/config/transport/timeout/quota_429/billing_402 are `health_failure`), `started_at`, `finished_at`, `observed_at`, `dedup_key` = sha256(`source_family` + `attempt_id`).
- Gateway log matching a Verdict attempt by request id is merged, never counted twice.
- **Denominator**: `success + task_failure` only. `task_failure` = completed HTTP response the role checker rejects (no tool call, invalid plan, empty/skipped review, tests failed, worker gave up). Health failures go to `health_cache` only; never reduce strength.
- **Served-identity validation**: If `served_model != requested route_id` (after alias collapse), observation credited to served identity; requested route records `identity_mismatch` as health failure. Unknown served identity → `role=unknown`, excluded from denominator.
- **Score**: Wilson lower bound (z=1.96) of `success/(success+task_failure)` per (identity, role) over 14 days and 24h call-log window. Fewer than `n_min=5` → reliability unknown (confidence penalty, not zero).
- **Demotion**: ≥ 20 role attempts with Wilson upper bound < 0.5 → skip reason `measured_non_completion` at S2 for that role only.
- **Cold-start exploration**: Unknown-reliability admitted routes get bounded exploration. Implementer low-risk nodes only; at most 1 in 10 and 3 per run per route. Never planner, reviewer, controller, escalation, or high-risk. Never ahead of known-good route (reliability LB ≥ 0.8) on critical node. Every exploration labelled `exploration=true` in ladder event.

**QuotaView Protocol (frozen in T3)**: `state(pool, now)` → `{status: open|pressure|cooldown|exhausted, until, reason}`. In T3–T5 no-op returning `open`.

**T6 Implementation (`verdict/quota_share.py`):**
- Store: One JSON file under `VERDICT_HOME`, `fcntl LOCK_EX` read-modify-write, atomic `os.replace` (same pattern as `orchestration/health_cache.py`). Atomic across orchestrator, escalation, and worker harnesses.
- Events: 429, 'credentials cooling down', `Retry-After`, quota headers from call logs or attempts. Dedup by `dedup_key`.
- **Cooldown expiry**: `until = max(existing until, now + max(Retry-After, local cooldown))`. Monotonic forward. `Retry-After` wins when larger. Local cooldown defaults 60s, doubling per repeat within 15 min, capped 30 min.
- **Hysteresis**: open→pressure at ≥2×429 in 15 min; pressure→cooldown on explicit cooldown/`Retry-After`; cooldown→pressure on expiry; pressure→open only after min hold 10 min with 0 new events.
- **Config** (precedence: env > `~/.config/verdict/verdict.yaml` key `quota_share:` > default; read once per ladder build; out-of-bounds/non-integer fails closed with `quota_config_invalid`, never clamped):

| Field | Env | YAML Key | Default | Bounds |
|-------|-----|----------|---------|--------|
| pressure_429_count | VERDICT_QUOTA_PRESSURE_429_COUNT | quota_share.pressure_429_count | 2 | 1..100 |
| pressure_window_s | VERDICT_QUOTA_PRESSURE_WINDOW_S | quota_share.pressure_window_s | 900 | 60..86400 |
| min_hold_s | VERDICT_QUOTA_MIN_HOLD_S | quota_share.min_hold_s | 600 | 0..86400 |
| cooldown_base_s | VERDICT_QUOTA_COOLDOWN_BASE_S | quota_share.cooldown_base_s | 60 | 1..3600 |
| cooldown_multiplier | VERDICT_QUOTA_COOLDOWN_MULTIPLIER | quota_share.cooldown_multiplier | 2 | 1..10 |
| cooldown_max_s | VERDICT_QUOTA_COOLDOWN_MAX_S | quota_share.cooldown_max_s | 1800 | cooldown_base_s..86400 |
| retry_after_policy | VERDICT_QUOTA_RETRY_AFTER_POLICY | quota_share.retry_after_policy | max | {max} |

- **Reserve by role priority** (under pressure/cooldown): implementer and reviewer drop first, then escalation, controller last. During cooldown, controller and escalation try pool only after its `until`; escalation takes next pool in its ladder (one per vendor), does not wait.
- **All pools exhausted** → `LadderResult FAIL_CLOSED` (reason `quota_exhausted_all_pools`), runtime ends BLOCKED. Never a wait loop.

### 4. Shadow Evaluation and Flag Modes

**Shadow Event** (`role_ladder.shadow`, schema_version 1): `role`, `node_id`, `run_id`, `correlation_id` (joins dispatch outcome), `mode`, `H0`, `H1`, `H2`, `H3`, `admitted_set` (full sorted list), `dynamic_candidates` (full ordered list with per-route score parts: `external_strength`, `strength_source`, `strength_age_s`, `reliability_lb`, `n`, `confidence`, `capacity_class`, `capacity_confidence`, `capacity_evidence`, `pool`, `quota_status`, `exploration`), `skips` `[{route_id, stage, reason}]`, `legacy_pick`, `dynamic_pick`, `agree` (bool), `signals_digest`, `metadata_fixture` (bool). Side-effect free: no HTTP beyond dispatch reads, no `health_cache`/quota writes, no dispatch/attempt/permit changes. Deterministic for fixed inputs (sorted keys, injected clock).

**Flag**: `VERDICT_DYNAMIC_SELECTION=off|shadow|on`, default `off`.

| Mode | Behavior |
|------|----------|
| **off** | Permanent fail-safe and rollback. Legacy selection identical to origin/main (except BOD-342 D22 lift, which applies in every mode). Never removed; T12 may delete a legacy constant only when off no longer reads it. |
| **shadow** | Legacy dispatch plus side-effect-free dynamic computation recorded as `role_ladder.shadow` v1 event. |
| **on** | Dynamic dispatch. Fails closed on missing or empty ladder. |

**Entry Points:**
- **In scope** (affected by flag): `verdict orchestrate` (planner, implementer, reviewer, escalation per node), `verdict autodev` (planner via decomposer, executor), worker spawns through `subagent_selection`/`worker_runtime` (implementer, controller fence), `verdict ladder` CLI (read-only in all modes).
- **Out of scope** (unchanged in all modes): `POST /v1/route`, `POST /v1/chat/completions` relay, `POST /v1/responses` relay, `verdict route` CLI Gate.

### 5. Role Specifics

**Reviewer Independence (Hard for Protected/High-Risk):** If no route from a distinct family AND distinct pool exists, review fails closed with `reviewer_independence_unavailable`. For low/medium risk: falls back to distinct pool of same family, receipt reason `reviewer_same_family_fallback`. Same-pool reviewer never allowed. Served author model family checked from call logs.

**Controller Role (First-Class):** Ladder decides which route the harness root runs. Also the fence: worker, reviewer, and escalation ladders exclude the controller's served route and, under quota pressure, give way to it. `CONTROLLER_MODEL(S)` constants become legacy-only (retained for off mode). Fence reads served controller identity from the controller role (same source as `subagent_selection`) in shadow/on; literal set stays for off mode only.

**Inventory Dispositions:** Every hardcoded reference carries a disposition:
- **DR** (Dynamically Replaced): Replaced by named task's ladder; legacy value kept for off fail-safe.
- **RS** (Retained Sentinel): Setup placeholder/input, labelled unverified where appropriate.
- **RT** (Retained Taxonomy): Capability taxonomy for advisory/eligibility, re-scoped.
- **FX** (Fixture Only): Labelled fixture, never live proof.
- **OO** (Operator Override): Host file, operator-owned; Verdict never writes.
- **DO** (Display Only): Example/docstring, frozen historical.

**T12 Cleanup Limits:** After L1-L5 pass on main for 14 consecutive days with no rollback, a new OpenSpec change `dynamic-role-selection-legacy-cleanup` may remove constants/branches no path still reads. `VERDICT_DYNAMIC_SELECTION=off` kept permanently as emergency fail-safe and T11 rollback. Task may be cut if nothing qualifies. **Never remove flag value `off` or its legacy selection path.**

### 6. Invariants (from ADR-0001, Unchanged)

- Admission comes before ranking.
- Unknown is never healthy.
- Opaque routes are never candidates.
- Attempts are bounded.
- Review runs on a separate route.
- A receipt is bound to the SHA-256 of `events.jsonl`.
- Advisory signals may reorder but never authorize.
- Fixture data never satisfies live proof.

## Consequences

- **Deterministic Selection**: Every selection is provenance-rich and replayable via the `role_ladder.shadow` event.
- **Safe Deployment**: Shadow mode allows validation of the dynamic pipeline against the legacy baseline without risking execution.
- **Reduced Waste**: Measured demotion and quota awareness reduce failed attempts.
- **Strict Safety**: The `FAIL_CLOSED` behavior for empty sets prevents silent failures or hangs.
- **Operator Control**: The permanent `off` mode and override file give the operator full control over the transition.