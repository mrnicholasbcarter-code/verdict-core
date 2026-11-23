# Design: context-lift-supersede

## Context

The daily `verdict-context-lift.timer` runs `verdict bench context-lift --json`
against a persistent `lift-memory.db` under the working directory. On each run
a new random token is generated and written to `patterns/lift_fact` via
`ingest_lift_fact`. `memory_gate._evaluate` guards against value contradictions
on the same namespace/key pair: if an active record exists and the new write does
not name it in `request.supersedes`, the gate denies the write with
`contradiction_requires_explicit_supersession`. Without an explicit `supersedes`
reference, every run after the first fails.

## Goals / Non-Goals

**Goals:**
- Fix `ingest_lift_fact` so the daily timer succeeds on runs 2–N as well as run 1.
- Preserve the secret-detection guard (lines 137 and 165 of `context_lift.py`).
- Distinguish the non-secret ingest failure code from the secret-write code.

**Non-Goals:**
- Changing `memory_gate` policy or the contradiction semantics.
- Altering the timer schedule or the bench command interface.
- Fixing the follow-up issue where the first run reports
  `conclusion: blocked, block_reason: live_surface_blocked` but exits 0
  (separate concern, noted in the PR body).

## Decisions

1. Look up the active record via `gate.plane.get("patterns", "lift_fact")` before
   the write, inside `ingest_lift_fact`. This keeps the fix local to the function
   that owns the semantic (planting a fresh token each cycle) and avoids changing
   `MemoryWriteRequest` defaults or any gate policy.
2. Use `record_id` (not `key`) as the `supersedes` value: `record_id` is
   unambiguous and already returned by `plane.get`. Both are accepted by
   `memory_gate._evaluate` (checked against `{current.record_id, current.key}`).
3. Change the non-secret failure code from `secret_refused` to `ingest_refused`.
   `git grep` confirms no test file asserts the old code on this path; the frozen
   contract spec names `secret_refused` for genuine secret writes, which are
   unchanged on lines 137 and 165.

## Risks / Trade-offs

- Minimal: `plane.get` is a lightweight SQLite read on the same connection. The
  `already_present` branch in `_evaluate` (identical content hash) still fires
  first, so a repeated write of the same token is a no-op — unchanged.
- The fix exposes a one-read-then-write race if the same plane is concurrently
  written from two processes; the daily timer is a single-process unit so this
  is not a live concern.

## Security / trust implications

No change to trust boundaries. `gate.plane.get` reads from the same SQLite file
that the write targets; the returned `record_id` is an internal identifier, not
user input. The secret-detection path (lines 137 and 165) is unchanged.

## Routing / context / memory implications

`ingest_lift_fact` is advisory context infrastructure — it plants a synthetic
fact for the bench, not a routing decision. The fix only affects the daily lift
bench; routing, admission, and all other memory paths are unaffected.

## Migration / rollback

Rollback: revert `ingest_lift_fact` to drop the `existing = gate.plane.get(...)`
lookup and remove `supersedes=...` from the write request. After rollback the
timer will fail again on day 2. No schema changes; existing `lift-memory.db`
files continue to work.

## ADR impact

No ADR required. This is a bug fix to context-pack bench infrastructure, not an
architectural decision.
