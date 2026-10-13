# Live acceptance job: "stays true" is observed, not hoped

## Why
Acceptance commands were run by hand once, at merge time. Nothing re-ran them
afterwards, so a regression (the prober never installed, the sync timer failing
with 401, 0 eligible routes) went unseen for days. CI's acceptance-gates job
only runs fixtures and an offline demo (relabelled as offline checks in
1b1d9417), so it cannot show the live setup still works.

## What Changes
- `verdict acceptance run` runs the A-table commands from the installed Verdict
  (`python -m verdict` with the same interpreter): `doctor --json`,
  `eligibility --json`, `choose --role implement --json`, `route`, one
  `verdict serve` chat request and `harness prime status --json`. It writes
  `$VERDICT_HOME/acceptance/<ts>.json` with pass/fail, a reason and sha256
  output digests for every check, and keeps the newest 48 results.
- The eligibility check lists Verdict's eligible FREE tool routes. It runs an
  independent 2-turn tool check (read_file then write_file) on a bounded sample
  of listed and unlisted live FREE routes. It requires precision >= 95% and
  estimated recall >= 90%. Routes answering 401/402/403/429 are removed, and
  listed routes that are unserved or in a cooling pool count as wrong.
- The job is green only when every check passed and at least one FREE
  tool-capable route is eligible. `choose` without role support is a FAIL
  ("not implemented"), never a skip.
- `verdict acceptance status` shows the newest result.
- `verdict setup services` installs `verdict-acceptance.{service,timer}`
  (hourly, persistent) from the `user_services` registry; `doctor` and
  `setup --check` check it like the other units. A red result exits 3, which
  the unit counts as success; only a crash fails the unit.
- `verdict doctor` gains a "Live acceptance" section: a red result or one older
  than 2 h is an issue; no result yet is a warning.
- Alerts: one email when a job that has been green is red for two runs in a
  row, and one when it recovers. A job that was never green does not alert.
  `--notify off|dry-run|send`; `send` uses the approved Composio
  `GMAIL_SEND_EMAIL` path; `state.json` dedupes. The installed unit uses
  `dry-run` until the job has been green (operator decision 2026-10-13).
- `harness prime status` gains `--json`.

## Impact
Selection, admission and probing are unchanged. The job reads the gateway and
sends at most 2 x 20 small tool-check requests per run to FREE routes.

## Evidence boundary
KNOWN: focused offline tests pass on the story branch. The live drill on the
operator host is recorded in `evidence.md`.
