# Verdict interview cheat sheet

**Describes `main` at commit 8954089, certified from a fresh clone on 2026-09-24.**

One page. Printable. Every number below has a named source. Sources:
[README](../README.md), [runbook](guides/interview-golden-path.md),
[ADR-036](adr/ADR-036-goal-to-receipt-orchestration.md),
main certification (`~/.verdict/evidence/interview-main/main-certification.md`, local operator evidence),
[story bank](portfolio/ADVERSARIAL_INTERVIEW_STORY_BANK.md),
[branch reconciliation](BRANCH_RECONCILIATION.md).

## 1. 30-second pitch

Verdict is a fail-closed control plane for LLM work. One goal goes in, one verified receipt
comes out. Hard eligibility gates run before any advisory ranking, so a model that fails a gate
cannot be re-admitted by a score. A single `verdict orchestrate` call takes a goal, asks a
frontier model for a DAG, selects a route per node through the eligibility ladder, runs workers
in parallel worktrees, recovers from real quota and outage faults by moving the same node to a
new route, verifies ownership and tests, runs an independent AI review that excludes every
implementer route, and writes an append-only event log with a digest. `COMPLETE` requires
validated work, an ok integration barrier and a `PASS` review. Everything else is `BLOCKED`.

## 2. Three-minute live demo

Prerequisites (from the runbook): OmniRoute on `127.0.0.1:20128` with the admission and
limiter drop-ins, `VERDICT_OMNIROUTE_API_KEY` exported, `ocr` on PATH, and every usable route
listed in Prime's `~/.prime/agent/models.json`.

Note: Codex capacity may be exhausted. The demo runs on Claude routes only: pass `--scope cc/`.

```bash
# 0. Scratch repo, so the demo never touches real work
DEMO=$(mktemp -d)/demo && mkdir -p "$DEMO" && cd "$DEMO" && git init -q .

# 1. Home screen
verdict

# 2. Dynamic eligibility - no model is chosen by hand
verdict eligibility --scope cc/ --probe --frontier

# 3. One goal -> plan -> parallel workers -> review -> receipt, with chaos
verdict orchestrate "Add textkit/stats.py and textkit/case.py with tests; full suite must pass" \
  --repo "$DEMO" --scope cc/ --max-parallel 3 \
  --inject "#2=route_quota" --inject "#3=no_final" --inject "#5=rate_limit"

# 4. Inspect the run afterwards
verdict watch <run-dir> --once
verdict run-receipt <run-dir>
```

What to point at, step by step:

- **Home screen:** gateway status, recent runs, main commands. Nothing is configured by hand.
- **Eligibility:** the ladder `DISCOVERED -> ENTITLED -> HEALTHY -> AVAILABLE -> TASK_ELIGIBLE
  -> SELECTED`, and the counts shrinking at each stage. Say: economics only orders what already
  passed; there is no static fallback chain.
- **CONTROLLER / PLAN panel:** the frontier planner route, the chosen topology
  (`PARALLEL_WORK_UNITS`), and the layers `L0 / L1 / L2`. Verdict validated this graph: no
  cycles, no unknown dependencies, no shared write ownership between concurrent nodes.
- **SELECT panel:** one route per node, each with its capacity class (`subscription`), spread
  across providers by load.
- **WORKERS panel:** the reassignment trail on a failed node, for example
  `sonnet-4-6 x -> sonnet-5 x -> haiku-4-5 ok`. Injected faults are tagged `[injected]`.
- **REVIEW:** the reviewer route, and that it is excluded from every implementer route.
- **`run-receipt`:** the event-log digest is recomputed, so the receipt is verified, not trusted.
  Per-node attempts, cooldowns, reassignments and review independence are all in it.
- Exit code is `0` only for `COMPLETE`.

Optional fifth step if there is time - controller survival:

```bash
VERDICT_CHAOS_G0="#2=hang" verdict supervise --run-id demo --runs-dir "$DEMO/.verdict/runs" \
  --stall-seconds 90 -- "<goal>" --repo "$DEMO" --scope cc/
```

**Fallback if the gateway or a model is down.** Do not retry live. Switch to recorded evidence
and narrate the same story from receipts:

Fresh `main` rehearsal evidence (most recent):
```bash
verdict run-receipt /home/nick/.verdict/evidence/interview-main/rehearsal/runs/clean    # COMPLETE
verdict run-receipt /home/nick/.verdict/evidence/interview-main/rehearsal/runs/chaos    # BLOCKED
```

Golden-path reference runs (further fallbacks):
```bash
verdict run-receipt ~/.verdict/evidence/golden-path/live9-run    # reassignment chain, COMPLETE
verdict watch ~/.verdict/evidence/golden-path/live10-run --once  # supervisor kill + resume
verdict run-receipt ~/.verdict/evidence/golden-path/live7-run    # pool exhaustion, BLOCKED
verdict run-receipt ~/.verdict/evidence/golden-path/certlive-run # fresh-clone certification run
```

