# Orchestrate ranks admitted routes by verified success

## Why

`verdict orchestrate` ranks the routes it has admitted by capacity class, slack and the name-based capability tier. The session ledger already holds every controller-verified pass and fail of a real worker attempt, and `_rank_components` already reports a score from it, but `_rank_key` ignores it ("observational only in phase 1"). A route that failed the last four implement nodes still wins a tie against a route with no history; a route that passed six in a row never rises above it. That is the gap between "cheapest model proven capable" and "cheapest model by name" (GitHub issue #1216, plan `20261120-learning-loop`, story L1b).

Mission link: free and cheap routes that really succeed rise within their class, and a route that keeps failing stops being tried first. That raises accepted work per paid dollar without ever spending more than the capacity class already allows.

## What Changes

**`verdict/orchestration/success_rank.py` (new)**

- `rank_success` scores one route for one node kind from the ledger's verified real-work rows, using the existing `score_task_history` (no second formula). Key `route+kind`; with fewer than `min_sample` results for it, key `route` (every kind); with fewer than that, neutral 0.5 and no key.

**`verdict/orchestration/session_evidence.py`**

- `SessionLedger.verified_real_outcomes(now, window_days)`: verified rows of kind `real_task` inside the window. `pool_key` names the alias pool `summarize_pool` already uses.

**`verdict/orchestration/contracts.py`**

- `TaskRequirements.node_kind`, set by `for_node` from the node.

**`verdict/orchestration/eligibility.py`**

- The ladder reads the ledger once per selection and puts the success rank second in `_rank_key`: after the capacity class, before slack and every name-based or rating key. `rank_components.success` records the score, n, key, source and the two settings for every candidate the receipt lists. `EligibilityLadder` takes `success_window_days` (14) and `success_min_sample` (3).

## Capabilities

### New Capabilities

- `orchestration-success-ranking`: how admitted routes are ordered within a capacity class by verified outcomes, and what the receipt records about it.

## Impact

`verdict/orchestration/{success_rank,session_evidence,contracts,eligibility}.py`, `docs/CONFIGURATION.md`, tests. No CLI, schema or event-type change. Each candidate in a selection event gains a `success` object (about 150 bytes).

Invariants this touches (AGENTS.md): admission comes before ranking and the admitted set only narrows (the score is read after admission and changes no membership or reason); the capacity class stays the first key; unknown is never healthy (a route without results is neutral, not good); fixture and imported rows never count.

## Evidence boundary

- **KNOWN:**
  - `SessionLedger` rows written by the runtime have `kind="real_task"`, `verified_by_controller=True` and, since #1397, the node's `node_kind`. Imported rows carry `session_canary_v1` or `free_real_task`.
  - `score_task_history` is `(weighted accepted + 0.5 * weighted partial + 1) / (weighted eligible + 2)` over a 30-day window with a 7-day half-life, and reports `observed_n`.
  - `verdict orchestrate` builds its ladder with `build_selector`, which attaches `SessionLedger()`; the runtime builds each node's requirements with `TaskRequirements.for_node`.
  - FREE and UNKNOWN routes whose pooled score is under 0.5 are already refused by the task gate.
- **INFERRED:** a minimum sample of 3, the history scorer's own demotion floor, is a sound first value; tenth-resolution ordering keeps a day of decay from outranking slack. Neither has live data behind it yet.
- **NOT AVAILABLE:** a live run showing the rank (task 4.1); the task class with risk (the ledger has no risk field); how the 52-row ledger of 2026-11-20 behaves per kind.

## Acceptance criteria

- Two admitted routes in one capacity class: the one with recent verified failures for the node's kind ranks below a route with no results; for another kind the same route ranks as before.
- A route with many verified passes ranks above a route with none in its class, and never above any route of a cheaper class.
- With fewer than the minimum sample for the kind the key is the route alone, and each candidate's `rank_components.success` names the key used, the score, n, the source, the window and the minimum.
- Membership and every rejection reason are identical with and without a ledger.
- Imported, probe and unverified rows do not count.
- (post-merge) On installed main, the receipt of a real `verdict orchestrate` run shows `success` with a score, n and key on its selection events.
