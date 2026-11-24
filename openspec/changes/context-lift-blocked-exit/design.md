# context-lift-blocked-exit design

## Context

The context-lift bench runs as a oneshot systemd service.  When no free-tier
route is eligible, the bench writes `{"conclusion": "blocked", ...}` to
`$VERDICT_HOME/context-lift/latest.json` and exits 0.  systemd marks the unit
as `Success`, `timer_state()` returns `{"state": "inactive", ...}`, and
`verdict doctor` renders the row as `ok`.

The operator gets no signal from doctor when no free routes exist.  A warning
row with the block reason gives actionable information without blocking an
otherwise healthy install.

## Goals / Non-Goals

**Goals:**
- Blocked bench (no eligible free routes) exits 2 so the unit records a
  non-success result and doctor can distinguish it from a passing run.
- Doctor surfaces a blocked bench as a `warning` row with the `block_reason`
  from the receipt, not as a failure that would block a healthy install.

**Non-Goals:**
- Changing the blocked conclusion itself or the conditions that produce it.
- Surfacing block state through any path other than doctor (no UX changes to
  `verdict bench` output beyond the exit code).
- Altering admission, routing, or any other verdict subsystem.

## Decisions

**Exit code 2** (not 1) distinguishes a blocked result from a process crash
(which typically exits 1 or non-zero from an exception).  systemd treats any
non-zero exit of a oneshot service as `failed`, so the timer unit records
`result=exit-code`.

**Receipt read in doctor** rather than parsing timer state further.  The
receipt is already written at `$VERDICT_HOME/context-lift/latest.json` on every
bench run; reading it in a small helper keeps the doctor edit minimal and easy
to merge alongside the O‍penCode adapter agent's concurrent edits.

**Tie the receipt to the failed invocation.** The receipt file is overwritten
on every run, so a blocked receipt from an earlier day would otherwise turn a
later crash into a warning.  A warning therefore needs all of: `ExecMainStatus`
is `2` (argparse usage errors and crashes exit differently), `conclusion` is
`blocked`, and the receipt's `finish_time` is not earlier than the unit's
`ExecMainStartTimestamp`.  `ActiveEnterTimestamp` is the wrong field: a
`Type=oneshot` unit exits while still activating, so on the real unit it reads
`n/a` after a run (checked with `systemctl --user show` on systemd 249) and a
first blocked run would stay a failure.  `ExecMainStartTimestamp` persists after
the process exits and is specific to the last invocation.

**Compare in UTC.** `systemctl show` prints timestamps in the host's zone by
default (`EST`, `CET`, ...).  Doctor asks for `--timestamp=us+utc`, which
prints `Day YYYY-MM-DD HH:MM:SS.UUUUUU UTC` under any `TZ` (verified on systemd
249 with `TZ=America/New_York` and `Europe/Berlin`).  The flag is opt-in per
call, so the metadata-refresh timer's output is unchanged.  Alternatives
rejected: `--timestamp=unix` does not exist on 249; `ExecMainStartTimestampMonotonic`
is boot-relative and converting it to wall time drifts across suspend and clock
steps.  systemd older than v246 rejects `--timestamp` and prints nothing, which
would read as a healthy unit, so `_show` asks again without the flag.  The
parser accepts only a value with a `UTC` suffix; a local-zone value is unusable
and the row stays a failure.  The weekday name is skipped because it is
locale-dependent.

**Receipt time must carry an offset.** The bench writes `finish_time` as
`datetime.now(timezone.utc).isoformat()`.  A value without an offset is treated
as invalid and never compared, because comparing it with an aware start time
raises `TypeError` and guessing a zone could let a stale receipt through.

**Warning, not failure.** A blocked bench means no free routes are available —
expected in a paid-only setup.  Failing doctor for this would block `verdict
setup` acceptance on those installs.

## Risks / Trade-offs

The receipt is read on every doctor run.  The helper catches all exceptions so
a missing, corrupt or non-object receipt, and any missing or unusable
timestamp, degrades gracefully (falls back to failure, not warning) rather than
crashing doctor.

## Security / trust implications

The receipt is a local file written by this process.  No external data is read.

## Routing / context / memory implications

No impact on routing decisions.

## Migration / rollback

The exit code change is backward-compatible: existing timer units record the
new exit code from their next run.  No re-install of the timer is required.

## ADR impact

No ADR changes required.
