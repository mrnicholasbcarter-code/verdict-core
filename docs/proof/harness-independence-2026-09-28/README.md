# Harness Independence Proof — 2026-09-28

## What this proves

The Verdict orchestration pipeline can complete an end-to-end run using either:

1. **Prime headless executor** (`PrimeHeadlessExecutor`): spawns a Prime Agent
   CLI per node, which edits files directly in the worktree.
2. **Direct gateway executor** (`DirectGatewayExecutor`): sends a single HTTP
   request to OmniRoute per node. For implement nodes the model returns a
   unified diff; the executor validates paths against `owned_files`, runs
   `git apply --check` then `git apply`, and fails closed on any violation.

Both runs used the same orchestration components: controller, eligibility
ladder, recovery classifier, integration barrier and reviewer integration.
Their executors, generated plans and throwaway repositories differed. This is
not a controlled comparison where only one component changed.

## What this does NOT prove

- The two runs are not identical: they received different plans from the planner
  (different throwaway repos, different planner calls). The proof shows both
  paths reach COMPLETE with integrity OK, not that they produce identical output.
- The direct-gateway executor does not have multi-turn capability. It sends one
  prompt and expects one response. Complex implement nodes that would benefit
  from iterative tool use may fail more often with this executor.

## Runs

| Run | Executor | Outcome | Integrity | Selected worker route | Reviewer |
|-----|----------|---------|-----------|-----------------------|----------|
| `prime-run/` | `prime-headless` | COMPLETE | OK | kr/claude-sonnet-4 | kr/gpt-5.6-terra |
| `direct-gateway-run/` | `direct-gateway` | COMPLETE | OK | kr/claude-sonnet-4 | kr/gpt-5.6-terra |

For `direct-gateway-run/`, the model reported by the worker was
`claude-sonnet-4` without `kr/`. Its receipt records `route_identity: mismatch`
and a warning: the selected route does not verify the serving provider.

## Verification

```
verdict run-receipt docs/proof/harness-independence-2026-09-28/prime-run
verdict run-receipt docs/proof/harness-independence-2026-09-28/direct-gateway-run
```

Both should report `integrity: OK (events digest verified)`.

## Path scrubbing

The capture author reports replacing absolute paths
(`/home/.../venv/bin/python`) in verification command output with
`<VENV>/python`, then calling `write_run_receipt` on the scrubbed events.
The retained public log contains the placeholders and its receipt digest
verifies with `verdict run-receipt`. The original logs and rewrite history
are not retained: this proves consistency of the public log against the
retained receipt, not the authenticity of the original log or scrub sequence.

## Executor identity

The `run_started` event in each run includes an `executor` field:
- `prime-run/`: `executor: "prime-headless"`
- `direct-gateway-run/`: `executor: "direct-gateway"`
