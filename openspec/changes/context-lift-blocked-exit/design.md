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

**Tie the receipt to the failed unit run by systemd's invocation ID.** Every
bench run overwrites the receipt file, including a manual or `--dry-run` run, so
a blocked receipt can come from a different run than the unit failure it is
shown with.  Two cases matter: a blocked receipt from an earlier day next to a
later crash, and a unit that exits 2 from an argparse usage error (which also
exits 2) without writing a receipt, followed by an operator's manual blocked run
that writes a newer one.  A time comparison cannot tell the second case apart,
even bounded by the unit's start and exit times, and it brings timezone,
precision and offset handling with it.  A first design that compared the
receipt's `finish_time` with `ExecMainStartTimestamp` was rejected on review for
these reasons.  systemd provides the identifier for exactly this: every process
of one runtime cycle of a unit gets the same `$INVOCATION_ID`, and the unit
reports it as `InvocationID` (systemd.exec(5)).  The bench writes
`$INVOCATION_ID` into the receipt as `invocation_id` (`null` outside a unit) and
doctor requires it to equal the service's `InvocationID`.  `ExecMainStatus` must
still be `2`.  The property persists after the unit exits (checked with
`systemctl --user show` on systemd 249, where it also matches the journal's
invocation field for that run).  systemd older than v232 has neither, so the row
stays a failure there.  IDs are compared trimmed and case-insensitively, and an
absent or empty ID never matches, so `""` cannot equal `""`.

**The timer must still be active.** `timer_state()` reports `failed` whenever
the service failed, even if the timer itself was stopped or failed and will not
fire again.  Doctor keeps the timer's own `ActiveState` and only downgrades to a
warning while it is `active`.

**Warning, not failure.** A blocked bench means no free routes are available —
expected in a paid-only setup.  Failing doctor for this would block `verdict
setup` acceptance on those installs.

## Risks / Trade-offs

The receipt is read on every doctor run.  The helper catches all exceptions so
a missing, corrupt or non-object receipt, and a missing invocation ID on either
side, degrades gracefully (falls back to failure, not warning) rather than
crashing doctor.

## Security / trust implications

The receipt is a local file written by this process and `systemctl show` output.
`$INVOCATION_ID` is an opaque per-run identifier, not a secret.

## Routing / context / memory implications

No impact on routing decisions.

## Migration / rollback

The exit code change is backward-compatible: existing timer units record the
new exit code from their next run.  No re-install of the timer is required.

## ADR impact

No ADR changes required.
