# Harness Independence Proof — 2026-09-28

## What this proves

The Verdict orchestration pipeline can complete an end-to-end run using either:

1. **Prime headless executor** (`PrimeHeadlessExecutor`): spawns a Prime Agent
   CLI per node, which edits files directly in the worktree.
2. **Direct gateway executor** (`DirectGatewayExecutor`): sends a single HTTP
   request to OmniRoute per node. For implement nodes the model returns a
   unified diff; the executor validates paths against `owned_files`, runs
   `git apply --check` then `git apply`, and fails closed on any violation.

Both runs used the same orchestration controller, the same eligibility ladder,
the same recovery classifier, the same integration barrier, and the same
independent code reviewer. The executor is the only substituted component.

## What this does NOT prove

- The two runs are not identical: they received different plans from the planner
  (different throwaway repos, different planner calls). The proof shows both
  paths reach COMPLETE with integrity OK, not that they produce identical output.
- The direct-gateway executor does not have multi-turn capability. It sends one
  prompt and expects one response. Complex implement nodes that would benefit
  from iterative tool use may fail more often with this executor.

## Runs

| Run | Executor | Outcome | Integrity | Worker model | Reviewer |
|-----|----------|---------|-----------|-------------|----------|
| `prime-run/` | `prime-headless` | COMPLETE | OK | kr/claude-sonnet-4 | kr/gpt-5.6-terra |
| `direct-gateway-run/` | `direct-gateway` | COMPLETE | OK | kr/claude-sonnet-4 | kr/gpt-5.6-terra |

## Verification

```
verdict run-receipt docs/proof/harness-independence-2026-09-28/prime-run
verdict run-receipt docs/proof/harness-independence-2026-09-28/direct-gateway-run
```

Both should report `integrity: OK (events digest verified)`.

## Path scrubbing

Absolute paths (`/home/.../venv/bin/python`) in verification command output
were replaced with `<VENV>/python`. The receipt digest was recomputed by
`write_run_receipt` from the scrubbed events so `verdict run-receipt` still
returns integrity OK.

## Executor identity

The `run_started` event in each run includes an `executor` field:
- `prime-run/`: `executor: "prime-headless"`
- `direct-gateway-run/`: `executor: "direct-gateway"`
