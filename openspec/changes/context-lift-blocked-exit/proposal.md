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
- The bench stamps a UTC `finish_time` (ISO-8601 with offset) into the receipt
  it writes, so a receipt can be tied to one run.
- `verdict doctor`'s context-lift row now reads the last bench receipt from
  `$VERDICT_HOME/context-lift/latest.json`.  When the timer state is `failed`
  with result `exit-code`, the unit's `ExecMainStatus` is `2`, the receipt shows
  `conclusion == blocked`, **and** the receipt's `finish_time` is not earlier
  than the unit's `ExecMainStartTimestamp`, the row is **warning** (not
  failure), with the `block_reason` in the detail text.  Otherwise it stays a
  failure, so a stale blocked receipt cannot hide a crash.
- The unit's timestamps are requested from systemd as explicit UTC
  (`--timestamp=us+utc`), so the comparison is correct on a host in any
  timezone.  A receipt time without a UTC offset is invalid.
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
  `finish_time`)
- `verdict/doctor_diagnostics.py` (`_context_lift_timer_row`, new
  `_context_lift_last_bench_result`, `_parse_systemd_utc`,
  `_parse_receipt_time` and `_receipt_is_from_invocation` helpers)
- `verdict/context_lift_service.py` (`timer_state` returns `exec_main_status`
  and `exec_main_start_timestamp`)
- `verdict/metadata_service.py` (`_show` requests `ExecMainStartTimestamp` and
  takes an optional `timestamp` format; the default command is unchanged)
- No change to admission, routing, or review.

## Evidence boundary

KNOWN: the unit tests in `tests/test_context_lift_blocked_exit.py` cover the
exit-code path, the receipt reader, every doctor row state, the invocation
binding, a non-UTC host, microsecond ordering, a systemd that rejects
`--timestamp`, and receipt times without an offset.  Each fix has a test that
fails with that fix reverted.  `systemctl --timestamp=us+utc` was checked on
systemd 249, where it prints explicit UTC under any `TZ`; `--timestamp=unix`
is rejected there, so it is not used.

NOT AVAILABLE: live acceptance (doctor shows warning on a real install with no
free routes) — tracked as a post-merge task.

## Acceptance criteria

- `verdict bench context-lift --json` exits 2 when conclusion is `blocked`.
- `verdict doctor` shows the context-lift row as `warning` when the timer
  exited with result `exit-code`, `ExecMainStatus` is `2`, and the receipt says
  `blocked` and was written by that invocation, with the `block_reason` in the
  detail.  A crash, or a receipt from an earlier run, stays a failure.
- Tests for both scenarios were red on the unfixed code and green on the fix.
