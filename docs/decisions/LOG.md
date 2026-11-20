# Decision Log

Source: [BOD-342 — Operator decision log](https://linear.app/bodanglin/issue/BOD-342/decisions-operator-decision-log-2026-10-13-memory-system-enforcement)
(Linear, Bodanglin workspace, exported 2026-11-20).

This file contains decisions D1–D22 imported verbatim. D23 and beyond remain in the
BOD-342 comment thread and are pending operator review; each new entry here is one line
pointing to its ADR.

**Note on D23–D25:** D23 (two colliding entries: BOD-387 worker/reviewer rule 2026-10-15
and the Part C bulk-move exception 2026-11-11), D24 (diff-format repair), and D25
(patch truncation rule) exist in the Linear comments. See the private Linear archive.

---

Operator decisions, 2026-10-13. Linear is the system of record for decisions. Files under \~/.agents/operator are only mirrors, and ai-memory holds cited copies.

## D1: ai-memory is the memory backend

* Decided: akitaonrails/ai-memory is the single memory backend for Prime, and later for Verdict. The four-proposal research panel was cancelled.
* Hindsight is a reference only and is never run as a second service. Engram was rejected because it relies on the agent calling mem\_save.
* Restates BOD-281 (POC, ADOPT) and BOD-336 (2026-10-10).
* Upstream Prime support may be contributed to ai-memory. A public PR needs the operator's OK.

## D2: ai-memory is a mandatory internal Verdict dependency

* It is not optional or selectable.
* New agent work is refused when memory is down, unauthorized or incompatible. Running work is never killed.
* Story: BOD-336.

## D3: hard enforcement of rules in code

* Rules are enforced in code, not by prompts.
* One versioned policy source feeds every enforcement point:
  * native Prime execution boundaries;
  * git hooks;
  * CI required checks;
  * Verdict gates.
* Each rule gets a bypass drill.
* Memory is evidence, never policy authority.
* Story: BOD-340.

## D4: every change is tracked in Linear before work starts

* All new stories, changes and fixes go in Linear before work starts, with:
  * acceptance criteria;
  * unit, integration and real-host e2e test criteria;
  * DoD proof commands.
* Decisions also live in tickets, not in plain files (operator 2026-10-13).

## D5: measure the outcome of every decision and feed it back

* Every decision Verdict makes is recorded with its options, choice, reason and expected result:
  * model and effort selection;
  * context pack;
  * routing;
  * delegation;
  * review;
  * planning.
* Its real outcome is joined later and feeds the next decision of the same kind.
* Goal to prove: with the right context, cheaper models match or beat frontier models. That means fewer tokens, lower cost per token, a higher success rate and better quality, shown with paired held-out tasks.
* Story: BOD-304.

## D6: code intelligence stays live and is linked to memory

* The code graph (codebase-memory) and symbol tools (Serena) stay the live source of code facts. They are not copied into memory.
* Memory stores knowledge about code, tagged with symbol and file entities and the commit SHA.
* Packs join live graph facts with cited memory for the same symbols.
* No language server runs per worker by default.
* Story: BOD-302.

## D7: ChatGPT history

* Manual Markdown export is impractical (4 months of conversations). The operator requested the full OpenAI data export.
* Import it offline, with provenance, idempotent and with coverage reporting.
* Tracked under BOD-339 / BOD-80.

## D8: Linear itself is ingested into memory

* Issue descriptions and comments become cited, read-only pages in ai-memory, updated incrementally.
* The ticket stays authoritative. Memory points back to the ticket id.

## D9: research once, workers reuse (queen / worker hive), and optimize intelligence through context

* The frontier orchestrator researches once and hands workers a structured, cited pack: settled facts, open questions, and the commands to run.
* Workers do not repeat research the pack already carries. Findings are written to ai-memory so later workers and sessions reuse them.
* Repeated research is measured per worker as a token share (target below 10%) and fed back into pack building.
* This is how cheaper models are made to match frontier models.
* Story: BOD-303.

## D10: context autopilot (operator 2026-10-13)

* Keep the context window small and relevant at every request: continuously trim what is no longer relevant and append what is, and never wait for a compaction.
* Evicted items stay retrievable by id.
* It must be measured to lose no quality on paired tasks.
* Story: BOD-333.

## D11: mine Ruflo, RuVector, agentic-flow and metaharness for ideas

* Use parallel Opus 5.5 researchers. These are ideas only, not a dependency switch.
* Output goes to work/mining/\*.md. Adopted ideas are folded into the stories they map to.

## D12: result of mining Ruflo, RuVector, agentic-flow and metaharness (2026-10-13)

* Nothing beats ai-memory or Verdict receipts. D1-D11 stand.
* Adopted ideas were added as AC to BOD-333, 303, 304, 306, 336, 340 and 302.
* None has relevance-based context trimming. RuVector's observation masking plus recall(id) is the best working base for BOD-333.
* Skip SONA, hive-mind and consensus: there is no measured task-level win, and RuVector's own ADRs gate SONA off. BOD-82 stays retired.
* Ideas only: agentic-flow has no LICENSE file; Ruflo and RuVector are MIT.

## D13: OmniRoute live evidence (proposed 2026-10-13, operator to confirm)

* OmniRoute 3.8.51 supplies the live facts:
  * quota windows (me/status);
  * cooldowns and breakers (resilience/connections);
  * configured-provider rankings with 24 h reliability and ELO (free-provider-rankings).
* Verdict keeps the decision, receipt and outcome feedback.
* Story: BOD-345.

## D14: OmniRoute 3.8.51 docs synthesis adopted (2026-10-13; D13 confirmed by the operator's go-ahead)

* Quota and cooldown inputs come only from me/status and resilience/connections. 28 of 42 accounts report not\_supported, and those mean UNKNOWN.
* Ranking order: outcome ledger, then model-latency-stats (24 h), then Verdict probe, then free-provider-rankings ELO (configured), then catalog. "warming" is not evidence.
* The census comes from provider-plugin-manifest. Hand-written alias families shrink to agy->antigravity and dva->devin-cli.
* Finish plan M1(a): the passive screen (model-latency-stats + rankings withUsage) replaces POST test-all. test-all runs only for zero-traffic routes.
* Delete the live\_routing\*.py and capacity\_\*.py modules (except capacity\_models), about 3,100-3,400 lines plus about 1,600 test lines, after a reachability check (BOD-345 AC8).
* auto/thrifty and auto/subscription are evaluation arms only, never the production router. Exploration stays in Verdict.
* Forbidden in Verdict jobs: GET /api/usage/analytics and /api/combos/auto. Both stall the gateway for 40-160 s.
* Every gateway call sends X-Correlation-Id `<run>:<node>:<attempt>` and x-omniroute-no-memory: true.
* Rejected: OmniRoute Evals for the paired proof, /api/memory, and the X-OmniRoute-Mode/Effort headers.
* Stories updated: BOD-345, 304, 300, 297, 314, 341, 336. New: BOD-347.

## D15: north star (operator 2026-10-14)

* Why: free tokens are a force multiplier. The operator cannot afford enough paid usage, so every task a free model does safely is extra work at no cost. The headline metric is accepted work per paid dollar (and per % of paid quota).
* Verdict's core is routing each prompt to the cheapest model proven capable for that task, whenever one is available, with eligibility from live OmniRoute evidence (BOD-345, BOD-300).
* Everything else (memory, context packs, outcome feedback) exists to serve this.
* New work must say how it moves this goal forward.
* Past failure: agents pitched other ideas, choked on eligibility checks, and after model switches undid the previous day's verified work, because there was no working memory.
* Rule: read decisions (BOD-342), failures (BOD-348) and ai-memory before changing prior work.

## D16: build order after the 2026-10-14 real-host checks

1. BOD-349: autodev dry-run plus one live free-model slice (worker running).
2. BOD-350: census daemon (auto-run, dead-provider filter, parallel tests, measured capabilities). This gives eligibility > 0.
3. BOD-345: OmniRoute evidence inputs plus code deletion.
4. BOD-351: animated setup plus live demo; then the M7 video.
   The memory track (BOD-339 canary, then the history import) runs in parallel.

## D17: one controller, one owner per ticket, cheap workers under each (operator 2026-10-14)

TICKET OWNER (operator 2026-10-14; the scalable shape: 1 controller, 1 owner per ticket, many cheap workers under each owner).
The controller (queen) assigns one ticket to one owner and verifies the owner's final result. It does no implementation.
The ticket owner is a mini-controller for exactly one Linear ticket. It runs on a mid model (agy/claude-opus-4-6-thinking, or cx when it has room), not Claude Max. It:

1. Decomposes the ask into the smallest pieces needed to finish it.
2. For each piece, attaches: the available tools and MCP servers; the industry best approach if it is a common problem (a small web or docs search); the docs for whatever is used (ref-\* memory projects, context7); code intelligence (codebase-memory symbols, callers, impact, similar existing code); the goal; and how to know it works (a test written first plus the real command).
3. Assigns each piece to the cheapest capable model, free first (the feature pipeline), and runs independent pieces in parallel.
4. Integrates: takes the sum of the parts and proves together they accomplish the ask, testing as far as reasonable, with no visible regressions.
5. Ships: opens the PR, gets a cross-family review (a cheap family first), merges when CI is green and the review approves, then checks for build and deploy problems on main (fresh clone, install, the real command).
6. Reports to the controller once, with the raw proof, then is done. The controller re-runs the proof and assigns the next ticket.
   Rules: one ticket per owner; owners never take a second ticket; workers never spawn owners; failures go to BOD-348; decisions go to BOD-342.
   This is also the product architecture Verdict must support at commercial scale (hundreds of agents in parallel): BOD-285 (harness-agnostic control plane) and BOD-303.

## D18 (PROVISIONAL, under the decision loop): controller + ticket owner + parallel vertical slices

* Options:
  * A: controller plus one owner per ticket, with parallel vertical slices on cheap/mid models and frontier verification (D17 plus the slicing rule).
  * B: one frontier model owns the whole ticket.
  * C: a hybrid chosen per ticket by the owner's estimate.
* Provisional choice: C (A when slices are independent, B otherwise).
* Expected result: lower cost per accepted result than B at similar or better wall time, with no quality loss.
* Research: 3 cheap researchers (vendor guidance, papers on cascades/routing, parallel coding agents in practice), then an Opus synthesis (work/research-slicing/SYNTHESIS.md).
* Measurement: the paired arms P vs F in BOD-304: wall time, cost per accepted result, review findings.
* Review/rating: after the first 3 tickets run each way. Result: keep, change or revert, recorded here; failures go to BOD-348.
* Status: open until rated.

## D18 UPDATE 2026-10-14: research result = ADAPT, not adopt (work/research-slicing/SYNTHESIS.md; 3 researchers plus an Opus synthesis that read the primary sources)

* Keep:
  * a strong planner/owner;
  * a clean-context frontier verifier, which sees only the diff and the code, not the author's trace;
  * cheap or free implementers executing specs that have a test written first.
* Writes stay single-threaded per ticket by default.
* Parallel writers ONLY when the slices share no files, the interfaces are frozen, and each slice has a test written first.
* Fall back to one frontier owner when any of these is true:
  * the work is coupled or exploratory;
  * the change is small;
  * cheap first-pass yield is under \~75%;
  * there is no free quota.
* Evidence:
  * multi-agent setups use \~4-15x tokens (Anthropic);
  * parallel writers make conflicting implicit decisions (Cognition);
  * a sequential strong-planner plus cheap-editor setup beat the strong model alone (Aider architect/editor);
  * no source measures mixed-tier PARALLEL coding, so the hypothesis is unproven.
* Source quality:
  * the hardnumbers benchmark is unreliable (impossible percentages, its results directory returns 404);
  * several numbers were mis-cited by the cheap researchers and corrected in the synthesis.
* Next measurement: 6-task paired smoke test, arm F (frontier solo) vs arm H (spec owner + cheap implementer + the same clean-context verifier).
  * Metric: verified, regression-free slices per paid dollar.
  * Pre-registered stop rule: H loses a task class if its yield is more than 15 points below F's or its wall time is more than 2x F's.
  * Grow to about 20 tasks before any rule goes into Verdict code.
* Status: D18 revised to the adapted form above, still PROVISIONAL until the paired run.

## D19: use the Anthropic Max API credit through OmniRoute (operator 2026-10-15, 'critical')

* The $100/month credit is live as anthropic/\* routes, with a local patch that drops the claude-code beta until OmniRoute 3.8.52.
* Use it for the controller and verification when the Max weekly quota is out. Implementation stays on cheap and free models.
* Re-test after any OmniRoute upgrade.

## D20: parallel tracks with Opus owners on the API credit (operator 2026-10-15)

Execution tracks (operator 2026-10-15): 3 parallel tracks, each owned by one agent on anthropic/claude-opus-5-5 (Max API credit), high effort (xhigh only for design or hard bugs), budget \~$15 per track, stop at budget. Delegation: a free model first; if none qualifies, the cheapest model that satisfies the need (ClinePass and Codex count as zero marginal cost when available); log every choice to the model-decision log.

* Track 1, context engine to workers: BOD-303 (CE-03, CE-04) + BOD-349 (AD-04).
* Track 2, Verdict picks the model and reselects: BOD-350 PR #839 rebase + BOD-271 live rules (AD-03, AD-06) + BOD-354 (stale key).
* Track 3, watcher + OCR MCP: BOD-358 + BOD-356 + BOD-357 triage (mostly delegated to free models).

Acceptance per track = the BOD-355 matrix rows passing on main, re-run by the parent. Measurement: cost per accepted result per track (D18).

## D21: the operator's PRIME GOAL (2026-10-15) is the acceptance target

* Criteria:
  1. Routing restored: 3 warm runs on merged main, each under 10 s and SELECTING a model; no stale-key fallback; the census runs unattended.
  2. A real autonomous existing-file Linear change, with dynamically chosen models, context packs, tests, independent review and merged-main verification.
  3. Native Prime memory finished: canary kept, safe activation, history import, cross-session recall, periodic checks.
  4. The learning loop closed from real attempts and outcomes.
* Models while cc and cx are exhausted: cp/cline-pass/glm-5.3 as controller, cp/cline-pass/kimi-k3 for complex ownership and review, free workers first.
* ClinePass is finite capacity. Other API credits need a verified balance and approved limits.
* Delivered = parent-replayed exact-SHA production acceptance only.

## D22: model policy (operator 2026-10-15, effective immediately)

* cc/\* and cx/\* are excluded from launches, planner fallbacks and reviews until live quota evidence and approval restore them.
* Root controller: cp/cline-pass/glm-5.3. Hard architecture, ticket ownership and cross-family review: cp/cline-pass/kimi-k3. Other measured candidates: qwen3.8-max, minimax-m3, deepseek-v4.1-flash. GLM-5.3 Flash (empty reply) and DeepSeek V4 Pro (timeout) are not qualified.
* Workers: eligible free models first, then subscriptions, with task-grade evidence. Separate account pools; no account cycling to dodge limits; ClinePass is finite.
* anthropic/\* credit is separate and limited to the confirmed balance and the BOD-342 allocation; it is not an automatic fallback.
* Every role gets an exact, fresh, eligible model; record chosen vs executed identity.
