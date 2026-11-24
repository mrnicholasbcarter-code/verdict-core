# Tasks

## 1. Implementation

- [x] 1.1 Failing tests: blocked bench exits 2 (not 0); passing bench exits 0; dry-run exits 0
- [x] 1.2 `_cmd_bench_context_lift` exits 2 when conclusion is `blocked` and `execute` is True
- [x] 1.3 Failing tests: doctor row is `warning` when timer failed with exit-code and receipt says blocked; `warning` detail includes block_reason; other timer states are unaffected
- [x] 1.4 Add `_context_lift_last_bench_result` helper and extend `_context_lift_timer_row` to inject and use it
- [x] 1.5 Update comments in `context_lift_service.py` to document exit-2 contract
- [x] 1.6 Failing tests, then fix: a warning requires `ExecMainStatus` 2, so a crash next to a stale blocked receipt stays a failure
- [x] 1.7 Failing tests, then fix: a `latest.json` that is valid JSON but not an object is treated as no receipt
- [x] 1.8 Failing tests, then fix: the bench records `$INVOCATION_ID` in the receipt and doctor requires it to equal the unit's `InvocationID`, so a receipt from another run (an earlier one, or a manual or `--dry-run` run after a usage-error exit) cannot hide a unit failure, and a first blocked run is still a warning
- [x] 1.9 Failing tests, then fix: a stopped or failed timer stays a failure even with a matching blocked receipt; `timer_state` keeps the timer's own `ActiveState`
- [ ] (post-merge) `verdict doctor` shows the context-lift row as `warning` on a real install where no free routes are available, and the receipt of a real run of the installed unit carries an `invocation_id` equal to the unit's `InvocationID`
