# Screening that learns: a queue built from evidence, and real work that counts as proof

## Why

Verdict discovers about 4,200 routes and can use about 33. "Tool proof before dispatch" is in force, so a route needs a proof, and the job that makes proofs cannot get ahead (GitHub issue #1387):

- It checks 40 routes every six hours and a proof lasts 24 hours, so the proven pool is whatever passed in the last day: 47, 56 and 41 routes after three consecutive runs.
- It does not learn. Over three runs, 40 of 120 probes went to two route prefixes that did not pass once (`api-airforce` 0 of 27, `ddgw` 0 of 13).
- It ignores what is already known for free. The gateway has per-model health, a day of real traffic per model, declared tool support, a free-model catalog with a terms rating, a ranking of free providers and candidate pools by channel. Verdict reads one candidate pool (`auto`) and none of the rest for the queue.
- A real task that used tools and passed its verification does not count as a proof. A route that works every day still expires and is probed again.
- 142 routes that pass every passive check are never screened, because their capacity class is `unknown` and the timer screens only `free` and `subscription`.

A run restricted to free routes found no eligible worker route at all (run `20261127T191901Z`).

## What Changes

**The screen queue (`verdict identify --screen`)**

- Candidates are the routes that pass the passive checks, plus frontier subscription routes. Routes that the passive checks already reject are not probed.
- The queue is ordered by expected value: the chance that a route passes, estimated from its prefix's own screening record and its passive health, times its quality rating.
- A prefix that keeps failing is backed off and reported, instead of taking the same share of every run.
- `unknown`-capacity routes are screened on the same terms as free ones (operator decision, 2026-11-27).
- A route whose provider the gateway's free catalog rates `avoid` on terms of use is not screened unless the operator has allowed that provider.
- Every run reports the size of the proven pool, the backlog and each prefix's record.

**Proof from real work (`verdict orchestrate`)**

- When a node that needed tools passes the run's verification on a live worker whose served model was verified, that exact route gets a tool proof, as the manual session import already grants (operator decision, 2026-11-27).

## Capabilities

### New Capabilities

- `route-screening`: which routes the scheduled screen probes, in what order, and what it reports.
- `real-task-tool-proof`: when a verified real task creates or renews a route's tool proof.

## Impact

`verdict/identify.py`, `verdict/identify_pools.py`, `verdict/identify_service.py`, new readers in `verdict/omniroute_evidence.py`, `verdict/orchestration/runtime.py`, `verdict/orchestration/session_feedback.py`, `verdict/orchestration/health_cache.py`, `verdict/doctor*.py`, docs and tests. The installed timer's arguments change, which `verdict setup` applies.

Rules this must keep (AGENTS.md, ADR-010, ADR-014, ADR-049): passive evidence orders and filters the queue and never grants eligibility; unknown is never healthy; a proof is for the exact route; advisory signals never change membership. The second capability amends one existing rule on purpose: the `session_feedback` rule that a session outcome can only narrow, and the `passive-health-refresh` requirement that passive traffic never grants tool proof. A controller-verified task is not passive traffic; the amendment says exactly when it counts.

## Evidence boundary

- **KNOWN** (on #1387; journal and read-only gateway reads of 2026-11-27, installed main `8e163edf`, OmniRoute 3.8.54):
  - Three screen runs: 40 checked each; 8, 3 and 18 passed; 47, 56 and 41 routes held a proof afterwards.
  - Rebuilt from four runs (160 probes): free routes inside the passively admitted pool passed 7 of 32, free routes outside it 1 of 71, subscription routes outside it 11 of 27.
  - 177 admitted, unproven routes at 18:20 UTC: 16 free, 19 subscription, 142 unknown capacity.
  - `record_agentic_evidence` has one caller, the manual session import; a dispatch success records liveness only.
  - Gateway reads that work without inference and that the queue does not use: `/api/monitoring/model-health`, `/api/usage/model-latency-stats`, `capabilities.tool_calling` and the capability filter on `/v1/models` (3,739 of about 4,250 ids), candidate channels `best-free` and `coding:free` (143 rows, 66 of them reachable, healthy and declaring tools), `/api/free-models` (465 rows with `freeType` and a `tos` rating: 311 caution, 71 avoid, 52 ok), `/api/free-provider-rankings` (37 providers with an average score).
  - The `free-coding-models` roster: 222 models, 51 matched to a gateway route, 8 with a fresh tool proof.
- **INFERRED:** a prefix's recent pass rate predicts its next result well enough to order by. The numbers above suggest it; nothing has tested it.
- **NOT AVAILABLE:** the cost of one probe in requests and seconds by provider; how the proven pool behaves under the new order (task 5.1); a live run in which a free route does a worker node (task 5.2); whether the gateway's `tos` rating is right for any given provider.

## Acceptance criteria

- With recorded evidence as input, the routes a screen run probes are the passively admitted ones (plus frontier subscription routes) in expected-value order; a prefix with a long record of failures and no pass is skipped and named in the report.
- `unknown`-capacity routes are probed on the same terms as free ones; a route of a provider rated `avoid` is not probed unless the operator allowed the provider.
- No passive source makes a route eligible: with every passive signal favourable and no proof, the route is still `no_agentic_probe` for a worker node.
- A node that needed tools and passed verification on a live worker with a verified served model gives that exact route a tool proof; a node without tool use, a fault-injected executor, an unverified served model or a failed verification gives none.
- (post-merge) On installed main the number of free and unknown-capacity routes holding a tool proof is reported daily for a week and compared with 2026-11-27.
- (post-merge) A `verdict orchestrate` run restricted to free routes dispatches a worker node.
