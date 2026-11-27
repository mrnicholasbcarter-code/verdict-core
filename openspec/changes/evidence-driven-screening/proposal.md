# Screening that learns: stop probing what never passes, and let real work count as proof

## Why

Verdict discovers about 4,200 route ids and can use about 33. "Tool proof before dispatch" is in force, so a route needs a proof, and the job that makes proofs spends its budget badly (GitHub issue #1387):

- It checks 40 routes every six hours and a proof lasts 24 hours, so the proven pool is whatever passed in the last day: 47, 56 and 41 routes after three consecutive runs.
- It does not learn. Over three runs, 42 of 120 probes went to two providers that did not pass once (`api-airforce` 0 of 27, `duckduckgo-web` 0 of 15). `api-airforce` alone holds 375 of the 529 free models waiting for a proof.
- A real task that used tools and passed its verification does not count as a proof. 36 of the 120 probes went to subscription routes that do real work every day.
- 142 routes that pass every passive check are never screened, because their capacity class is `unknown` and the timer screens only `free` and `subscription`.
- The catalog lists most models under two ids, and the counts above treat them as two routes: 4,243 ids are 2,312 distinct models.

A run restricted to free routes found no eligible worker route at all (run `20261127T191901Z`).

Without the provider that never passes, 154 free models are waiting. That is one day of probes. The backlog is a matter of order, not of capacity.

## What Changes

**The screen queue (`verdict identify --screen`)**

- One probe per model: the queue is keyed on the catalog's provider and model, not on the id.
- A provider with a week of probes and no pass is backed off to one probe a day and named in the report.
- A route that does not declare tool support is not probed.
- Providers that have passed are probed first, then routes the passive check admits, then the rest; one probe in eight is kept for providers with no record.
- `unknown`-capacity routes are screened on the same terms as free ones (operator decision, 2026-11-27).
- An operator-owned deny list excludes a provider. The gateway's terms rating is shown beside each provider and excludes nothing.
- Connections that sign in as a person get at most two probes per run and none while they are terminal or cooling.
- Every run reports the size of the proven pool, the backlog and each provider's record.

**Proof from real work (`verdict orchestrate`)**

- When a node that needed tools passes the run's verification on a live worker whose served model was verified, that exact route gets a tool proof, as the manual session import already grants (operator decision, 2026-11-27).

## Capabilities

### New Capabilities

- `route-screening`: which routes the scheduled screen probes, in what order, and what it reports.
- `real-task-tool-proof`: when a verified real task creates or renews a route's tool proof.

## Impact

`verdict/identify.py`, `verdict/identify_pools.py`, `verdict/identify_service.py`, `verdict/prove_at_rest.py`, one new reader in `verdict/omniroute_evidence.py`, `verdict/orchestration/runtime.py`, `verdict/orchestration/session_feedback.py`, `verdict/orchestration/health_cache.py`, `verdict/doctor*.py`, docs and tests. The installed timer's arguments change, which `verdict setup` applies.

Rules this must keep (AGENTS.md, ADR-010, ADR-014, ADR-049): passive evidence orders and filters the queue and never grants eligibility; unknown is never healthy; a proof is for the exact route; advisory signals never change membership. The second capability amends one existing rule on purpose: the `session_feedback` rule that a session outcome can only narrow, and the `passive-health-refresh` requirement that passive traffic never grants tool proof. A controller-verified task is not passive traffic; the amendment says exactly when it counts.

## Evidence boundary

- **KNOWN** (journal of three screen runs and read-only gateway reads of 2026-11-27, installed main `46ef6daa`, OmniRoute 3.8.54; the analysis is on #1387):
  - Three screen runs: 40 probes each; 8, 3 and 18 passed. Free routes passed 10 of 84, subscription routes 19 of 36.
  - Free passes by provider: `openrouter` 5 of 11, `bazaarlink` 3 of 7, `nvidia` 2 of 5; nine other providers 0 of 61.
  - Free routes that declare tool support passed 10 of 66; those that do not, 0 of 18.
  - With the two never-passing providers left out, free routes in the gateway's free candidate pool passed 7 of 15 and those outside it 3 of 27.
  - `/v1/models` lists 4,243 ids for 2,312 distinct `owned_by` + `root` pairs; 1,706 ids carry a `parent`. Three of 96 probed models were probed under both ids.
  - The 18:20 queue holds 559 distinct free models; 529 declare tools; 375 of those are `api-airforce`.
  - `record_agentic_evidence` has one caller, the manual session import; a dispatch success records liveness only.
  - Signals that did not separate passes from failures: the candidate row's `reachable` flag (6 of 27 against 1 of 3), free-catalog membership (1 of 26 against 9 of 58), having a rating (1 of 11 against 9 of 73), and the gateway's success rate (`duckduckgo-web`: 666 of 785 gateway requests succeeded, 0 of 15 probes passed).
  - The gateway setting `excludeTosAvoid` is `false`; no connection is marked banned.
- **INFERRED:**
  - A provider's record predicts its next results. The figures above are computed on the same probes they describe; task 5.1 tests it forward.
  - A probe from Verdict can mark a connection banned. The gateway's ban-detection document says only its own test probes are isolated and that a ban phrase in an upstream error marks the connection banned for a year; this was read in the 3.8.55 docs and not observed.
  - The gateway can answer a budget-exhausted request from another model (emergency fallback, on by default per its docs). Not observed; task 0.2 checks it.
- **NOT AVAILABLE:** the cost of one probe in requests and seconds by provider; how the proven pool behaves under the new order (task 5.1); a live run in which a free route does a worker node (task 5.2); whether the gateway's terms rating is right for any given provider.

## Acceptance criteria

- With recorded evidence as input, a screen run probes each model at most once, skips routes that do not declare tools, skips a provider with a week of probes and no pass except for its one daily probe, and probes providers with passes before the rest.
- `unknown`-capacity routes are probed on the same terms as free ones; a provider on the operator's deny list is not probed, and a terms rating alone excludes nothing.
- A connection that signs in as a person receives at most two probes in one run and none while it is terminal or cooling.
- No passive source makes a route eligible: with every passive signal favourable and no proof, the route is still `no_agentic_probe` for a worker node.
- A node that needed tools and passed verification on a live worker with a verified served model gives that exact route a tool proof; a node without tool use, a fault-injected executor, an unverified served model or a failed verification gives none.
- (post-merge) On installed main the number of free and unknown-capacity models holding a tool proof is reported daily for a week and compared with 2026-11-27.
- (post-merge) A `verdict orchestrate` run restricted to free routes dispatches a worker node.
