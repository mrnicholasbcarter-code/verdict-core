# BOD-340 slice: Prime dispatch refuses an invalid or stale packet

## Why
The Prime dispatch path (`.prime/agent/skills/verdict-dispatch/scripts/runtime_bridge.py`
`start` -> `python -m verdict.worker_runtime`) launched workers from a bare prompt. The
`hydrate-context` and `verdict-dispatch` skills told the controller to validate the packet
and recheck the Linear revision, base and lease first, but no code called `validate_packet`
on that path. Only `scripts/prime_workflow.py` `check-proof`/`open-pr` (ship side) did.
A guide alone is not enforcement (MASTER finish-line plan section 7; BOD-340 result 1).

## What Changes
- `scripts/prime_workflow.py` gains `validate_dispatch(packet, repo, *, issue_updated_at,
  state)`: runs `validate_packet`, then refuses when the packet worktree is not `repo`, the
  Linear `updatedAt` read just before dispatch differs from the packet's, the packet branch
  is not checked out, `base_sha` is not an ancestor of HEAD, or the issue lease is absent,
  inactive, stale, for another attempt or generation (`require_lease_for_pr`), or held for
  another worktree or branch.
- The bridge requires `packet` and `issue_updated_at`, runs the controller checkout's
  `validate_dispatch` before creating a worker-run directory or launching any process, and
  binds `packet`, `attempt_id` and `lease_generation` into the worker `config.json`.
- A caller-coverage test fails if any production file launches `verdict.worker_runtime`
  without calling `validate_dispatch` first.

## Capabilities
### New Capabilities
- `prime-dispatch-packet-gate`: dispatch-time packet validity and currency.

## Impact
- Breaking for bridge callers: `start` now requires `packet=` and `issue_updated_at=`.
  The `verdict-dispatch` skill example is updated.
- ADR disposition: enforces the accepted ADR-031 decision ("All attempts bind issue,
  worktree, base SHA, exact target and attempt ID"; lease generation is the fence token).
  No new architectural decision, so no new ADR.
- Out of scope (owned elsewhere in BOD-340): approach review from a different family,
  OpenSpec AC->scenario->test mapping and ADR disposition as machine-checked packet fields,
  goal-based entry points (`verdict orchestrate`/`run`/`autodev`) that have no packet.
- Not claimed: live acceptance on the operator host. DoD evidence stays with BOD-340.
