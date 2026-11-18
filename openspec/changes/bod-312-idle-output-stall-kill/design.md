# Design: BOD-312 idle-output stall kill

## Context

`PrimeHeadlessExecutor._run` awaited `proc.communicate()` under one
`asyncio.wait_for(timeout_seconds)`. Output was only seen at exit, so silence
could not be detected.

## Decisions

- Read both pipes with two reader tasks that stamp `last_output` on every
  chunk, and wait on the readers plus `proc.wait()` with a deadline of
  `min(attempt left, idle left)`. One loop enforces both bounds.
- Reuse `_kill_group` (SIGTERM, 5 s grace, SIGKILL) for both cuts, so a
  stalled worker's children die with it.
- A new category `stalled` instead of reusing `timeout`: the receipt has to
  say why the attempt ended. It shares timeout's REROUTE/route/120 s policy,
  and maps to the `timeout` class wherever a fixed taxonomy is required
  (SessionLedger, outcome records, health cache).
- Default 300 s: a Prime worker streams JSON events while it works, so five
  silent minutes is a stall, and it is well under the 900 s attempt bound.

## Non-goals

- Other executors (`DirectGatewayExecutor` already streams with per-read
  httpx timeouts).
- Event-driven supervision beyond this kill.
