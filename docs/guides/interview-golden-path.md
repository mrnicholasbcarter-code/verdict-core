# Interview golden path

One goal in, one verified receipt out. This guide uses only commands that exist on
`feat/interview-golden-path`. Every claim below links to a recorded run. See [Evidence](#evidence).

## What the audience sees

```
GOAL -> CONTROLLER (frontier planner) -> PLAN / DAG -> SELECT (eligibility ladder)
     -> WORKERS (parallel, per-node worktrees) -> QUOTA/COOLDOWN -> FAILURE/REASSIGN
     -> VERIFY (ownership + node checks + integration barrier) -> REVIEW (independent OCR)
     -> COMPLETE | BLOCKED
```

Roles:

- **Verdict:** plans, selects models, recovers, verifies and writes receipts.
- **Prime Agent:** only runs the worker processes (`prime-agent -p --model <exact route>`).
- **OmniRoute:** only provides inventory and transport.

## Prerequisites

1. OmniRoute is running on `127.0.0.1:20128`. Admission must be tuned for parallel agents
   (upstream issue diegosouzapw/OmniRoute#13648, affects v3.8.50). The systemd user drop-in
   `~/.config/systemd/user/omniroute.service.d/admission.conf` sets:
   `OMNIROUTE_CHAT_MAX_HEAVY_IN_FLIGHT=4`, `OMNIROUTE_CHAT_ADMISSION_HEALTHY_HEADROOM=2`,
   `OMNIROUTE_CHAT_ADMISSION_QUEUE_MS=120000`, `OMNIROUTE_CHAT_ADMISSION_MAX_QUEUED_BYTES=16777216`.
   `heap.conf` keeps `--max-old-space-size=3072`. Without these settings, concurrent heavy
   requests get `503 chat_admission_busy`, and older 690 MB heaps ran out of memory.
2. The Claude OAuth connection has `rateLimitProtection=true`. Give it
   `rateLimitOverrides.maxWaitMs=120000`. The global 15 s limiter expiry otherwise returns a
   local `504` on long agent or reviewer turns.
3. Every route that workers may use must be listed in Prime's `~/.prime/agent/models.json`.
   Routes that are not listed fail the `ENTITLED` stage with reason `not_harness_visible`.
4. `ocr` (open-code-review v1.12.9) must be on PATH, and `VERDICT_OMNIROUTE_API_KEY` must be
   exported.

## Run it

```bash
# 1. Show dynamic eligibility (no model is chosen by hand):
verdict eligibility --scope cc/,cx/ --probe --frontier

# 2. One goal -> plan -> parallel workers -> review -> receipt (live view):
verdict orchestrate "Add textkit/stats.py and textkit/case.py with tests; full suite must pass" \
  --repo /path/to/repo --scope cc/,cx/ --max-parallel 3

# 3. Same, with chaos: planner quota, a worker quota, a worker that never answers:
verdict orchestrate "<goal>" --repo /path/to/repo --scope cc/,cx/ \
  --inject "#1=quota" --inject "#2=route_quota" --inject "#3=no_final"

# 4. Controller survival: generation 0 hangs, the supervisor kills it and resumes:
VERDICT_CHAOS_G0="#2=hang" verdict supervise --run-id demo --runs-dir /path/to/repo/.verdict/runs \
  --stall-seconds 90 -- "<goal>" --repo /path/to/repo --scope cc/,cx/

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
| `#N` | N-th executor call; `#1` is planning |
| `*` | any call |

Faults:

- quotas and rate limits: `quota` (account-wide), `route_quota` (model-scoped), `rate_limit`;
- HTTP errors: `auth`, `payment`, `forbidden`, `server`;
- transport: `timeout`, `transport`, `hang`;
- bad worker output: `empty`, `no_final`, `malformed`, `mismatch`.

Injected failures are tagged `[injected]` in the view and `fault_injected` in the receipt.
Chaos runs keep cooldowns in a per-run state file, so real capacity is not affected.

## Evidence

Recorded on 2026-09-24 against live OmniRoute. The runs are in `~/.verdict/evidence/golden-path/`:

| Run | What it proves | Outcome |
|-----|----------------|---------|
| live2 | 3 parallel nodes on 3 distinct `cc/*` routes, integration, OCR on a different family | COMPLETE, 24 tests on integration ref |
| live7 | planner quota -> planner replaced; account-level quota on both subscription providers -> explicit pool exhaustion | BLOCKED (fail-closed, no hang) |
| live9 | route quota, then no-final, then 429 rate limit; same node moved sonnet-4-6 -> sonnet-5 -> haiku; sibling moved to `cx/gpt-5.5`; review independence fell back to route level | COMPLETE, 28 tests on integration ref |
| live10 | `verdict supervise`: generation 0 hung -> STALLED -> process group killed -> generation 1 `--resume` reused the validated node -> COMPLETE | COMPLETE |

## Known limits

See ADR-036 "Known limits". Merge to `main`, Linear status changes and PR updates are separate
operator decisions.
