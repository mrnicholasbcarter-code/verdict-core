# Spec Delta: serve request path off the event loop

## ADDED Requirements

### Requirement: An evidence read SHALL verify the scope's chain once

`DurableEvidenceStore._current` SHALL walk the whole hash chain of the scope exactly once per
call and SHALL NOT keep the result beyond the call. The walk SHALL happen before any row is read;
a broken chain SHALL raise `ReceiptIntegrityError` there. The reads of that call SHALL be made
through `ReceiptStore.verified_reads(scope)`, whose reads end with the call. The public readers
`get_receipt` and `query_receipts` SHALL take no skip argument and SHALL walk on every call.

#### Scenario: One walk per call
- **WHEN** `DurableEvidenceStore.find(evidence_id=...)` runs once on a store with several rows
- **THEN** the scope's chain is walked exactly once, and `append_event` walks twice (one per `_current` call)

#### Scenario: Tampering anywhere in the scope still fails the read
- **WHEN** any row of the scope has a changed payload, chain link or metadata, in the first, a middle or the last position
- **THEN** `find` (by id, by selector, for an unknown id) and `append_event` raise `ReceiptIntegrityError` and append nothing, and `verified_reads` raises before it yields

#### Scenario: Verified reads do not outlive their call
- **WHEN** the reads yielded by `verified_reads` are used after its block ends
- **THEN** they raise, and the public readers, even inside a block, walk the chain themselves

### Requirement: The request path's state work SHALL run on one serial lane off the event loop

`verdict serve` SHALL own one serial worker lane, created at startup and drained at shutdown, and
SHALL run on it the route decision (including the assigned lane), the evidence start and finish,
the dispatch permit commit and the dispatch observation, awaiting each from the handler. Jobs
SHALL run one at a time in the order submitted, with the submitting request's context copied in.
The lane SHALL NOT be held across an upstream call. A job queued by a request that is then
cancelled SHALL still run.

#### Scenario: /health and cheap refusals answer during a decision
- **WHEN** a request's route decision is held inside a slow function
- **THEN** `GET /health` and an unauthenticated `GET /v1/models` (401) answer in under 0.2 s, and the held request then completes and is dispatched upstream once

#### Scenario: One lane, submission order
- **WHEN** two requests are in flight and the first decision is held
- **THEN** no two lane jobs overlap, lane jobs run in the order they were submitted, and each request's own events keep their order

#### Scenario: Request context reaches the lane
- **WHEN** a worker request is decided on the lane
- **THEN** the request instant bound for that request and the verified assignment are visible to the decision, and the assigned route is the one dispatched

#### Scenario: Denials and errors are unchanged
- **WHEN** a request is denied, or the decision raises `ExecutionPathError` or another error
- **THEN** no upstream call is made and no proof is minted for a denial, `ExecutionPathError` is the same 400 and another error the same 500

#### Scenario: A cancelled waiter does not drop its job, and a started row is ended
- **WHEN** a request task is cancelled while its terminal evidence write is queued behind a busy lane, or while its decision row is being started or its permit committed
- **THEN** the queued write still runs in order, and the cancelled request's decision row gets a `cancelled` terminal event, with no upstream call made

#### Scenario: A row is ended even when the lane closed first
- **WHEN** the lane is closed (its worker still running, or told to stop after the close gave up on a held job) and then a request task is cancelled while its start job or its permit commit is still held
- **THEN** the row gets exactly one `cancelled` terminal event: the start job ends its own row when it has written it, and a cleanup for a later step is queued behind the held job while the worker runs, or run at once when it has stopped

#### Scenario: Shutdown
- **WHEN** the app ends, normally or with an error, with jobs queued
- **THEN** the lane refuses new jobs, waits up to its bound for the queued ones and for any cleanup accepted meanwhile, stops its worker even if that wait is cancelled, runs jobs still queued at the bound to completion on the worker thread, and the lifespan clears its references even if its own teardown is cancelled
