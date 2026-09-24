# ADR-036: Goal-to-receipt orchestration with capacity-aware, same-node recovery

- **Status:** Accepted: implemented and merged to `main` via #590 (`4617445`)
- **Date:** 2026-09-24
- **Related:** ADR-0001 (eligibility before ranking), ADR-015/017 (receipts), ADR-023 (governed
  supervision, superseded in scope by this ADR for the orchestration runtime), ADR-031 (Prime
  workflow skills), ADR-032 (OmniRoute is inventory/execute/health only)
- **Stories:** BOD-151 (topology), BOD-152 (failure intelligence), BOD-153 (DAG fan-out),
  BOD-159..166 (controller survival, minimum slice), BOD-177 (terminal view), BOD-185 (independent review)

## Context

Verdict could select one route and could run a decomposed objective, but units ran in sequence on
one tree. Worker model choice was manual or fixed. A quota-exhausted worker or controller stopped
the run, or made it hang. No verdict required independent review before a run could be called done.

## Decision

`verdict.orchestration` owns one run from goal to receipt. Prime Agent is only the execution
harness. OmniRoute is only transport and inventory.

1. **Frontier decomposition (BOD-151).** One frontier model call returns a JSON `WorkGraph`. The
   frontier model is picked through the same eligibility ladder, with `frontier_worthy=True`.
   Verdict validates the graph: no cycles, no unknown dependencies, and no shared write ownership
   between nodes that can run at the same time. It normalizes planner free text to its own
   capability vocabulary. It then picks a topology (`SOLO`, `WORKER_CRITIC`,
   `PARALLEL_WORK_UNITS`) with deterministic, replayable rules. The planner never picks worker
   models.
2. **Eligibility ladder.** For every node attempt, each route must pass these stages in order:
   `DISCOVERED` (live `/v1/models`), `ENTITLED` (an active OmniRoute account backs it, and the
   harness can spawn it), `HEALTHY` (a fresh cached 1-token probe, probed lazily and bounded),
   `AVAILABLE` (no route or provider cooldown, no account rate-limit window), `TASK_ELIGIBLE`
   (capabilities, context, frontier policy, reviewer exclusions), then `SELECTED`. Economics only
   orders the routes that pass. Capacity class comes from account evidence (OAuth plan labels,
   free-only flags, pricing), not from model names. The order is
   subscription > free > metered > unknown. A configurable provider preference (default
   `claude`) and current load then spread concurrent nodes. There is no static fallback chain.
3. **DAG runtime (BOD-153).** Ready nodes run at the same time, up to `max_parallel`. Each attempt
   runs in its own git worktree, based on its validated dependencies. Lifecycle:
   `PLANNED -> ADMITTED -> DISPATCHED -> RUNNING -> TERMINAL_SUCCESS/TERMINAL_FAILURE ->
   VALIDATED/REJECTED`. Admission is never success. A node is `VALIDATED` only after its
   ownership barrier and its verification command pass. Integration nodes merge validated commits
   and run the combined check. A failed node blocks only its dependents. Siblings keep running.
4. **Failure intelligence and same-node reassignment (BOD-152).** Terminals are classified from
   the status code first and versioned text second. Classes include quota vs. rate limit,
   401/402/403/400/404, 5xx, timeout, transport, empty or no final answer, malformed, model
   mismatch, gateway admission shed, verification and ownership. Reset hints (`Retry-After`,
   `resets at`, `resets in 2h`) set the cooldown. The scope is route or provider, and
   model-scoped caps cool down only the route. The same node contract is then sent to a newly
   selected route. Gateway-local admission sheds retry the same route without penalizing the
   model. When the bounded pool is empty, the result is an explicit `FAIL_CLOSED`.
5. **Independent review (BOD-185).** After integration, Alibaba OpenCodeReview (`ocr`) reviews the
   integrated diff. It runs on a reviewer model that the ladder selects, excluding every
   implementer route, and a different model family when one has capacity. The OCR config is
   isolated per run, and the key never appears in argv or in artifacts. An error, timeout, empty
   output, or every-item-failed result is `ERROR`, never `PASS`. If a reviewer provider fails,
   another independent reviewer is selected.
6. **Receipt.** An append-only, fsynced, secret-scrubbed `events.jsonl` is the single source for
   both the terminal view and `receipt.json`. The receipt carries per-node attempts (route,
   provider, capacity class, outcome, fault injected, abandoned), cooldowns, reassignments, review
   and independence, and a digest of the event log. `completion_verdict` returns `COMPLETE` only
   with validated work, an ok integration barrier and a `PASS` review. Otherwise it returns
   `BLOCKED` with the first concrete reason.
7. **Controller survival (BOD-159..166, minimum slice).** Frontier planning uses the same
   classify, cooldown and reselect loop, so controller-model quota moves planning to another
   frontier model. `verdict supervise` runs the controller as a child process and watches
   `progress.json`. It kills a stalled or quota-dead controller's process group, restarts with
   `--resume` (validated nodes are reused, and abandoned attempts are recorded), and has bounded
   restarts and a deadline. When it gives up, it writes `FAILED_CLOSED` and a `BLOCKED` verdict.
8. **Terminal view (BOD-177).** `verdict orchestrate`, `watch`, `run-receipt`, `eligibility` and
   `supervise` render the event stream with the existing Verdict design tokens from
   `terminal_ui.py`. There is a plain ASCII fallback for NO_COLOR, CI or non-TTY output.

## Consequences

- Quota, auth or transport loss on one worker or on the controller does not stop the run.
  Recovery is bounded and visible in the receipt.
- Chaos is first-class: `--inject` keys (`route`, `cc/*`, `@node`, `#N`, `*`) and
  `VERDICT_CHAOS_G<n>` inject faults. Injected failures are marked `fault_injected` in every
  artifact, and chaos runs use an isolated health-state file.
- Harness visibility is part of `ENTITLED`: a route that Prime cannot spawn is not eligible.
  Today this reads Prime's `models.json` model ids, never credentials.

## Known limits (not shipped)

- Worker concurrency within a story is not yet adapted from dogfood outcomes (BOD-157). The cap
  is operator-set (default 3).
- `WORKER_CRITIC` and `SOLO` topologies are selected and recorded. A separate critic pass per
  node is not yet executed. Independent review runs once per run, on the integrated diff.
- Controller survival is a local supervisor process, not a daemon or service unit.
