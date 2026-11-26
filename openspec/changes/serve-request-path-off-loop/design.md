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
   the row, on the lane thread, so nothing has to be submitted after the lane has closed or its
   worker has been told to stop; if the job had already finished, the event is queued as cleanup
   (below). A task cancelled while a later step (the permit commit, a fallback event) is queued
   queues the same event as cleanup: `StateLane.submit_cleanup` is taken by a closed lane while
   its worker runs (behind the job the task waited on; the close waits for it), runs at once on
   the calling thread once the worker has been told to stop, and the event is written directly
   when the app has no lane left. Every write to a row that already exists (attempt events, the
   terminal event of an attempt, a denial, a stream or a buffered response) goes the same way
   (`_write_row_on_lane`), so a request that outlives the close, or is cancelled during its
   upstream call after it, still records how it ended; only new work (the decision, the row's
   start, the permit commit) is refused by a closed lane. A stream's terminal bookkeeping (event and observation) and
   the buffered response's are each one job. `_EvidenceStreamAdapter` awaits an async terminal
   callback and still accepts a plain one.
6. **Never hold the lane across an upstream call.** The lane runs the decision before the call
   and the bookkeeping after it; streaming and the upstream call stay on the loop.
7. **Lifecycle.** The lifespan creates the lane as its last startup step (a failed startup leaves
   none), attaches it to the service, and on shutdown, however the app ended, refuses new jobs,
   waits up to 10 s for the queued ones, then tells the worker to stop. The bound ends the wait,
   not the work: jobs still queued run to completion on the worker thread, so no terminal write
   is dropped, and the loop is never held. With no lifespan (tests that set the instances by
   hand) the handlers run the work inline.

## Risks / Trade-offs

- Once a close has given up on a stuck job, the cancelled terminal event of a task cancelled then
  is written on the thread of the cancelled task (one store append, on the event loop) and can
  land before the stuck job's own write. The request was cancelled, so `cancelled` is its true
  outcome, and the store refuses a second, different terminal outcome for the row.
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
