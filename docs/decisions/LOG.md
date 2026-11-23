# Decision Log

Source: [BOD-342 — Operator decision log](https://linear.app/bodanglin/issue/BOD-342/decisions-operator-decision-log-2026-10-13-memory-system-enforcement)
(Linear, Bodanglin workspace, exported 2026-11-20).

This file contains decisions D1–D30 and the D22 amendment, migrated from the BOD-342
comment thread on 2026-11-20. Entries that pre-dated this migration are from the Linear
source with private commercial details removed. D30 was recorded as D23 in the Linear
thread; it is renumbered here because D23–D29 already existed (see the entries below).

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

* Quota and cooldown inputs come only from me/status and resilience/connections. Most accounts report not\_supported, and those mean UNKNOWN.
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

## D19: use the Anthropic API credit through OmniRoute (operator 2026-10-15, 'critical')

* A limited paid API credit is live as anthropic/\* routes, with a local patch that drops the claude-code beta until OmniRoute 3.8.52.
* Use it for the controller and verification when the subscription quota is exhausted. Implementation stays on cheap and free models.
* Re-test after any OmniRoute upgrade.

## D20: parallel tracks with Opus owners on the API credit (operator 2026-10-15)

Execution tracks (operator 2026-10-15): 3 parallel tracks, each owned by one agent on anthropic/claude-opus-5-5 (a limited paid API credit), high effort (xhigh only for design or hard bugs), capped per-track budget, stop at cap. Delegation: a free model first; if none qualifies, the cheapest model that satisfies the need (finite subscription pools count as zero marginal cost when available); log every choice to the model-decision log.

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
* Models while cc and cx are exhausted: \<subscription\>/glm-5.3 as controller, \<subscription\>/kimi-k3 for complex ownership and review, free workers first.
* This subscription pool is finite capacity. Other paid credits need a verified balance and approved limits.
* Delivered = parent-replayed exact-SHA production acceptance only.

## D22: model policy (operator 2026-10-15, effective immediately)

* cc/\* and cx/\* are excluded from launches, planner fallbacks and reviews until live quota evidence and approval restore them.
* Root controller: \<subscription\>/glm-5.3. Hard architecture, ticket ownership and cross-family review: \<subscription\>/kimi-k3. Other measured candidates: qwen3.8-max, minimax-m3, deepseek-v4.1-flash. GLM-5.3 Flash (empty reply) and DeepSeek V4 Pro (timeout) are not qualified.
* Workers: eligible free models first, then subscriptions, with task-grade evidence. Separate account pools; no account cycling to dodge limits; this subscription pool is finite.
* The anthropic/\* route uses a limited paid credit, separate from subscription pools; it is not an automatic fallback.
* Every role gets an exact, fresh, eligible model; record chosen vs executed identity.

## D22 lifted (operator 2026-11-17)

This replaces D22 (2026-10-15: "cc/\* and cx/\* are excluded from launches, planner
fallbacks and reviews until live quota evidence and approval restore them").

Decision: cc/\* (Claude subscription) and cx/\* (Codex subscription) routes may be used
again for launches, planner fallbacks and reviews, through the normal admission ladder.
Pay-per-token routes stay excluded.

Live usage evidence at the time of the decision (OmniRoute call logs,
2026-11-17 12:21–15:29Z): claude-opus-5-5 59/59 ok, claude-sonnet-5-5 4/5 ok;
claude-fable-5-2 hit "credentials cooling down" twice the same day. Known cost:
cc/\* models share one subscription with the orchestrator and escalation agents, so
worker fallback draws on that quota.

Code that enforced D22 and is being removed: `verdict/decomposer.py` (refuses any
cc/\* planner), `verdict/autodev_run.py` `_PLANNER_EXCLUDED_POOLS = {"claude", "codex"}`,
and the tests that pin both. Tracked with BOD-406.

## D23: worker/reviewer eligibility evidence (BOD-387, 2026-10-15)

Options: (a) loosen worker/reviewer to accept unmeasured headroom; (b) treat absent
provider health as admissible; (c) use OmniRoute /api/resilience/connections as positive
health evidence plus allow unmeasured headroom only with a fresh census passport.
Chosen: (c), because it uses live gateway evidence plus a real proven call, not the
absence of a block (same principle as PR #840). Expected: worker+reviewer select live
with cross-family independence and no paid fallback. Measure: select-only on this host
plus the census passports used. Reversible.

## D24: diff-format repair for owned files ≤ 300 lines (BOD-391, 2026-10-15)

A free/cheap executor kept returning the same miscounted unified diff even after a repair
turn with the numbered file (codestral, 2/2 + 3/3 live). Options: (a) fuzzy blank-line
apply (rejected: could misapply); (b) search/replace edit format everywhere (larger
redesign); (c) repair turn asks for the FULL new content of the owned file when it is
small (≤300 lines); Verdict computes the diff itself (difflib) and the normal
git apply --check, owned-files bound and verification command still gate it.
Chosen: (c), plus sending OmniRoute's cache-bypass on executor/repair/retry calls so
retries are real samples (earlier runs were cache HITs). Expected: the scratch-repo
AD-04 command verifies 3/3. Measure: 3 live runs with cache MISS. Reversible.

## D25: patch repair for owned files > 300 lines (BOD-392, 2026-10-16)

Evidence: a 3-line rename in the 407-line `credentials_registry.py`; the diff-format
repair reply was truncated at max\_tokens=4096. Options: (a) raise max\_tokens (does not
fix a model that echoes the file); (b) raise the full-file cap (costly, and risks
truncating large files); (c) for files over 300 lines, the repair turn asks for
SEARCH/REPLACE edit blocks: an exact old snippet that must match exactly once in the
owned file, plus its replacement. Verdict applies the blocks, builds the diff itself,
and runs the same git apply --check, owned-files and verification gates.
Chosen: (c). It is bounded and exact-match (no fuzzy apply), and the reply stays small.
Expected: BOD-392 verifies. Measure: the real BOD-392 command on main. Reversible.

## D26: delete live\_routing\_run/live\_routing\_usage (BOD-384, 2026-10-16)

Delete `live_routing_run` and `live_routing_usage` (no production caller; duplicates
OmniRoute quota feed). Reversible by revert. Operator rule recorded: never park on
approval; after ~30 min decide on a branch, worst case revert.

## D27: three orphan modules from the Researcher pack (BOD-397, 2026-10-16)

(1) `memory_providers/ai_memory_experimental.py`: DELETE. The native Rust ai-memory
adapter (BOD-339) replaced it and has been live since 04:25Z. Keep
`docs/proof/ai-memory-poc-2026-09-28/REPORT.md` as history and add a "superseded by
BOD-339" note. (2) `workflows/autodev.py`: DELETE, in a separate follow-up PR. The real
core loop is `verdict/autodev_run.py` (proven on main by BOD-392).
`docs/architecture/AUTONOMOUS_DEV_LOOP.md` must be updated in that same PR to point to
`autodev_run.py`. (3) `execution_hosts`: KEEP as ALLOWLIST, reason "documented contract
(`docs/guides/execution-host-contract.md`); wire or retire under BOD-316". Reversible.

> Path note (2026-11-20): the decision text above is kept as written. Since then
> `AUTONOMOUS_DEV_LOOP.md` moved to [`docs/archive/AUTONOMOUS_DEV_LOOP.md`](../archive/AUTONOMOUS_DEV_LOOP.md),
> and `docs/guides/execution-host-contract.md` was removed in a later cleanup; it
> survives only in git history.

## D28: merge flow (2026-10-16)

The core loop is proven on main (BOD-392), so the 2026-10-09 release-train preference
applies again; it was paused by the 10-14 no-churn rule until the core loop worked.
Rule: each story keeps its own branch and its own cross-family review. When 2 or more
reviewed stories are ready, the controller merges them into `integration/train-<n>`,
runs CI once, and merges one PR to main with `--match-head-commit`. A single urgent fix
(main red, security, or a blocker for a running proof) may still go straight to main.
Measure: CI runs per merged story. Reversible.

**D28 amendment (2026-10-16):** Train 1 (#878) merged after suite + reachability +
openspec + ruff + vgate + CI, but WITHOUT the whole-repo release review required by the
operator preference of 2026-10-11 (behavior evidence on the integration head before
merge). Its real command was run only AFTER merge on main 93b42f1b (passed: new test
file under a missing dir, 4 attempt records). From train 2 on, before merging:
(1) a real `verdict autodev --allow-live` on a scratch repo, run FROM THE TRAIN HEAD;
(2) `verdict doctor` from the train head;
(3) a whole-repo review by a reviewer from a different family than every story author,
covering product behaviour, not per-diff.
Only then squash-merge with --match-head-commit and check the tree.

## D29: doctor key resolver contract (BOD-411, 2026-10-16)

Doctor reads the OmniRoute key through the one resolver (operator prevention rule P1,
failure-review-and-prevention as of 2026-10-13).
`tests/test_cli_inprocess.py::test_omniroute_token_does_not_inspect_home_or_private_paths`
encodes the pre-credentials-store contract from BOD-238 (#623), so it is replaced by a
NARROWER test. The token lookup may read only the documented owner-only key file
(`~/.config/verdict/omniroute-api-key`, 0600) and the credential store, in the
documented precedence. It must not read any other private path, and a key file readable
by group or others is refused. This counts as a contract change recorded here, not a
weakened test. Reversible.

## D30: exception to the "never touch operator or ref-\*" rule (BOD-339 Part C, accepted 2026-11-11)

(Recorded as D23 in the Linear thread; renumbered here because D23–D29 already existed.)

* **What happened:**
  * Part C moved the closed sessions of `dev`, `mrnicholasbcarter-code-verdict-core`,
    `nick` and `.hermes` into `default/verdict`, one at a time with
    `ai-memory move-session <id> --confirm` (no `--force`).
  * `move-session` re-stamps **every** observation of a session, including rows filed in
    other projects. `lifecycle-ops.md:760` says so: "every `observations` row of the
    session (including any that landed in another …)".
  * One session carried observations across 11 projects; two of those rows were in the
    `operator` project. All rows now sit in `verdict`.
* **The 2 `operator` rows:** both were 2026-11-06 observations of one Claude Code read
  of `~/.agents/operator/definition-of-done.md`. Project: `operator` → `verdict`.
  Bodies unchanged (same hash).
* **What did not change:** `operator` pages (445 versions, all body hashes identical).
  `operator` sessions and handoffs: 0 → 0. `ref-omniroute` page versions: unchanged.
* **Why no undo:** no ai-memory command moves single observations. A reverse
  `move-session` would move all rows from the session, not just the 2 `operator` ones.
* **Decision:** the operator accepts the moved observations, Part C stands, nothing is
  restored.
* **Rule going forward:** before any bulk `move-session`, count each session's
  observations by project and skip any session that touches `operator` or `ref-*`.
  The process gap is logged as BOD-348 N50.

## D32: a route is paid only from facts (operator, 2026-11-21; ADR-049, accepted)

(D31 is reserved by PR #1242, story U1, which is parked.)

* **Decision:** a route is PAID only from declared or observed facts, never from its name. A route
  with no cost facts is treated as free; its class can still read `UNKNOWN` in receipts. The cost
  order is per-operator policy, and an unknown-cost mode (`assume_free`, `probe_only`, `exclude`)
  decides how `UNKNOWN` routes run. This operator: `assume_free`. Default for other operators:
  `probe_only`. Pay-per-token routes are on by default (ranked by the cost order). Each `UNKNOWN`
  route gets a per-route daily call cap until its first fact; CP1 sets the value.
* **Supersedes:** the 2026-11-19 rule "unknown is never free".
* **Record:** [ADR-049](../adr/ADR-049-cost-policy-and-unknown-handling.md). Implemented by CP2
  (#1245) and CP1 (#1244); main's behaviour is unchanged until CP1 lands.

## D33: Hindsight is the memory of record (operator, 2026-11-21; ADR-050, accepted)

* **Decision:** Hindsight (self-hosted at `http://127.0.0.1:8888`, bank `shared`) replaces
  ai-memory as the cross-session, cross-harness memory store. MEM1 (#1253) removes the
  ai-memory ref-docs sync chain (the only ai-memory dependency in doctor/setup paths).
  MEM2 (#1254) removes the Hermes per-turn recall hook and AI_MEMORY_AUTH_TOKEN.
* **Supersedes:** D1 (ai-memory as memory of record).
* **Record:** [ADR-050](../adr/ADR-050-hindsight-memory-of-record.md).

## D34: ai-memory is fully decommissioned (2026-11-21; MEM2 #1254, accepted)

* **Decision:** The Hermes memory harness (`verdict/harness_hermes_memory.py`), its
  bundled plugin (`verdict/hermes_plugin/`), the `AI_MEMORY_AUTH_TOKEN` credential, the
  setup-wizard ai-memory offer, and the recall-eval script are removed. The ai-memory
  server can be stopped. `verdict harness hermes enable/disable` no longer installs
  memory hooks. No ai-memory dependency remains in `verdict/`.
* **Supersedes:** D1 and D2 (ai-memory as mandatory dependency; now fully removed).
* **Record:** `openspec/changes/remove-ai-memory-integrations/`, issue #1254.

## D35: planners come from the frontier band of live ratings (operator, 2026-11-23; ADR-051, accepted)

* **Decision:** a planner (autodev, orchestrate, certification preflight) must be in the
  frontier band: the top 20% of rated models in the route inventory by fresh
  `aa_intelligence`, from the rating source's newest fetch, with `:suffix` variants counted
  once and ties at the cutoff included. If no admissible route is in it, use the top 30%
  and record the downgrade; with neither, fail closed (`no_frontier_or_fallback_planner`).
  Within a band the strongest admissible non-metered route plans (UNKNOWN counts as
  non-metered); equal scores go to the cheaper class, then `aa_coding`; METERED only when
  nothing else in the band is admissible and within the CP1 spend limit. No family guard;
  doctor warns when the frontier band is empty. Verdict's own planner receipts are a later ADR.
  Autodev keeps its two-family planner list; the other-family failover may come from the
  fallback band (recorded); fewer than two families across both bands fails closed.
* **Process:** agreed over five rounds of researched options; this is the standing pattern
  for decisions (research, ask each point, record, then accept).
* **Supersedes:** `PLANNER_MAX_CAPABILITY_TIER` and the id classifier as planner gates.
* **Record:** [ADR-051](../adr/ADR-051-planner-frontier-from-ratings.md),
  `openspec/changes/frontier-planner-ratings/`. Not implemented yet.

## D37: Verdict builds a GitHub issue through to a ready PR (operator, 2026-11-23; ADR-052, accepted)

* **Decision:**
  * The operator names an issue with `verdict build-issue N`. The issue must be labelled type/story, not be operator-only, have been opened by the operator, have no open dependencies, and have Goal and Acceptance check sections.
  * Verdict drafts an OpenSpec change as a design PR. Building starts only after the operator approves it.
  * Verdict pushes through vgate to `verdict/issue-N-<slug>` and opens a draft PR. The PR is marked ready only after a cross-family review and green CI. If the in-run reviewer was from the same family, an extra OpenCodeReview from another family runs first; if none is available, the PR stays a draft.
  * When CI goes red, a bounded fix loop gets 2 tries.
  * Verdict uses the existing gh login.
  * The operator merges.
  * After the merge, Verdict reinstalls, runs the acceptance check live and drafts the DoD file.
  * Build order: deliver, then build-issue, then spec drafting, then the CI loop (pr-watch moves to GitHub as part of it), then a trial on #1214.
  * Verdict builds the pipeline itself, through orchestrate.
* **Process:** agreed over three rounds of researched options.
* **Record:** [ADR-052](../adr/ADR-052-github-issue-to-ready-pr.md), `openspec/changes/verdict-deliver/`. Not implemented yet.

## D38: OpenCode becomes the default worker executor; autodev adds it as the model-tier coding rung (operator, 2026-11-23; ADR-053, accepted)

* **Decision:** OpenCode (v2.3.2) becomes the DEFAULT worker executor for `verdict orchestrate`
  as soon as the adapter merges. Workers' model calls go through `verdict serve` (:8000), never
  direct to OmniRoute; the relay pins the chosen route and records the attempt. OpenCode is added
  as the coding executor for model-tier units in `verdict autodev`; PatchExecutor is kept for
  mechanical units. Workers run with per-attempt isolated config (`OPENCODE_CONFIG` pointing at a
  temp directory, `--standalone`) and may edit only inside their node worktree; shell commands are
  NOT restricted to an allow-list (operator's explicit choice). Verdict enforces owned files
  afterwards and runs node verification and repo gates itself. OpenCode is pinned at v2.3.2;
  `verdict doctor` warns on version mismatch; any upgrade must pass the adapter's live check.
  Existing bounds apply (900 s timeout, idle-output stall kill, 4-attempt-per-node budget); no
  new token cap. Build order: adapter first, then #1269 (deliver), then #1276 and #1274.
* **Process:** agreed over two rounds of researched options.
* **Amends:** ADR-041 default worker executor.
* **Record:** [ADR-053](../adr/ADR-053-opencode-worker-adapter.md),
  `openspec/changes/opencode-worker-adapter/`. Not implemented yet.
