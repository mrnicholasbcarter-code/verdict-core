# Runtime ownership and orchestration run ownership

This guide is about the current execution path, not a general process manager.
For the historical global-runtime ownership contract, see
[ADR-008](../adr/ADR-008-global-runtime-ownership.md). It is separate from the
ADR-036 orchestration runtime.

## One goal, one run directory

[ADR-036](../adr/ADR-036-goal-to-receipt-orchestration.md) makes Verdict the
owner of one orchestration run. `verdict orchestrate` writes a run directory
under `.verdict/runs` by default, relative to `--repo`. The command records an
append-only event stream and produces a receipt for the run.

Use the run commands rather than inspecting or changing worker processes by
name:

```bash
verdict orchestrate "<goal>" --repo /path/to/repository --max-parallel 3
verdict watch <run-id-or-directory> --once
verdict run-receipt <run-id-or-directory> --json
```

`verdict watch` renders the current run state. `verdict run-receipt` shows and
verifies the orchestration receipt. Both accept a run ID or a run directory;
`--runs-dir` selects a different root.

## Controller supervision

`verdict supervise` starts and watches an orchestration controller for a named
run. It requires `--run-id` and `--runs-dir`; the arguments for the resumed
orchestration command follow `--`. Its bounded controls are
`--stall-seconds`, `--poll-seconds`, `--max-restarts`, and
`--total-deadline-seconds`.

```bash
verdict supervise --run-id demo --runs-dir /path/to/repository/.verdict/runs \
  --stall-seconds 90 -- "<goal>" --repo /path/to/repository
```

The supervisor uses the run's progress data to detect a stalled controller and
restarts it with resume semantics within its configured limits. The receipt
records the run outcome. A worker or controller failure is not silently treated
as success.

## Exact worker ownership

The orchestration executor launches each selected route through the Prime
headless CLI. The selection boundary is in `verdict.subagent_selection` and
`verdict.worker_runtime`: inventory and Prime visibility are checked, a
bounded health probe is used, the exact selector is launched, and the terminal
result is validated. Replacement attempts stay within the configured runtime
budget and use another eligible candidate.

Do not put credentials, prompts, raw provider responses, or process command
lines into receipts or run evidence. See [interview golden path](interview-golden-path.md)
for the end-to-end path.
