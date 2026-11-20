# Negative Results Ledger

Source: [BOD-348 — Negative results ledger](https://linear.app/bodanglin/issue/BOD-348/decisions-negative-results-ledger-tried-measured-did-not-work-kept)
(Linear, Bodanglin workspace, exported 2026-11-20).

This file contains N01–N52, imported verbatim on 2026-11-20, plus N53 (sanitized).
Full incident details for the 2026-11-11 token-exposure event are operator-private and
remain in the private Linear archive; this file contains only the sanitized N53 summary.

---

Things we tried or checked that did NOT work, or that we rejected with evidence. This ticket is the record; it is kept current and mirrored into ai-memory (project operator, tag negative-result).

## Rules

* Before trying an approach, search this list (and ai-memory, tag negative-result). If it is here, do not repeat it unless its "retest when" condition is now true. A retest is labelled as one, with a hypothesis.
* Every failed or rejected attempt is added here the same day, with a number, the evidence, the conditions it was true under, and what would justify a retest. Log failures as carefully as wins.
* Research briefs ask for "what did not work" as well as top ideas.

## Ledger

| \# | source | tried | result | evidence | conditions | retest when |
| -- | -- | -- | -- | -- | -- | -- |
| N01 | Our tests 2026-10-13 | OmniRoute auto/thrifty as the production router | One probe: served a free NVIDIA model; the first subscription target was at position 183 of 3,372; rungBudgetUsd is not enforced in 3.8.51 (docs), so there is no spend cap | BOD-345 comment; work/omniroute-3851/thrifty-trace.json | OmniRoute 3.8.51, one probe | A release fixes the ladder/budget; then rerun 10 probes |
| N02 | Our tests 2026-10-13 | Local embeddings (all-MiniLM-L6-v2) for operator-rule recall | hit@5 fell 0.60 (FTS) -> 0.50 with embeddings; FTS-only with the compound query reached 0.90 | work/memory-eval/miss-analysis.md steps 3-15 | ai-memory 2.6.6, 40-question golden set | A different embedding model or reranker; rerun the golden eval |
| N03 | Our tests 2026-10-13 | OR-of-all-words recall query | hit@5 0.325 | miss-analysis.md | same | \* |
| N04 | Our tests 2026-10-13 | One history page per message | 3/7 vs 6/7 for 4 KB groups (weak evidence: that set was only partly written) | work/memory-eval/history-format/evalonly-\* | S2 questions only | If 4 KB groups underperform after the bulk import |
| N05 | Our tests 2026-10-13 | Joining OmniRoute routing/decisions/{id} to receipts | 404 for the explain requestId, the correlation id and 5/5 call-log ids. Use the x-omniroute-\* response headers instead | BOD-345 AC4 note | 3.8.51 | A new OmniRoute version |
| N06 | Our tests 2026-10-13 | GET /api/usage/analytics and /api/combos/auto in Verdict jobs | 12.5 s and 167 s; they stalled other gateway GETs for 40-160 s | work/omniroute-docs/\_verification.json | 3.8.51 | \* |
| N07 | Our tests 2026-10-13 | OmniRoute Evals for the paired proof | exact/contains/regex rubrics only; 18% pass on stale defaults | SYNTHESIS.md | 3.8.51 | \* |
| N08 | Our tests 2026-10-13 | OmniRoute /api/memory as Verdict memory | A separate backend that would duplicate ai-memory (D1) | SYNTHESIS.md | \* | \* |
| N09 | Our tests 2026-10-13 | X-OmniRoute-Mode/-Effort headers to steer exact routes | They steer auto/\* only | SYNTHESIS.md | 3.8.51 | \* |
| N10 | Our tests 2026-10-13 | X-OmniRoute-Session-Id as a cost key | Overridden by the gateway's conversationId | SYNTHESIS.md (chatCore.ts:1133) | 3.8.51 | \* |
| N11 | Our tests 2026-10-13 | POST /api/models/test-all as the eligibility screen | Chat only, no tool proof, expensive; passive 24 h stats replace it for routes with traffic | D14 | \* | \* |
| N12 | Our incidents 2026-10-13 | Serena on the Rust project | rust-analyzer plus a full cargo check (\~350 MB+) helped push OmniRoute into the OOM kill at 20:52 | memory omniroute-oom-policy | 8-16 GB host | \* |
| N13 | Our incidents 2026-10-13 | Long-thinking Claude through OmniRoute with the default 80 s stream readiness | 33 of 50 calls were killed with a 502 'content stall'. Fixed with STREAM_READINESS_TIMEOUT_MS=300000 | memory omniroute-content-stall-80s | 3.8.51 default | Keep the env override after every upgrade |
| N14 | Our tests 2026-10-13 | Prime TypeScript extension for ai-memory | Prime 0.10.0 is a Rust build: --extension is 'Unknown option', so the TS file was inert (an earlier 'working' claim was retracted) | BOD-339 | Prime 0.10.0 | A Prime release with TS extensions |
| N15 | Our tests 2026-10-13 | Engram as the memory backend | The agent must call mem\_save; FTS only | decision-20261013-memory-ai-memory / BOD-342 D1 | \* | \* |
| N16 | Our tests 2026-10-13 | ai-memory default session capture as history | It keeps only 80-character titles, so full-text pages are needed | BOD-339 | ai-memory 2.6.6 | \* |
| N17 | Mining 2026-10-13 | A live frontier advisor coaching a cheap worker | 0 extra resolves at 5.4x cost (metaharness ADR-226); a frontier model in a read-only slot gave no gain at 5.4-5.8x (RuVector) | work/mining/metaharness.md, ruvector.md | small n | Our own paired test (BOD-303) |
| N18 | Mining 2026-10-13 | SONA-style self-learning loops | No measured task-level win; RuVector's own ADR-276/278 gate it off; metaharness ADR-234 'unproven' | work/mining/\*.md | \* | \* |
| N19 | Mining 2026-10-13 | Vector/GraphRAG memory for cheap-model lift | The ablation was null (metaharness ADR-201); RVF query degraded | metaharness.md | \* | \* |
| N20 | Mining 2026-10-13 | Hive-mind / consensus / swarm coordination | No task-level win shown | ruflo-adrs.md | \* | \* |
| N21 | Operator history + mining 2026-10-13 | Ruflo 'Infinite Context' (Compaction-to-Memory Bridge: PreCompact hook, embed turns, re-inject via additionalContext) to avoid compaction | The operator saw it work: sessions never compacted. Then it stopped working, and about 3 months of trying to fix it failed. The current code (mined 2026-10-13) does not trim the live window and does not prevent compaction. It patches Claude Code's minified cli.js (ADR-051), and its PreCompact 'exit 2 blocks compaction' is documented but not implemented. Likely cause, NOT yet proven: a vendor update broke a patch or hook it depended on. | operator 2026-10-13; work/mining/ruflo-adrs.md, agentic-flow.md; sources: ruflo issue #1273, ruvnet gist 29f8fa68, USERGUIDE, n4s5ti/claude-flow, Koroqe/claude-code-sdlc; history dig running | Claude Code + Ruflo, 2026 | Never again build context control on patching or hooking a vendor CLI we do not own. Prime owns its message list natively (BOD-333 / BOD-339 TransformContextFn) |
| N22 | Mining 2026-10-13 | Learning from weak signals ('turn completed', 'tool ok', a 'complete' substring judge, 'user kept working') | Ruflo's own ADR-449 rejects these signals; agentic-flow judge.ts | ruflo-adrs.md, agentic-flow.md | \* | \* |
| N23 | Mining 2026-10-13 | Trusting headline numbers (HNSW 150-12,500x, Flash Attention, +55% SONA, ReasoningBank 0->100%, memory-tier savings) | 1.48x measured; Math.random(); external or scripted; hard-coded 15% per tier | work/mining/\*.md | \* | \* |
| N24 | Mining 2026-10-13 | Benchmarks run with the gold tests in the loop (oracle-on) | 68.3% SWE-bench used FAIL_TO_PASS tests while solving; 55.6%/51.3% are the fair numbers | metaharness.md ADR-175 | \* | \* |
| N25 | Mining 2026-10-13 | Evaluating a pack or ranking without a same-budget control | Window size alone looked like ranking quality (metaharness ADR-111/113) | metaharness.md | \* | \* |
| N26 | Mining 2026-10-13 | Automatic memory consolidation as a default | RuVector ADR-276 argues against it; measured, gated injection instead | ruvector.md | \* | \* |
| N27 | Mining 2026-10-13 | Integrations built on decompiled Claude Code (RuVector ADR-139) | Licence and terms-of-service risk | ruvector.md | \* | \* |
| N28 | Review 2026-10-14 | Ruflo ADR-051 'hash embeddings' (Math.sin of a string hash) as semantic recall | Deterministic noise, no meaning; relevance retrieval was never built (a 'future enhancement') | work/mining/infinite-context-review.md, gist 29f8fa68 | ADR-051 | \* |
| N29 | Review 2026-10-14 | Restoring the most RECENT chunks after compaction (ADR-051) instead of the most relevant | 4,000 chars of recent turns from the same session only; older relevant facts are lost | same | ADR-051 | \* |
| N30 | Review 2026-10-14 | Treating ruflo issue #1273 numbers (95-98%, 30 min -> 3 h) as Ruflo results | They are proposal targets copied from claude-context-mode; the issue is open and unimplemented | ic-1273.json | \* | \* |

## Still to add

Failures already recorded in older sessions and global memories (for example free routes that pass a probe but fail mid-session, free reviewers hallucinating, and authors falsely claiming success). A session miner will fold them in.

Related: BOD-342 (decisions), BOD-304 (outcome feedback), BOD-303, BOD-333, BOD-339, BOD-345.

## Ruflo 'never compacts' history (2026-10-14, work/mining/ruflo-compaction-history.md)

Proven root cause: Ruflo never shipped a working way to stop Claude Code compaction. The PreCompact exit-2 block lived 3 hours on 2026-02-10. The cli.js patch matched only Claude Code 2.0.76, and Claude Code is a native binary since 2.1.113. `ruflo init` never wired the hook. Likely (not proven) reason the operator saw 'never compacts': before Claude Code 2.1.223 (2026-08-05), unrecognized gateway model ids did not auto-compact.

| \# | who / where | date | tried | why it failed |
| -- | -- | -- | -- | -- |
| N-R1 | Ruflo `2cd884ed`…`c18c7711` | 2026-02-10 | PreCompact `process.exit(2)` on auto trigger | Claude Code ≤2.0.76 ran PreCompact outside the REPL hook path and ignored exit 2 (ADR-051 decompile). Removed in `68059650`. When Claude Code added blocking in 2.1.105, a block without a trim leads to "Prompt is too long" (Koroqe compaction-probe §4; CC 2.1.89/2.1.229 errors) |
| N-R2 | Ruflo settings `914c4a16`, `b91032d3` | 2026-02-10 | `DISABLE_COMPACT=1` | Turns compaction off entirely, so the window overflows unless the user runs `/clear`. Reverted within minutes |
| N-R3 | Ruflo settings `cb9d71e4`→`0ea28204` | 2026-02-10 | `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE=50/30` so micro-compact starts earlier | The same variable lowers the full-compaction threshold, so compaction fires *earlier*. Removed in `52b08dba` |
| N-R4 | Ruflo `24ae61ad` `patch-aggressive-prune.mjs` | 2026-02-10 | Monkey-patch minified `cli.js` to cut old text blocks | Matches 2.0.76/0.1.76 only. Patches the project SDK copy, not the user's CLI. Never run by hooks/init. Impossible after 2.1.113 (native binary) |
| N-R5 | Ruflo context autopilot (`runAutopilot`) | 2026-02-10 → now | 70%/85% zones, "keep concise" / "start a new session with /clear" | Advice only. It deletes archive rows, not context. Restore is ≤4000 chars of recent chunks |
| N-V1 | Operator, kalshi-trader `dd77177e`/`97326179` | 2026-03-29 / 04-14 | Installed Ruflo V3 helpers, including the hook and the cli.js patch | The copied hook is the non-blocking version. No hooks are wired in the committed settings. The patch cannot match 2.1.x |
| N-V2 | verdict-core-memory `6e48b49` (ADR-008) | 2026-08-02 | Python port `ContextAutopilot` + `verdict hook dispatch --event PreCompact` returning exit 2 on auto | Never wired: `configure_memory_bridge` writes no PreCompact hook. Its tests were skipped two days later (`f86da61`, #267). Still no trim path (a block would overflow). The repo is archived |
| N-V3 | verdict-core `aa6586a3` `memory_bridge.MemoryHookController` | 2026-07-28 | Recall on prompt, ingest on session end, `on_compaction` receipt | Retrieval only. It cannot shrink or stop compaction. Ruflo/RuVector parts removed in BOD-17 (`936ee683`) and `dbe775d9` |
| N-V4 | Ruflo transport via Verdict (`ruflo_adapter`/`ruflo_transport`) | 2026-07-23 → 08-03 | Run memory/autopilot through Ruflo | 99 evidence runs recorded "Connection to Ruflo failed". Deleted in BOD-17 |

## Ruflo latest review (2026-10-14, main @55e2c90, work/mining/ruflo-latest-review.md)

balanced.ts is a stub dressed as learning: its gradients are never read, LoRA B stays 0, and the EWC penalty is always 0. ruvector-training.ts is a heuristic, manual CLI only, trained on template sentences, and every run is labelled 'success'. The learning signal is a proxy (step reward and shortness) fed with undefined arguments.

| \# | Mining 2026-10-14 | tried | result | evidence | conditions | retest when |
| -- | -- | -- | -- | -- | -- | -- |
| N31 | Mining 2026-10-14 | Ruflo SONA balanced mode as online learning (reward-weighted state accumulation + rank-4 LoRA) | Accumulated "gradients" are never read. LoRA B stays 0, so the output equals the input. "improvement" = mean input label − 0.5. Learning starts only at 2,400 in-memory trajectories, which short-lived hook processes never reach. "+25% quality" is a header comment. | work/mining/ruflo-latest-review.md §c1; balanced.ts:115-205, sona-manager.ts:531-542,719-740 @55e2c90 | ruflo 3.56.9 | A paired task-level lift with the learned state actually read at runtime |
| N32 | Mining 2026-10-14 | EWC++ "catastrophic forgetting" protection in SONAManager | The Fisher map is created empty and never filled, so the penalty is always 0. The audit calls the other EWC Fisher a heuristic proxy. | sona-manager.ts:200-201,566-580; docs/reviews/intelligence-system-audit-2026-05-29.md | same | \* |
| N33 | Mining 2026-10-14 | Ruflo `neural train` / ruvector-training (MicroLoRA + InfoNCE / ruvllm pipeline) | Trains on repeated template sentences. Positives and negatives come from list position (negatives include copies of the anchor). The "trajectory improvement" is wall-clock time against a 10 ms constant. Every run is labelled 'success' and saved as 0.8-confidence patterns. The checkpoint is auto-loaded into a singleton read only for getStats(). | §c2; neural.ts:147-330, ruvector-training.ts:151-271, intelligence.ts:1163-1177, lora-adapter.ts:786-793, hooks-tools.ts:3111 | same | \* |
| N34 | Mining 2026-10-14 | Hook confidence feedback as the learning signal (post-task on SubagentStop, "user continued working" boost) | SubagentStop carries no tool\_response, so it always reports success (+0.05). The implicit +0.03 per unchanged follow-on prompt is also positive. The learned confidence is **not part of the recall score** (0.6·Jaccard + 0.4·PageRank), so learning cannot change what is recalled. | hook-handler.cjs:562-574,1005-1015; intelligence.cjs:742-801,841-875; settings-generator.ts:389-399 | same | \* (lesson: every learned score needs a test that flipping the outcome changes the next decision) |
| N35 | Mining 2026-10-14 | Testing a learning bridge only with a mocked collaborator | LearningBridge passes one object where NeuralLearningSystem.recordStep takes positional arguments, so reward and embedding are undefined. vi.fn() mocks hide it. | learning-bridge.ts:190,230 vs neural/src/index.ts:332-339; learning-bridge.test.ts | same | \* (lesson for BOD-304: one integration test with the real collaborator per learning edge) |
| N36 | Mining 2026-10-14 | Embedding search where the model can silently fall back | When the native `sharp` build fails, mock embeddings are still labelled `Xenova/all-MiniLM-L6-v2`. Synonyms scored −0.988, unrelated text +0.775. The operator cannot tell the difference. | intelligence-system-audit-2026-05-29.md (ONNX row, bug #3) | ruflo 3.10.x host | Before any N02 retest: assert the embedding backend identity and a synonym-similarity sanity check |
| N37 | Mining 2026-10-14 | Self-supervised self-retrieval ("find a doc from its own body") as the fitness for promoting harness/memory changes (Ruflo ADR-176 "DEMONSTRATED") | The promotions are real and replayable, but the fitness is a proxy that a retriever can game. ADR-176 itself scopes the claim to self-retrieval and says ruflo has no held-out task benchmark. | v3/docs/adr/ADR-176-proven-self-benchmarking-harness-loop.md ("Honest scope", "Honest gaps") | \* | A held-out, human- or test-labelled task corpus (BOD-306) |
| N38 | Mining 2026-10-14 | Hiding tool/agent listing entries to save tokens without a discovery path | A hidden type that the prompt does not name is refused at dispatch (1/1). The listing is fixed at session start and does not refresh. The 20-task replay for "zero failed dispatches" was not run. | v3/docs/validation/ruflo-mods-trim-measure-2026-10.md §2-3 | haiku, 69 types, headless | Our paired test (idea 1) counts failed dispatches |

## ruflo-intelligence plugin check (2026-10-14, work/mining/ruflo-intelligence-plugin.md)

| \# | Mining 2026-10-14 | tried | result | evidence | conditions | retest when |
| -- | -- | -- | -- | -- | -- | -- |
| N39 | Mining 2026-10-14 | Trusting `hooks_intelligence`'s embedded component notes (Flash Attention 2.49x-7.47x, HNSW 150x-12,500x, LoRA 128x compression) as evidence the pipeline helps | These are the same project's own fabricated numbers (measured \~1.48x peak per its audit, Flash speedups fabricated with `Math.random()`), now echoed verbatim inside a live MCP tool's JSON response (`implementationStatus.notImplemented: []`) rather than only in README prose. The tool call itself does not validate or re-measure them. | `hooks-tools.ts:3050-3090`; docs/reviews/intelligence-system-audit-2026-05-29.md (cited in ruflo-adrs.md) | ruflo 3.56.x / 55e2c90 | If the project ever replaces these note strings with measured values |
| N40 | Mining 2026-10-14 | Treating the "4-step intelligence pipeline" (RETRIEVE/JUDGE/DISTILL/CONSOLIDATE) as something the default install runs | `settings-generator.ts` wires only `UserPromptSubmit`->route and `SubagentStop`->post-task; DISTILL (`hooks_intelligence_learn`) and CONSOLIDATE (EWC Fisher update) have no hook entry anywhere in the generator. The pipeline exists only as a sequence of manual MCP tool calls an operator or agent must choose to make. | `v3/@claude-flow/cli/src/init/settings-generator.ts:275-420` (full hook list read); `hooks-tools.ts:3146-4395` (tool definitions, no registration into hooks) | ruflo 3.56.x, default `claude-flow init` | If a future hook config adds a learn/consolidate call to SubagentStop or SessionEnd |

## Slicing research (2026-10-14)

| \# | Research 2026-10-14 | tried | result | evidence | conditions | retest when |
| -- | -- | -- | -- | -- | -- | -- |
| N41 | Research 2026-10-14 | Parallel cheap code-writers as the DEFAULT delegation shape (original D18 option A) | Unsupported: \~4-15x tokens; conflicting implicit decisions between parallel writers; cheap models resolve <10% on SWE-bench Lite in some setups; cascades lose once cheap failure passes \~20-25%; no published measurement of mixed-tier parallel coding | work/research-slicing/SYNTHESIS.md | 2026-10 sources | Our 6-to-20-task paired run (BOD-342 D18) |
| N42 | Research 2026-10-14 | A weak worker consulting a frontier 'smart friend' when stuck | Weak models cannot tell when they are stuck; no gain (also metaharness ADR-226) | rs-papers-cascades.md | - | - |
| N43 | Research 2026-10-14 | Trusting cheap-model research citations without checking | 4+ numbers were mis-cited by the cheap researchers (aider case count, Cognition %, Agentless, Google figures); a frontier synthesis that reads the primary sources is required | SYNTHESIS.md section 5 | agy gemini flash / sonnet | - |
| N44 | Review 2026-10-14 | Ruflo @claude-flow/guidance as drop-in enforcement | Real code but its tests are not run in CI; hooks register only if the caller passes a registry (off by default); default embeddings are a test-only hash; no measured result; 'tighten-only' lives in a different layer (ADR-447/449) | work/mining/ruflo-guidance.md | main 2026-10 | Read guard/index.ts and mod-projection.ts before copying any code |

## N45–N47 (added 2026-10-15)

| \# | source | tried | result | evidence | conditions | retest when |
| -- | -- | -- | -- | -- | -- | -- |
| N45 | Our run 2026-10-15 | 3 parallel Opus 5.5 (high) ticket owners on the Anthropic API credit with a $15/track budget checked via OmniRoute call-log cost | \~12.6M input tokens in \~25 min (\~$70 at list price); the call-log cost field reads 0 for the anthropic provider, so the budget check was blind; no prompt caching (cacheRead 0), so every turn resent the full context | OmniRoute call logs (tokens.in/out), BOD-342 D20 | OmniRoute 3.8.51, anthropic API key | Owners on a cheaper model, a token-based budget (tokens.in \* price), and prompt caching enabled |
| N46 | Our run 2026-10-15 | ClinePass (qwen3.8-max, kimi-k3) as the model for a long-running owner agent | Rate-limited: qwen3.8-max lasted 131 turns then 429 'all credentials cooling down'; kimi-k3 got 4 straight 429s (8 failures, \~10 min cooldown) on both accounts | OmniRoute model-cooldowns, child logs | ClinePass $9.99 plan x2 | Use ClinePass for bounded implementation slices (a few calls), not a 100+ turn owner loop; rotate models; space the calls |
| N47 | Our run 2026-10-15 | Free Nemotron 120B as a native AGENT worker (multi-step, with tools) on a real code fix (route latency) | False success: reported implemented and tests passing, but the diff was only 2 moved definitions plus commented-out code; no tests, no commit. It works for single-shot new-file slices via autodev (verified by tests), not for open-ended agent work | track2-v5 report 2026-10-15 07:4xZ | nemotron :free via kilocode | Use it only through `verdict autodev` with test verification, never as a free-form agent |

---

## N48–N52 (added 2026-11-11)

<!-- Note: N48 and N49 also appear from 2026-10-15 (prose entries, see section below).
     Operator should renumber one set. The 2026-11-11 table entries are canonical. -->

| \# | source | tried | result | evidence | conditions | retest when |
| -- | -- | -- | -- | -- | -- | -- |
| N48 | Our test 2026-11-11 | Isolating a Claude Code `-p` test against a throwaway ai-memory instance (port 49395, fake `HOME`/`AI_MEMORY_HOME`, `--strict-mcp-config`) | **It wrote to the LIVE store.** `--strict-mcp-config` isolates MCP only. Claude Code's installed ai-memory **plugin hooks** still fired and posted to the live server (49374). The live router applied the fake home's routes and created a real live project `verdict` holding 1 session, 1 page and 8 observations. Removed after a fresh backup with `ai-memory purge-session`. The project row is now empty | live DB `sessions`/`projects` rows; backup `~/.hermes/backups/` (74.47 MiB) | Claude Code with the ai-memory@ai-memory plugin 3.4.5; ai-memory 3.4.5 | Throwaway harness tests must disable hooks too (`claude --bare`), or run under a separate OS user. Verify by counting live `sessions` rows before and after |
| N49 | Our test 2026-11-11 | Running a throwaway Hermes (fake `HOME`/`HERMES_HOME`, own `HERMES_GATEWAY_LOCK_DIR`) with a **copied `~/.hermes/.env`** | The throwaway gateway connected the **live Telegram and Photon accounts**. Live gateway: `Telegram polling conflict (1/5)` and Photon `SIDECAR_CRASHED` at 01:36:34Z. Both self-recovered: Photon reconnected at 01:36:39Z, Telegram confirmed healthy at 01:37:09Z. Copies removed | `~/.hermes/logs/gateway.log` 2026-11-11 01:36-01:37Z | Hermes multiplexed gateway | **Never copy `.env` or `auth.json` into a scratch home** (operator rule). Pass the one provider key through the environment instead |
| N50 | Our run 2026-11-11 | Bulk `move-session` (Part C) with a per-session dry run that checked only the session's **home** project | `move-session` moves **all** of a session's observations wherever they are filed. One `dev` session carried 18,957 observations across 11 projects, including 2 in `operator`, so the move pulled rows out of `operator` and 14 other projects into `verdict`. The dry run's counts did not show where the rows were filed, so the protected-project check missed it. No command moves single observations back. Accepted by the operator (BOD-342 D23) | live DB vs pre-C backup `ai-memory-pre-C-20261111T042624Z`; `c-run/apply.jsonl`; `lifecycle-ops.md:760` | ai-memory 3.4.5 | Never: before a bulk move, count each session's observations by `project_id` and skip sessions that touch protected projects |
| N51 | Our run 2026-11-11 | Operator golden eval (BOD-346 AC5) on ai-memory 3.4.5 | hit@5 **0.70**, hit@1 0.55, 0 foreign hits, 12 misses. It was **0.825** on the live store on 3.2.x on 2026-11-06. The 0.90 bar is unchanged. Every expected page is still in `operator` with the right citation and text (445 pages, all embedded, hashes identical to before Part C), so recall ranks them below the top 5. **Cause: nomic-embed-text vector stream degrades recall; fix is `embedding_provider = "none"` with reranker on (N51 update below, hit@5 0.825)** | `scripts/memory_recall_eval.py` @ `f8ae14d1`; golden `~/.local/share/verdict-memory-eval/operator-golden.jsonl`; `openspec/changes/ref-docs-in-memory/tasks.md` 3.2 | ai-memory 3.4.5, live store, scoped to `default/operator` | 0.90 bar: retest after changing the recall model or reranker |
| N52 | Our review 2026-11-11 | `MrLuciano/ai-memory-hermes-plugin` as Hermes's memory provider (recall every turn, replacing the shell hooks except the two tool events) | **Rejected, not installed.** (1) Hermes CLI and `hermes -z` sessions pass no `cwd`, so the plugin scopes them to `hermes/hermes-default`, project unknown, writes off. (2) Its `sys.path.insert` and bare-name imports shadow Hermes's own `cli` module, so `from cli import save_config_value` raises ImportError. (3) No spool, a silent 60 s skip after 3 failures, and session-end on a daemon thread that `-z` can kill. Its recall is FTS-only (`/admin/search`, no reranker, 0 LLM calls), unscoped by default | plugin source v0.1.3; Hermes `agent/agent_init.py:1200`; ai-memory `admin.rs:2015`; `hermes plugins validate` passed 12/12 (structure only) | Hermes Agent v0.23.0, ai-memory 3.4.5, `plugins.isolation: in_process` | When the cwd-scoping and import-shadowing issues are fixed upstream |

**Follow-up from N48/N49 run:** Prime's throwaway recall also hit the live server. `~/.prime/agent/extensions/ai-memory-pi.ts` hardcodes `SERVER = "http://127.0.0.1:49374"` and ignored the scratch settings.

**N51 update (2026-11-11):** Cause confirmed as the nomic-embed-text vector stream. Mode b (embeddings off, reranker on) achieves hit@5 **0.825**. Chosen and applied by the operator. The 0.90 bar is unchanged.

---

## N53 — 2026-11-11: bearer token printed to transcript, local-only, rotated

| \# | source | tried | result | evidence | conditions | retest when |
| -- | -- | -- | -- | -- | -- | -- |
| N53 | Incident 2026-11-11 | An agent reading a hook-spool file printed its full field set, including the raw bearer token for the local memory service | The value reached the harness transcript and, via the proxy, the model provider's call logs. The listener was loopback-only (127.0.0.1); the token was never committed to git. Impact class: local-only credential exposure, no external reach, no data loss. The token was rotated the same session. Full details in the private Linear archive. | Incident log 2026-11-11, stored in the private archive | ai-memory hook-spool, local listener | — |

**Prevention rule:** when reading spool files, access only `url`, `attempts`, and `created_ms`; never log or print the `token` field. The memory-admin skill enforces this.

---

## Historical entries: N48 and N49 at 2026-10-15

These entries predate the canonical 2026-11-11 N48/N49 table above. The operator should
renumber them. They are preserved here verbatim.

**N48 (2026-10-15 12:14Z):** `verdict autodev --objective` on an EXISTING 32 KB test file (tests/test\_free\_tier\_admit.py, add one sibling test):

- No --executor-model: refused, "missing ExecutionPathDecision" (autodev does not choose its own executor; AD-03 gap).
- --executor-model mistral/codestral-latest: refused, "bootstrap requires an explicit provider/model:free route" (only :free ids are accepted).
- kc/nemotron-3-super:free: planned OK (codestral planner, 557 tokens); executor transport TimeoutError after 124 s.
- openrouter/nemotron-3-super:free: 13,129 executor tokens; patch rejected "bare hunk old lines must match source exactly once".

Lesson: on large existing files, the free single-shot patch path fails (a timeout, then an inexact hunk). Criterion 2 needs AD-03, the context pack, and a more tolerant patch format or edit-by-symbol. Do not retry the same route on the same file.

**N49 (2026-10-15):** an external agent created a user in the local memory service through its API. That enabled human auth with no root user, so the next service restart failed. Lesson: shared local services need an access rule for outside agents (read-only token, no user/config creation); before restarting, check the user table and auth state. Recovery: via `AI_MEMORY_AUTH__RECOVERY_TOKEN` in the EnvironmentFile.

---

## Numbering conflicts

The ledger has duplicate IDs: N48 and N49 appear once at 2026-10-15 (see historical
section above) and again at 2026-11-11 (the table). The operator should renumber the
2026-10-15 entries before the next review cycle. The 2026-11-11 N48–N52 table entries
are the canonical ones.
