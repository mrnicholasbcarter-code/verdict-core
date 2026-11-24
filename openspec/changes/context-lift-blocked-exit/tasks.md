# Tasks

## 1. Implementation

- [x] 1.1 Failing tests: blocked bench exits 2 (not 0); passing bench exits 0; dry-run exits 0
- [x] 1.2 `_cmd_bench_context_lift` exits 2 when conclusion is `blocked` and `execute` is True
- [x] 1.3 Failing tests: doctor row is `warning` when timer failed with exit-code and receipt says blocked; `warning` detail includes block_reason; other timer states are unaffected
- [x] 1.4 Add `_context_lift_last_bench_result` helper and extend `_context_lift_timer_row` to inject and use it
- [x] 1.5 Update comments in `context_lift_service.py` to document exit-2 contract
- [x] 1.6 Failing tests, then fix: a stale blocked receipt must not turn a crash into a warning; require `ExecMainStatus` 2 and a receipt `finish_time` not earlier than `ExecMainStartTimestamp`; stamp `finish_time` in the receipt
- [x] 1.7 Failing tests, then fix: a `latest.json` that is valid JSON but not an object is treated as no receipt
- [x] 1.8 Failing tests, then fix: use `ExecMainStartTimestamp` (not `ActiveEnterTimestamp`, which is `n/a` for a one-shot unit) so a first blocked run is a warning
- [x] 1.9 Failing tests, then fix: request UTC timestamps (`--timestamp=us+utc`) with a fallback for a systemd that rejects the flag; accept only a `UTC` start time
- [x] 1.10 Failing tests, then fix: a receipt `finish_time` without a UTC offset is invalid and never compared
- [ ] (post-merge) `verdict doctor` shows context-lift row as `warning` on a real install where no free routes are available
