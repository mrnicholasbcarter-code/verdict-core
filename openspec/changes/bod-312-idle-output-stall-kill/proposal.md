# Proposal: BOD-312 idle-output stall kill

## Why

A Prime headless worker that stops writing to stdout and stderr holds its
attempt until the whole attempt timeout (default 900 s) runs out. The node
makes no progress for up to fifteen minutes before recovery can move it to
another route. `ocr` review already has an idle-output watchdog (BOD-224);
workers do not.

## What Changes

- `PrimeHeadlessExecutor` reads the worker's stdout and stderr as they arrive.
  When no byte arrives for N seconds it kills the worker's process group
  (SIGTERM, then SIGKILL after the grace period) and returns a terminal with
  `stop_reason: "stalled"` and an error starting `stalled:`.
- N comes from `VERDICT_WORKER_IDLE_SECONDS`, default 300 s. A missing, zero,
  negative or malformed value keeps the default, so the kill cannot be
  switched off by a typo.
- `FailureIntelligence` classifies that terminal as category `stalled`,
  action `REROUTE`, scope `route`, cooldown 120 s. The existing bounded
  recovery then reassigns the node to a different route within the attempt
  budget, and fails closed when the budget is spent.
- The receipt records `failure_category: "stalled"` for the attempt; the
  outcome-record taxonomy maps it to `timeout`.

Out of scope: the rest of event-driven supervision (BOD-312 follow-ups).

## Impact

- Code: `verdict/orchestration/executors.py`, `recovery.py`, `receipt.py`,
  `runtime.py`, `session_feedback.py`, `contracts.py` (comment).
- Tests: `tests/test_orch_idle_stall.py`.
- Docs: `docs/CONFIGURATION.md` (environment variable).