Run ids worth naming: `clean` (12 tests on integration ref, OCR PASS), `chaos` (provider-scoped
429, fail-closed), `live9` (route quota, then no-final, then 429; 28 tests), `live7` (planner
quota, then pool exhaustion), `live10` (hung controller, killed, resumed), `certlive` (fresh
clone, 30 tests). Say plainly: these are local operator evidence files, not a public CI artifact.

## 3. Architecture in six bullets

- **Three-role split.** Verdict plans, selects models, recovers, verifies and writes receipts.
  The Prime Agent harness only runs worker processes (`prime-agent -p --model <exact route>`)
  and never selects a model. OmniRoute only gives `/v1/models` inventory and
  `/v1/chat/completions` execution; it is not a metadata source of truth.
- **Eligibility ladder.** Per node attempt: `DISCOVERED` (live inventory), `ENTITLED` (an active
  account backs it and the harness can spawn it), `HEALTHY` (fresh cached 1-token probe),
  `AVAILABLE` (no route/provider cooldown, no rate-limit window), `TASK_ELIGIBLE` (capabilities,
  context, frontier policy, reviewer exclusions), `SELECTED`. Capacity class comes from account
  evidence, ordered subscription > free > metered > unknown.
- **DAG runtime.** Ready nodes run concurrently up to `max_parallel`, each in its own git
  worktree based on its validated dependencies. Lifecycle: `PLANNED -> ADMITTED -> DISPATCHED ->
  RUNNING -> TERMINAL_SUCCESS/TERMINAL_FAILURE -> VALIDATED/REJECTED`. Admission is never
  success. A failed node blocks only its dependents; siblings keep running.
- **Recovery.** Terminals are classified by status code first, versioned text second: quota vs
  rate limit, 401/402/403/400/404, 5xx, timeout, transport, empty or no final answer, malformed,
  model mismatch, gateway admission shed, verification and ownership. Reset hints set the
  cooldown scope (route or provider). The same node contract then goes to a newly selected
  route. An empty bounded pool is an explicit `FAIL_CLOSED`.
- **Independent review.** After integration, `ocr` (Alibaba OpenCodeReview) reviews the
  integrated diff on a ladder-selected reviewer route that excludes every implementer route, and
  a different model family when one has capacity. Error, timeout, empty output or all-items-
  failed is `ERROR`, never `PASS`.
- **Receipts.** One append-only, fsynced, secret-scrubbed `events.jsonl` feeds both the live view
  and `receipt.json`. The receipt carries per-node attempts, cooldowns, reassignments, review
  independence and a digest of the event log. `completion_verdict` returns `COMPLETE` only with
  validated work, an ok integration barrier and a `PASS` review; otherwise `BLOCKED` with the
  first concrete reason.

## 4. Hard questions, honest answers

**Why not a static fallback chain?** A static chain encodes yesterday's capacity. Verdict
re-derives candidates per attempt from live inventory, account entitlement, a fresh probe, and
current cooldowns. Economics only orders the routes that already passed. `--prefer` is a
ranking policy among subscription capacity, not a chain. A chain also cannot express
"this route is cooled until the parsed reset time, but its provider is fine".

**How do you avoid self-certification?** Three separations. The reviewer route is excluded from
every implementer route used in the run, including routes from before a controller restart, and
a different family is preferred when capacity allows. Verification is a command the controller
runs, not a model opinion: ownership barrier, per-node checks, then the full suite on the
integration commit. The receipt is verified by recomputing the event-log digest. The residual
honest gap: an AI reviewer does not detect every defect, and the story bank is interview
material, not an independent attestation.

**What if the controller's own model quota dies?** Frontier planning uses the same
classify / cooldown / reselect loop as workers, so planning moves to another eligible frontier
model (`live7`: `cc/claude-fable-5` quota -> provider `cc` cooled -> `cx/gpt-5.5` planned;
`live11` is the reverse). If the controller process itself hangs or dies, `verdict supervise`
watches `progress.json`, kills the stalled process group, and restarts with `--resume`;
validated nodes are reused and abandoned attempts are recorded. Restarts and a total deadline
are bounded. When it gives up it writes `FAILED_CLOSED` and a `BLOCKED` verdict.

**How do you know receipts are not faked?** The receipt is derived from an append-only, fsynced
event log and carries a SHA-256 digest of it; `run-receipt` recomputes the digest. Be precise
about the boundary: this proves the run record was not altered after the fact. It does not
attest LLM output quality, and it is not a signature against a tampering author who controls the
whole machine.

**What is not shipped?** See section 7 for shipped vs not shipped detail.

## 5. Four STAR stories

**Gateway admission is not a model failure.**
- S/T: parallel agent calls returned `503 chat_admission_busy`, and long turns returned a local
  `504`; both look like model failures and would cool healthy routes.
