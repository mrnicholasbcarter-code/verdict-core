# Proposal: propagate chosen effort into orchestrate worker dispatch

## Why

`choose_effort` already decides Prime thinking level from task kind and explicit model support, but only the Prime worker path records and sends it. `verdict orchestrate` selects a route and dispatches a node without choosing effort, so the receipt cannot bind the level Verdict asked for.

## What Changes

- Map implement and integrate nodes to task kind `implement`, review to `review`, and research to `plan`.
- Compute chosen effort before the selection event and record `chosen_effort` plus `effort_reason` on that event.
- Pass the chosen value as an optional per-call `effort` argument. Omitted keeps the executor constructor default. Explicit null omits the provider override. Executors that do not accept the keyword are called unchanged.
- Prime headless uses the per-call value for `--thinking` without mutating shared executor state. Direct gateway sends `reasoning_effort` only when a level was chosen. That field is passthrough transport, not evidence the provider applied it.
- Attempt receipts copy the chosen effort from the selection that matches the dispatched route. A replaced selection cannot leak onto another route. Legacy events without the field keep the previous receipt shape. `executed_effort` stays `unverified`.

## Capabilities

### New Capabilities

- `orchestrate-effort-dispatch`: chosen effort is recorded and forwarded on the orchestrate path.

### Modified Capabilities

## Impact

Orchestrate runtime, executor call plumbing, and run receipts. No eligibility, ranking, or gateway setting changes. No live provider calls are required to prove the dispatch contract.
