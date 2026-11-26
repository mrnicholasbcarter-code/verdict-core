# Proposal: serve decides and records a request off the event loop, and verifies the evidence chain once per read

## Why

With one OpenCode worker the installed `verdict serve` used a full core and `/health` took 3 to
more than 10 s, so the hourly acceptance probe (2 s) went red (#1354) and no self-build can run.
KNOWN: on a copy of the real 829 MB receipt store one assigned request cost 43 to 48 s of event
loop CPU and `/health` answered in 16 s (median) to 32 s (worst): 15 of 17 chain walks were of
the 818-row `server-auth` scope (three per `DurableEvidenceStore._current` call), and the route
decision, evidence writes, permit commit and observation ran on the loop. NOT AVAILABLE until
merged: `/health` on the installed serve under one OpenCode worker.

## What Changes

- `DurableEvidenceStore._current` walks the scope's chain once per call and hands the proof to
  `get_receipt` and `query_receipts` (keyword `verified`, from `ReceiptStore.verified_scope`).
  Every call still verifies the whole scope; a broken chain still raises `ReceiptIntegrityError`
  before any row is read; other callers of the readers verify exactly as before.
- `verdict serve` owns one serial worker lane (`StateLane`, one thread), created at startup and
  drained at shutdown. The route decision, evidence start and finish, permit commit and dispatch
  observation run on it and are awaited from the handlers.

Not here (operator decision pending): incremental verification on reads, smaller decision
receipts, fewer `HealthCache` constructions, pruning the receipt store.
## Capabilities
### New Capabilities
- `serve-request-path-off-loop`: one chain verification per evidence read; the request path's
  state work on one serial lane off the event loop.
## Impact
`verdict/receipt_store.py`, `verdict/evidence.py`, `verdict/intelligence.py`, `verdict/api.py`
and their tests. No change to admission, ranking, what is recorded or verified, or any response.

## Acceptance criteria
- One `_current` call makes one chain walk (three before); tampering anywhere still fails reads.
- `/health` and the cheap 401 answer in under 0.2 s while a route decision is held.
- Evidence writes keep one serial order; denials make no upstream call and mint no proof.
- On installed main, `/health` stays under 2 s while one OpenCode worker runs (post-merge).
