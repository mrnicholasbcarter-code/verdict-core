# Orchestration golden path

One goal enters the orchestrator. Completed execution paths can produce locally
verifiable receipts; early admission or planning failures may retain only events,
a graph, and a blocked reason, without a receipt. This guide uses repository
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
VERDICT_CHAOS_G0="#2=hang" verdict supervise --run-id demo --runs-dir /path/to/isolated/runs \
  --stall-seconds 90 -- "<goal>" --repo /path/to/repo --scope cc/,cx/ \
  --state-file /path/to/isolated/controller-chaos-health.json

# 5. Inspect any run afterwards:
verdict watch <run-dir> --once
verdict run-receipt <run-dir>
```

Exit code is `0` only for `COMPLETE`.

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

## Known limits

See ADR-036 "Known limits". Merge to `main`, Linear status changes and PR updates are separate
operator decisions.
