## Why

`verdict bench context-lift --json` reports `"conclusion": "blocked"` when no
free-tier route is eligible, but exits 0.  `verdict doctor` reads the timer
state as healthy, and the hourly acceptance treats it as passing.  An operator
with no free routes gets no signal from doctor that context lift is blocked.

A blocked result is a legitimate outcome (no eligible free routes, gateway
down), not a crash.  It should surface as a **warning** in doctor, not a
failure, so the operator can see _why_ there are no free routes without blocking
a healthy install.

## What Changes

- `verdict bench context-lift --json` exits **2** when the conclusion is
  `blocked` and the bench actually ran (not `--dry-run`).  systemd's oneshot
  unit records exit code 2 as a failed result, so `timer_state()` returns
  `{"state": "failed", "result": "exit-code"}`.
- `verdict doctor`'s context-lift row now reads the last bench receipt from
  `$VERDICT_HOME/context-lift/latest.json`.  When the timer state is `failed`
  with result `exit-code` **and** the receipt shows `conclusion == blocked`, the
  row is **warning** (not failure), with the `block_reason` in the detail text.
- The service and dispatch code comments now describe the exit-2 contract.

## Capabilities

### Modified Capabilities

- **context-lift bench**: a blocked run exits 2; a passing run still exits 0;
  a crash exits non-zero (unchanged).
- **doctor context-lift row**: distinguishes a blocked bench (warning) from a
  crashed unit (failure) by reading the last bench receipt.

## Impact

- `verdict/commands/dispatch.py` (`_cmd_bench_context_lift`)
- `verdict/doctor_diagnostics.py` (`_context_lift_timer_row`, new
  `_context_lift_last_bench_result` helper)
- `verdict/context_lift_service.py` (comment only)
- No change to admission, routing, or review.

## Evidence boundary

KNOWN: 16 unit tests cover the exit-code path, the receipt reader, and all
doctor row states; they were red on the unfixed code and green on the fix.

NOT AVAILABLE: live acceptance (doctor shows warning on a real install with no
free routes) — tracked as a post-merge task.

## Acceptance criteria

- `verdict bench context-lift --json` exits 2 when conclusion is `blocked`.
- `verdict doctor` shows the context-lift row as `warning` when the timer
  exited with result `exit-code` and the receipt says `blocked`, with the
  `block_reason` in the detail.
- Tests for both scenarios were red on the unfixed code and green on the fix.
