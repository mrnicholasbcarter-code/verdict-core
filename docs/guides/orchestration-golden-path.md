# Orchestration golden path

One goal enters the orchestrator. Every settled run produces a locally verifiable
receipt, including runs blocked at startup, admission or planning: those receipts
have no nodes and carry the recorded blocked reason. This guide uses repository
commands. The historical observations below are not an exact-SHA public proof
bundle; see [Evidence](#evidence).

## What the audience sees

```
GOAL -> CONTROLLER (frontier planner) -> PLAN / DAG -> SELECT (eligibility ladder)
     -> WORKERS (parallel, per-node worktrees) -> QUOTA/COOLDOWN -> FAILURE/REASSIGN
     -> VERIFY (ownership + node checks + integration barrier) -> REVIEW (independent OCR)
     -> COMPLETE | BLOCKED
```

Roles:

- **Verdict:** plans, selects models, recovers, verifies and writes receipts.
- **Prime Agent:** is the default harness for bounded planner and worker calls
  (`prime-agent -p --model <exact route>`). Verdict also supports direct-gateway
  execution and mixed worker backends; these do not change Verdict's orchestration ownership.
- **OmniRoute:** only provides inventory and transport.

## Prerequisites

1. Use a reachable gateway (default `127.0.0.1:20128`; override with `--gateway`)
   and verify its actual admission limits for your workload. **Historical
   operator-reported reference deployment, not a universal prerequisite:** an
   OmniRoute v3.8.50 deployment associated with upstream issue
   diegosouzapw/OmniRoute#13648 used a systemd user drop-in
   `~/.config/systemd/user/omniroute.service.d/admission.conf` with
   `OMNIROUTE_CHAT_MAX_HEAVY_IN_FLIGHT=4`,
   `OMNIROUTE_CHAT_ADMISSION_HEALTHY_HEADROOM=2`,
   `OMNIROUTE_CHAT_ADMISSION_QUEUE_MS=120000`, and
   `OMNIROUTE_CHAT_ADMISSION_MAX_QUEUED_BYTES=16777216`; `heap.conf` set
   `--max-old-space-size=3072`. The reported `503 chat_admission_busy` and
   older 690 MB heap failures have no immutable incident/config artifact linked
   in this checkout. Do not copy these values without measuring your gateway.
2. **In that same operator-reported setup**, the Claude OAuth connection used
   `rateLimitProtection=true` and `rateLimitOverrides.maxWaitMs=120000` to avoid
   a reported local `504` after a 15 s limiter expiry. These are not required
   settings for every route or deployment; verify your own connection.
3. For Prime-executed workers, list usable routes in Prime's
   `~/.prime/agent/models.json`. A route absent from that harness can fail the
   `ENTITLED` stage with `not_harness_visible`. Other backends need their own
   gateway authorization and execution setup.
4. `ocr` (open-code-review v1.12.9) must be on PATH, and `VERDICT_OMNIROUTE_API_KEY` must be
   exported.

## Run it

```bash
# 1. Show dynamic eligibility (no model is chosen by hand):
verdict eligibility --scope cc/,cx/ --probe --frontier

# 2. One goal -> plan -> parallel workers -> review -> receipt (live view):
verdict orchestrate "Add textkit/stats.py and textkit/case.py with tests; full suite must pass" \
  --repo /path/to/repo --scope cc/,cx/ --max-parallel 3

# 3. Same, with chaos: planner quota, first worker route quota, second worker never answers.
# Use an explicit isolated state file outside real capacity state:
verdict orchestrate "<goal>" --repo /path/to/repo --scope cc/,cx/ \
  --state-file /path/to/isolated/chaos-health.json \
  --inject "#1=quota" --inject "worker#1=route_quota" --inject "worker#2=no_final"

# 4. Controller survival: generation 0 hangs, the supervisor kills it and resumes.
# Env-injected chaos also needs an explicit isolated state file:
# Supervised runs contain workers in a delegated cgroup-v2 subtree; a transient
# delegated user scope provides one without host changes.
VERDICT_CHAOS_G0="#2=hang" systemd-run --user --scope -p Delegate=yes \
  verdict supervise --cgroup-root auto --run-id demo --runs-dir /path/to/isolated/runs \
  --stall-seconds 90 -- "<goal>" --repo /path/to/repo --scope cc/,cx/ \
  --state-file /path/to/isolated/controller-chaos-health.json

# 5. Inspect any run afterwards:
verdict watch <run-dir> --once
verdict run-receipt <run-dir>
```

Exit code is `0` only for `COMPLETE`.

Rejected gateway credentials or permissions during selector discovery
write a `run_finished` BLOCKED event with `phase=startup` before planning. The
supervisor reports that cause without restarting unchanged credentials. These
early blocked runs still get a receipt with no nodes, so `verdict run-receipt`
shows the startup cause. Unexpected controller
implementation errors still use bounded crash recovery.

Keep the planner's `verification_command` test-focused: the runtime runs declared
repo quality gates at integration, after the combined tests pass. Discovery reads
Ruff/Mypy declarations from Python configuration and `typecheck`, `lint`, and
`format:check` scripts from `package.json` in a fixed order. The event log records
each gate's declared and executed command, source, exit code, and bounded output.
A gate failure blocks completion with verification feedback. No declared gates
is recorded as `none declared`, not a silent pass. `RuntimePolicy.run_repo_gates`
defaults to `True`; isolated fixtures can opt out explicitly.

## Partial-work salvage on retry

When a worker attempt fails, the runtime (`verdict/orchestration/runtime.py`)
preserves the attempt's owned-file partial work before the failed worktree is
removed, and restores it into the SAME node's next attempt worktree
(`verdict/orchestration/salvage.py`). Retries of a half-finished node continue
from the salvaged owned files instead of only the node base plus failure
feedback. Salvage changes no decision: acceptance, the node verification
command, admission and route selection behave exactly as without it, and wrong
salvaged work fails verification the same way wrong worker output does.

Handoff evidence is bounded and content-free. A `salvage` event in
`events.jsonl` and a node history record carry relative paths, byte counts,
content digests and rejection reasons only. File content never enters the
event log, the history or the digest-bound receipt.

Rejection boundaries:

- paths outside the node's ownership contract are never staged or restored;
  they are named in the rejection evidence without content;
- credential-bearing files are rejected whole before staging — neither content
  nor partial excerpts are kept or emitted;
- fault-injected attempts (`fault_injected` terminals) salvage nothing, so
  chaos runs cannot seed later attempts with synthetic partial work;
- an attempt with no owned-file changes stages nothing and the retry proceeds
  exactly as before (no-change safe).

Contract and tests: [`openspec/changes/bod-308-partial-salvage/`](../../openspec/changes/bod-308-partial-salvage/proposal.md),
`tests/test_orch_salvage.py` (actual `DagRuntime` failure-to-retry restoration).

## Fault keys (`--inject KEY=FAULT[,FAULT]`)

Keys:

| Key | Meaning |
|-----|---------|
| `cc/claude-sonnet-5` | exact route |
| `cc/*` | provider prefix |
| `@node_id` | node id |
| `#N` | N-th executor call; without `--graph`, `#1` is initial planning. Each plan repair increments N; with `--graph`, planning is bypassed. |
| `worker#N` | N-th worker dispatch (attempt worktree `<node>-a<N>`; planner calls do not count) |
| `*` | any call |

Faults:

- quotas and rate limits: `quota` (account-wide), `route_quota` (model-scoped), `rate_limit`;
- HTTP errors: `auth`, `payment`, `forbidden`, `server`;
- transport: `timeout`, `transport`, `hang`;
- bad worker output: `empty`, `no_final`, `malformed`, `mismatch`.

Injected failures are tagged `[injected]` in the view and `fault_injected` in the receipt.
Do not assume automatic per-run cooldown isolation: with `--inject` and no
`--state-file`, the default is the shared `<runs-root>/chaos/chaos-health.json`
(unless resuming a named run). Generation-scoped `VERDICT_CHAOS_Gn` injection
alone does **not** activate that chaos default and can use the global health
state. Pass a unique, explicit `--state-file` on **every** chaos launch,
including `verdict supervise`'s child `orchestrate` command. Use a path that
cannot overlap production capacity state; do not reuse it between chaos runs.

## Evidence

Historical operator/receipt-reported observations from 2026-09-24 against live
OmniRoute follow. The cited `~/.verdict/evidence/golden-path/` run records are
mutable host-local files, **not retained in this checkout**. The recorded
COMPLETE/BLOCKED summaries are bounded observations, not an attestation of the
producer SHA, real semantic review coverage, gateway settings, or exact
integration-test counts. A reproducible public claim would need sanitized,
committed event/receipt/review artifacts, producer/source revisions, and test
summaries. The table preserves the reported results without elevating them to
verified public proof:

| Run | Operator/receipt-reported observation | Reported outcome |
|-----|----------------|---------|
| live2 | 3 parallel nodes on 3 distinct `cc/*` routes, integration, OCR on a different family (review coverage not retained here) | COMPLETE; 24 integration tests reported, report not retained |
| live7 | planner quota -> planner replaced; account-level quota on both subscription providers -> explicit pool exhaustion | BLOCKED reported (fail-closed, no hang) |
| live9 | route quota, then no-final, then 429 rate limit; same node moved sonnet-4-6 -> sonnet-5 -> haiku; sibling moved to `cx/gpt-5.5`; review independence fell back to route level (review coverage not retained here) | COMPLETE; 28 integration tests reported, report not retained |
| live10 | `verdict supervise`: generation 0 hung -> STALLED -> process group killed -> generation 1 `--resume` reused the validated node | COMPLETE reported |

## Improvement cycles

`verdict improve` runs bounded cycles above `verdict supervise`: one supervised
run, measured from its receipt, then a lesson the next cycle passes to the
planner. A cycle with unchanged inputs after a failure is refused. See the
[CLI reference](../CLI_REFERENCE.md#verdict-improve--measured-improvement-cycles)
and [ADR-038](../adr/ADR-038-improvement-cycle.md).

## Controller bounded operations and Prime ownership

Supervision distinguishes real event progress from `WAITING_BOUNDED`. Initial and
repair planning each publish a fixed, owner/generation-bound operation deadline.
The supported cap is 600 seconds per planner call. Each worker attempt runs for
at most 900 seconds: `--attempt-timeout` values above 900 are reduced to 900.
REVIEW publishes a distinct `review-round-N` operation with a 900-second cap; the
remaining run/supervisor deadline still wins. An active valid operation can outlast the ordinary stall
window; its deadline and the absolute supervisor deadline still terminate it.
Invalid, stale, extended or reopened leases do not defer stalls. Selection refresh
and DAG operations do not gain this lease exemption. Admission and completion proof
gates are unchanged.

The OCR reviewer runs selection and subprocess calls off the event loop under the
remaining REVIEW budget. The supported subprocess runner accepts cancellation,
kills/reaps its process group, and returns terminal evidence. If cleanup cannot
settle inside the bounded reserve, Verdict reports `ocr review cleanup UNKNOWN
after cancellation`, fails the review closed with local `scope=none`, and does not
retry or fall back to another independence level based on uncertain reviewer work.

Supervised Prime requires an operator-selected writable delegated cgroup-v2
subtree, passed as `verdict supervise --cgroup-root PATH` or the environment variable
`VERDICT_CONTROLLER_CGROUP_ROOT`. `--cgroup-root auto` uses the supervisor's own
scope when it is delegated, for example under
`systemd-run --user --scope -p Delegate=yes verdict supervise --cgroup-root auto ...`.
Without a root, `verdict supervise` exits 2 before creating run state. It must expose writable `cgroup.procs` and `cgroup.kill`, and
readable `cgroup.events`. The runtime creates only its unique generation child leaf.
Never point at the cgroup mount root. Never migrate or kill the supervising process's
current scope. No systemd/global permissions or resource-controller settings are
changed automatically. Missing delegation blocks Prime before payload execution.
Unsupervised Prime keeps its existing execution behavior.

The worker launcher records pending intent before spawn, joins the generation leaf
and registers exact process identity before exec. Cleanup uses kernel `cgroup.kill`
and requires `populated 0` before restart. Ordinary fork/clone/setsid descendants are
contained. Pending registration, unverified revocation, ownership mismatch or cleanup
errors block restart even after known containment is stopped. Deliberate same-UID
cgroup escape or state tampering is outside this cooperative-controller trust model.
Older run artifacts cannot prove the absence of workers detached before this fix.

Offline fake clocks, local children and fixture cgroup files validate the mechanism;
they do not establish live provider or self-hosting COMPLETE evidence.

## Known limits

See ADR-036 "Known limits". Merge to `main`, Linear status changes and PR updates are separate
operator decisions.
