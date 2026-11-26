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

1. **A proof object, not a flag.** `ReceiptStore.verified_scope(scope)` walks once, raises
   `ReceiptIntegrityError` if the chain is broken, and returns a `VerifiedScope` naming the store
   and scope. `get_receipt` and `query_receipts` take it as keyword-only `verified`, and skip
   their own walk only when it names that store and that scope. Anything else (none, another
   scope, another store) walks as before. `_current` creates one per call and drops it.
2. **Same failure as before.** `get_receipt` walked first, so a broken chain raised from
   `_current` before any row was read, also for an unknown key. `verified_scope` raises the same
   error at the same point. The old `return None` after a second walk could only be reached by a
   race between two walks and is gone with the second walk.
3. **One lane, one thread.** `StateLane` wraps `ThreadPoolExecutor(max_workers=1)`, so jobs run
   in submission order. `submit` queues at the call; `run` awaits it. A shared pool would decide
   requests concurrently and reorder the evidence chain.
4. **The lane is the service's route hop.** `IntelligenceService.route` binds the request
   instant (a context variable) on the loop, then runs `_route` on the lane with a copy of the
   context. The verified assignment travels in the `context` argument. A service with no lane
   (the CLI, orchestration, tests) decides inline, unchanged.
5. **Cancellation never drops queued work.** `run` shields the job from its waiter: a request
   task cancelled while its terminal evidence write is queued still gets the write, in order. A
   stream's terminal bookkeeping (event then observation) is one job. `_EvidenceStreamAdapter`
   awaits an async terminal callback and still accepts a plain one.
6. **Never hold the lane across an upstream call.** The lane runs the decision before the call
   and the bookkeeping after it; streaming and the upstream call stay on the loop.
7. **Lifecycle.** The lifespan creates the lane as its last startup step (a failed startup leaves
   none), attaches it to the service, and on shutdown refuses new jobs, gives queued ones up to
   10 s, then stops the worker without blocking the loop. With no lifespan (tests that set the
   instances by hand) the handlers run the work inline.

## Risks / Trade-offs

- A request task cancelled while it waits on a lane job before its first upstream attempt (only
  at shutdown in practice) can leave a decision row without its terminal event. Before, those
  steps could not be interrupted. The terminal write of a cancelled attempt or stream still lands.
- Work now runs on a second thread beside the loop. The state it touches is already used from
  `to_thread` workers (receipt store: locked writes and per-call connections; health cache: file
  lock), and only the lane thread decides.
- The lane serialises decisions as the loop did. Four concurrent clients each wait for the
  decisions ahead of them; this change does not shorten a decision.

## Security / trust implications

- Verification is not weakened: every `_current` call walks the whole scope, a proof is valid for
  the store and scope it names, and a read that is given none walks as before. The proof is made
  by one method and kept for one call; an out-of-band edit after the walk is caught by the next
  call, as before.
- No change to admission, the proof minted, the permit committed, or any refusal: a denial makes
  no upstream call and mints no proof. Errors raised on the lane surface as the same HTTP
  responses. No new credential or endpoint.

## Routing / context / memory implications

- None to what is chosen or recorded: the same decision runs, now on another thread with the
  request's context copied in. Context packs, memory and ranking are untouched.

## Migration / rollback

- No data migration; the receipt schema and the evidence content are unchanged.
- Roll back by reverting the change: the readers keep working with no proof argument, and the
  service decides inline when no lane is attached.

## ADR impact

- None. ADR-001 (admission narrows, never re-admits) and ADR-053 (assigned lane) are unchanged;
  the assigned lane's logic is not touched, only the thread it runs on.
