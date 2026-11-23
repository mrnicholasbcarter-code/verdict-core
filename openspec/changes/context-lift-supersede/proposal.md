## Why

`verdict-context-lift.timer` runs `verdict bench context-lift --json` daily
using a persistent `lift-memory.db` under the working directory. Each run
generates a new random token and writes it to `patterns/lift_fact` via
`ingest_lift_fact`. The first run succeeds; every subsequent run fails with
`ContextIntelligenceError("secret_refused", "contradiction_requires_explicit_supersession")`
because `ingest_lift_fact` did not pass a `supersedes` reference when an active
record already existed. `memory_gate._evaluate` treats the new value as an
unauthorized contradiction and denies the write.

This breaks `verdict doctor` and the hourly acceptance run after the first day.

## What Changes

- **`verdict/context_lift.py` — `ingest_lift_fact`**: before writing, look up
  the active `patterns/lift_fact` record via `gate.plane.get`. When one exists,
  pass `supersedes=<its record_id>` in the write request so `memory_gate`
  accepts the new value as an intentional update rather than a contradiction.
- **`verdict/context_lift.py` — `ingest_lift_fact`**: change the non-secret
  ingest failure path from error code `secret_refused` to `ingest_refused`,
  matching the actual refusal category. `git grep` confirms no test asserts
  the old code on this path; lines 137 and 165 (genuine secret writes) are
  unchanged.
- **`tests/test_context_lift.py`**: add `test_repeated_ingest_supersedes_previous`
  to verify two sequential ingest calls with distinct tokens both succeed and
  the second supersedes the first.

## Capabilities

### Modified Capabilities

- **context-lift bench**: `ingest_lift_fact` now accepts repeated calls against
  the same plane with different tokens. The second call supersedes the first
  record rather than raising a contradiction error. The first-run behaviour
  (no active record, no `supersedes`) is unchanged.

## Impact

- The daily timer and doctor are no longer broken after the first run.
- No behaviour change on the first run (no active record → `supersedes=None`).
- No change to secret-detection logic on lines 137 and 165 of `context_lift.py`.
- Blast radius: `verdict/context_lift.py` and its focused tests only.

## Evidence boundary

KNOWN: `verdict/memory_gate.py` line 337 rejects a write whose value differs
from the active record unless `request.supersedes` names the active record's id
or key. KNOWN: `ingest_lift_fact` did not pass `supersedes` on any call prior
to this fix. KNOWN: `MemoryRecord.supersedes: str | None` exists and
`MemoryPlane.get` returns the active record or `None`. INFERRED: the fix
restores idempotent daily runs; the timer's first run still succeeds because
`get` returns `None` on a fresh db. NOT AVAILABLE until a live run: verification
that `verdict doctor` goes green after the next daily timer execution on
installed main.

## Acceptance criteria

- `test_repeated_ingest_supersedes_previous` passes; running it before the fix
  produces `ContextIntelligenceError: contradiction_requires_explicit_supersession`.
- Existing secret-write tests (lines 137, 165) are unchanged and pass.
- Full test suite green.
- On installed main, the next daily context-lift run and acceptance run show
  doctor green (post-merge live check).
