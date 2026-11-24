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
- The bench records the `$INVOCATION_ID` that systemd passes to the unit's
  processes as `invocation_id` in its receipt (`null` outside a unit, as in a
  manual or `--dry-run` run).  systemd gives every process of one runtime cycle
  of a unit the same ID and reports it as the unit's `InvocationID`.
- `verdict doctor`'s context-lift row now reads the last bench receipt from
  `$VERDICT_HOME/context-lift/latest.json`.  When the timer state is `failed`
  with result `exit-code`, the unit's `ExecMainStatus` is `2`, the receipt shows
  `conclusion == blocked`, the receipt's `invocation_id` equals the service's
  `InvocationID`, **and** the timer's own `ActiveState` is `active`, the row is
  **warning** (not failure), with the `block_reason` in the detail text.
  Otherwise it stays a failure, so a stale blocked receipt, or a receipt that a
  later manual run wrote, cannot hide a crash or a usage-error exit, and a
  stopped timer is not reported as merely blocked.
- A `latest.json` that is valid JSON but not an object is treated as no receipt.
- The service and dispatch code comments now describe the exit-2 contract.

## Capabilities

### Modified Capabilities

- **context-lift bench**: a blocked run exits 2; a passing run still exits 0;
  a crash exits non-zero (unchanged).
- **doctor context-lift row**: distinguishes a blocked bench (warning) from a
  crashed unit (failure) by reading the last bench receipt.

## Impact

- `verdict/commands/dispatch.py` (`_cmd_bench_context_lift`: exit code and
  `invocation_id`)
- `verdict/doctor_diagnostics.py` (`_context_lift_timer_row`, new
  `_context_lift_last_bench_result` and `_receipt_is_from_invocation` helpers)
- `verdict/context_lift_service.py` (`timer_state` returns `exec_main_status`,
  `invocation_id` and `timer_active_state`)
- `verdict/metadata_service.py` (`_show` also requests `InvocationID`)
- No change to admission, routing, or review.

## Evidence boundary

KNOWN: the unit tests in `tests/test_context_lift_blocked_exit.py` cover the
exit-code path, the receipt reader, every doctor row state, the receipt's
`invocation_id`, a receipt from another run, a manual run after a usage-error
exit, a unit with no `InvocationID`, and a stopped or failed timer.  Each fix has
a test that fails with that fix reverted.  `$INVOCATION_ID` is documented in
systemd.exec(5); on systemd 249 the unit's `InvocationID` persists after the run
and matches the journal's invocation field for that run.

NOT AVAILABLE: live acceptance (doctor shows warning on a real install with no
free routes, and the receipt of a real unit run carries the unit's
`InvocationID`) — tracked as a post-merge task.

## Acceptance criteria

- `verdict bench context-lift --json` exits 2 when conclusion is `blocked`.
- `verdict doctor` shows the context-lift row as `warning` when the timer
  exited with result `exit-code`, `ExecMainStatus` is `2`, the receipt says
  `blocked` and carries the service's `InvocationID`, and the timer is active,
  with the `block_reason` in the detail.  A crash, a receipt from another run, or
  a stopped timer stays a failure.
- Tests for both scenarios were red on the unfixed code and green on the fix.
