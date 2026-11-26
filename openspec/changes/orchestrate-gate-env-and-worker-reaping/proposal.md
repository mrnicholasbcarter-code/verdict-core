# Proposal: repo gates run in the project's environment; worker processes end with their attempt

## Why

The fourth self-build run (#1354) validated every node, ended BLOCKED at integration on an
environment fault, and left worker processes running (issue #1363). A run that cannot reach
review unattended yields no accepted work, and a worker that outlives its run holds a model
session and a worktree. KNOWN: `uv run --frozen mypy` in a fresh worktree used a global mypy
without the project's extras (50 errors in 16 files); with `--all-extras` the tree passes.
KNOWN: OpenCode 2.3.2 starts its server and the model's tool commands in sessions of their own,
outside the group the executor signals. NOT AVAILABLE: why a leftover `opencode run` leader was
seen. NOT AVAILABLE until merged main: a full self-build run.

## What Changes

- `resolve_gate_argv`: mypy runs as `uv run --frozen --all-extras mypy` when the project
  declares extras; ruff unchanged; a uv-made `.venv` is completed through `uv run`.
- Integration barrier: a gate that cannot start, and unresolved third-party imports in a failed
  mypy gate whose every error the run's untouched base has too, are an `environment_error` (BLOCK,
  not judged), not `verification_failed`. An import only the candidate cannot resolve (a typo, a
  deleted package) and a new type error stay `verification_failed`.
- `OpenCodeExecutor`: every attempt's processes carry a marker; every exit path ends them,
  waits, then removes the attempt directory. `run_golden_path`: SIGTERM and SIGINT cancel the
  run, workers end, exit 143 or 130.

## Acceptance criteria

- The resolved mypy gate passes in a uv project whose stubs are in an extra; today's fails (test).
- Environment fault at integration is `environment_error`; a newly introduced missing import,
  a first-party one, a cut output with more errors than the base and other failures are not (tests).
- No process of an attempt outlives `run`: success, failure, stall, timeout, cancellation (tests).
- A signalled orchestrator process leaves no worker behind (test).
- On merged main a self-build run reaches review and leaves no worker process (post-merge).

## Capabilities

### New Capabilities

- `orchestrate-gate-env-and-worker-reaping`: gate environment, integration environment errors,
  worker process reaping.

### Modified Capabilities

- None.

## Impact

`verdict/orchestration/{verification,runtime,executor_opencode,run}.py` and tests. Unchanged:
admission, routing, serve, the other items of #1354.
