# Proposal: serve decides and records a request off the event loop, and verifies the evidence chain once per read

## Why

With one OpenCode worker, the installed `verdict serve` used a full core and `/health` took 3 to
more than 10 s, so the hourly acceptance probe (2 s) went red (#1354). No self-build can run
until this is fixed. It advances the mission directly: a worker on a free route only produces
accepted work if serve stays answerable while it works.

KNOWN: on a copy of the real 829 MB receipt store, one assigned request took 43 to 48 s of CPU
on the event loop and `/health` answered in 16 s (median) to 32 s (worst); 15 of its 17 chain
walks were of the 818-row `server-auth` scope, 3 per `DurableEvidenceStore._current` call. KNOWN:
the route decision, the evidence writes, the dispatch permit and the observation are synchronous
and ran on the loop. NOT AVAILABLE until merged: `/health` on the installed serve under one
OpenCode worker.

## What Changes

- `DurableEvidenceStore._current` walks the scope's chain once per call and hands the proof to
  `get_receipt` and `query_receipts` (new keyword `verified`, from `ReceiptStore.verified_scope`).
  Every call still verifies the whole scope; a broken chain still raises `ReceiptIntegrityError`
  before any row is read; the other callers of the two readers verify exactly as before.
- `verdict serve` owns one serial worker lane (`StateLane`, one thread), created at startup and
  drained at shutdown. The route decision, `_start_evidence`, `_finish_evidence`, the permit
  commit and the dispatch observation run on it and are awaited from the handlers.

Not in this change: incremental verification on reads, smaller assigned-decision receipts,
fewer `HealthCache` constructions, pruning the receipt store (operator decision pending).

## Capabilities

### New Capabilities

- `serve-request-path-off-loop`: one chain verification per evidence read; the request path's
  state work on one serial lane off the event loop.

## Impact

`verdict/receipt_store.py`, `verdict/evidence.py`, `verdict/intelligence.py`, `verdict/api.py`,
`tests/test_durable_receipts.py`, `tests/test_serve_event_loop_responsive.py`. No change to
admission, ranking, what is recorded, what is verified, or any HTTP response.

## Acceptance criteria

- One `_current` call makes one chain walk (three before); tampering anywhere in the scope still
  fails every read (tests).
- `/health` and the cheap 401 answer in under 0.2 s while a route decision is held (test).
- Evidence writes keep one serial order for two concurrent requests; denials make no upstream
  call and mint no proof (tests).
- On installed main, `/health` stays under 2 s while one OpenCode worker runs (post-merge).
