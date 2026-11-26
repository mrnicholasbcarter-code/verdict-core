# Design

## Context

`verdict serve` handles a worker request in one coroutine on the event loop. The route decision
(`IntelligenceService._route`, an `async def` with no `await`), the evidence writes, the dispatch
permit commit and the dispatch observation are synchronous file and SQLite work, so they hold the
loop. Each evidence read (`DurableEvidenceStore._current`) also walked the whole `server-auth`
hash chain three times (an explicit check plus one inside each of the two readers), and the chain
only grows (it was 818 rows, 158 MB). Measured on a copy of the real store: about 43 s per
request, `/health` 16 to 32 s.

## Goals / Non-Goals

**Goals:**
- One full chain verification per `_current` call, with no cache that outlives the call.
- The request-path state work runs on one serial worker lane, so `/health` and the cheap
  refusals answer while a request is being decided, and decisions keep today's order.

**Non-Goals:**
- Incremental verification on reads, smaller assigned-decision receipts, fewer `HealthCache`
  constructions, pruning the receipt store. They change what is verified or recorded, or need an
  operator decision. They are not here.
- Making a request cheaper in CPU or latency beyond the removed walks. The lane frees the loop,
  not the CPU: with four clients the decisions still queue (each about 16 s).
- Moving every synchronous call on the relay path. `revocation_epoch`, `record_revocation`,
  `request_rescreen`, the failure feedback, the attempt ledger and the outcome log stay on the
  loop (each is one small file read or append).

## Decisions

1. **A scoped read block, not a flag or a token.** `ReceiptStore.verified_reads(scope)` is a
   context manager: it walks the scope once, raises `ReceiptIntegrityError` if the chain is
   broken (before yielding), then yields a `VerifiedReads` whose two readers have no scope
   argument and no walk. The object stops working when the block ends, so it cannot be kept and
   used after it. Inside the block it is not a snapshot: the walk and the reads are not one
   transaction, as when the readers walked separately. `get_receipt` and `query_receipts` keep
   their signatures and always walk; they have no skip argument for a caller to forge.
2. **Same failure as before.** `get_receipt` walked first, so a broken chain raised from
   `_current` before any row was read, also for an unknown key. `verified_reads` raises the same
   error at the same point (on entry to the block). The old `return None` after a second walk
   could only be reached by a race between two walks and is gone with the second walk. The walk
   and the reads were never one transaction, and still are not.
3. **One lane, one thread.** `StateLane` wraps `ThreadPoolExecutor(max_workers=1)`, so jobs run
   in submission order. `submit` queues at the call; `run` awaits it. A shared pool would decide
   requests concurrently and reorder the evidence chain.
4. **The lane is the service's route hop.** `IntelligenceService.route` binds the request
   instant (a context variable) on the loop, then runs `_route` on the lane with a copy of the
   context. The verified assignment travels in the `context` argument. A service with no lane
   (the CLI, orchestration, tests) decides inline, unchanged.
5. **Cancellation never drops queued work, and a started row is always ended.** `run` shields
   the job from its waiter: a request task cancelled while its terminal evidence write is queued
   still gets the write, in order. A task cancelled while the decision row is being started hands
   the ending of the row to the start job itself (`_StartJob`): if the waiter gave up before the
   job finished, the job writes a `<surface>_cancelled` terminal event as soon as it has written
   the row, on the lane thread (the row does not exist before that, so there is nothing to
   queue); if the job had already finished, the event is queued as cleanup (below). A task
   cancelled while a later step (the permit commit, a fallback event) is queued queues the same
   event as cleanup. Every write to a row that already exists (attempt events, the terminal
   event of an attempt, a denial, a stream or a buffered response) goes the same way
   (`_write_row_on_lane`), so a request that outlives the close, or is cancelled during its
   upstream call after it, still records how it ended; only new work (the decision, the row's
   start, the permit commit) is refused by a closed lane. A stream's terminal bookkeeping (event
   and observation) and the buffered response's are each one job. `_EvidenceStreamAdapter`
   awaits an async terminal callback and still accepts a plain one.
   A cleanup (`StateLane.submit_cleanup`) is taken by a closed lane, queued behind every earlier
   job and run on the lane's worker, never on the caller's thread, which is a handler on the
   event loop: the close's bound ends the wait, not the queue. The worker retires (shuts down)
   only when the lane is closed and nothing is pending, so a close that gave up on a held job
   leaves the worker running its queue and any cleanup that arrives, in order; a cleanup that
   arrives after the worker retired gets a fresh worker, which retires again when idle. The
   lifespan clears the app's lane references at shutdown (new work then has no lane), but keeps
   a lane that has not drained reachable for cleanup only (`draining_state_lane_instance`), so a
   handler that is still finishing writes behind the held job instead of inline. A cleanup that
   cannot be queued (no worker can be started) fails its future and is never run inline.
6. **Never hold the lane across an upstream call.** The lane runs the decision before the call
   and the bookkeeping after it; streaming and the upstream call stay on the loop.
7. **Lifecycle.** The lifespan creates the lane as its last startup step (a failed startup leaves
   none), attaches it to the service, and on shutdown, however the app ended, refuses new jobs,
   waits up to 10 s for the queued ones, then lets the worker retire once nothing is pending. The
   bound ends the wait, not the work: jobs still queued run to completion on the worker thread,
   so no terminal write is dropped, and the loop is never held. With no lifespan (tests that set the instances by
   hand) the handlers run the work inline.

## Risks / Trade-offs

- A write to an existing row made when no lane is running any job runs on the caller's thread:
  an app whose lifespan never started (tests that set the instances by hand), or one that has
  shut down and drained. Nothing is queued for it to overtake, and after the lifespan has ended
  there is no `/health` left to stall.
- The lane references are process-wide, like `intelligence_instance` and the other globals the
  lifespan replaces. A handler that outlives its own lifespan and then meets a later lifespan in
  the same process resolves its lane, like its service, from the later one. `verdict serve` has
  one lifespan per process (`uvicorn.run`; `reload=True` restarts the process), so this arises
  only in a test that starts the app twice, where no request outlives its lifespan. Lane
  ownership per request is not built.
- A close that gave up on a stuck job leaves its worker running past the lifespan, so the
  process exit waits for that job, as it does for any executor thread.
- Work now runs on a second thread beside the loop. The state it touches is already used from
  `to_thread` workers (receipt store: locked writes and per-call connections; health cache: file
  lock), and only the lane thread decides.
- The lane serialises decisions as the loop did. Four concurrent clients each wait for the
  decisions ahead of them; this change does not shorten a decision.

## Security / trust implications

- Verification is not weakened: every `_current` call walks the whole scope, the verified reads
  are bound to their store and scope and end with the call, and the public readers have no skip
  argument and walk as before. An out-of-band edit after the walk is caught by the next call, as
  before; between the walk and the reads of one call it is not, exactly as when the readers
  walked separately.
- No change to admission, the proof minted, the permit committed, or any refusal: a denial makes
  no upstream call and mints no proof. Errors raised on the lane surface as the same HTTP
  responses. No new credential or endpoint.

## Routing / context / memory implications

- None to what is chosen or recorded: the same decision runs, now on another thread with the
  request's context copied in. Context packs, memory and ranking are untouched.

## Migration / rollback

- No data migration; the receipt schema and the evidence content are unchanged.
- Roll back by reverting the change: the public readers are unchanged, and the
  service decides inline when no lane is attached.

## ADR impact

- None. ADR-001 (admission narrows, never re-admits) and ADR-053 (assigned lane) are unchanged;
  the assigned lane's logic is not touched, only the thread it runs on.