- A: found upstream OmniRoute issue `diegosouzapw/OmniRoute#13648`, recorded the admission /
  headroom / queue settings, set `rateLimitOverrides.maxWaitMs=120000`, and classified
  `gateway_busy` as an infrastructure retry that does not penalize the route. Admission to an
  SSE stream is not a final answer: `no_final_answer` is its own class.
- R: the certified host ran with those prerequisites and `certlive` completed. This does not
  claim the upstream bug is fixed or that 503/504 can never recur.

**Reviewer independence across a controller restart.**
- S/T: a resume restored validated commits but dropped their implementer route identities, so
  the reviewer could have been the model that wrote the code.
- A: restored `(commit, route_id)` from validated node events into resumed state, kept the
  exclusion over all implementer routes including pre-restart ones, and added
  `tests/test_orch_resume.py::test_resume_restores_implementer_routes_for_review_independence`.
- R: `live10` resumed a validated node after a stalled generation; its receipt records the
  previous Claude implementer routes as exclusions, a `cx/gpt-5.5` reviewer, review `PASS`,
  `COMPLETE`. This is a route-exclusion invariant on that run, not a defect-detection claim.

**Replacing a quota-exhausted frontier planner.**
- S/T: planning itself needs a model; if its provider's quota is gone there is no DAG yet, and a
  worker-only retry policy cannot recover.
- A: applied the same classify / cooldown / reselect discipline to the planner call, scoped the
  cooldown to the affected provider, and reselected before validating any graph; the injected
  fault is tagged in the attempt records.
- R: `live7` replaced `cc/claude-fable-5` with `cx/gpt-5.5`, which produced a plan; `live11`
  showed the reverse. Both runs still ended `BLOCKED` when the worker pool exhausted - planner
  recovery did not manufacture a `COMPLETE`.

**A read-only worker ran a destructive command.**
- S/T: a worker prompted to stay read-only ran `rm -rf /tmp/vgp && mkdir -p ...` in its own
  shell tool call to build a scratch HOME; the damage had to be contained and understood.
- A: the controller noticed the missing directory on its next command, read the worker session
  journal, deleted that worker, sent explicit rules to the sibling audit workers, and
  re-dispatched to a different route with an ABSOLUTE RULES block banning destructive commands
  and requiring private `mktemp -d` scratch.
- R: loss was confined to `/tmp/vgp` scratch; repo, branches, worktrees and
  `~/.verdict/evidence` were untouched. Detection was after the fact. A prompt is policy, not an
  OS-level enforcement boundary; there is no automated shell interception here.

## 6. Numbers you can quote

| Number | Meaning | Source |
|---|---|---|
| 2976 passed, 1 warning | full suite on fresh clone of `main` @ 8954089 | main certification |
| mypy --strict on 213 files | fresh-clone gate, pass | main certification |
| 270 doc files verified | doc links checked on fresh clone | main certification |
| 12 tests, OCR PASS on cc/claude-fable-5 | main rehearsal clean run | main rehearsal |
| provider-scoped 429 fail-closed | main rehearsal chaos run outcome | main rehearsal |
| A-J | live scenario matrix, faults injected and tagged | golden-path certification |
| 4 CPU / 8 GB, OmniRoute v3.8.50 | certification host | golden-path certification |
| 24 / 28 / 30 tests | integration-ref suite size in `live2` / `live9` / `certlive` | runbook |
| 85 -> 16 -> 5 -> 1 -> 1 | `live9` ladder: DISCOVERED, ENTITLED, HEALTHY, AVAILABLE, ELIGIBLE | README |
| 4 nodes, 3 reassignments, 3 cooldowns | `live9` recorded header line | README |
| cli.py 5901 -> 4549 lines | BOD-187 parser/dispatch split | repo git log |
| 69 -> 8 local branches, 29 -> 8 worktrees | branch reconciliation (BOD-190) | reconciliation |
| 52 local branches removed | each PR MERGED or ancestor of `origin/main` | reconciliation |
| 79 remote branches deleted | operator-authorized, after a verified bundle backup | reconciliation |
| 101 remote-tracking refs before cleanup | audit baseline | reconciliation |
| ~$0.16 routed vs ~$0.52 baseline | deterministic mock over 100 requests; estimates, not invoices | README |
| version 0.2.0, 36 numbered ADRs | current state | README |

## 7. Shipped vs not shipped

**Shipped on main:** Goal-to-receipt orchestration (ADR-036), eligibility ladder, parallel
DAG runtime with reassignment/cooldowns, independent OCR review, digest-verified receipts,
`verdict supervise` stall/resume, home screen and CLI, UNDERSTAND and HYDRATE stages.

**Not shipped:** BOD-157 adaptive concurrency, fix-and-review loop after blocking OCR
finding, per-node critic pass, interactive Prime root-session survival, BOD-70 dogfood on
main, BOD-188 full harness-independence proof.

Phrases to avoid: "production deployment", "proves the reviewer catches every bug". Say instead:
merged to main with green CI, certified from a fresh clone on one host, with local operator evidence.
