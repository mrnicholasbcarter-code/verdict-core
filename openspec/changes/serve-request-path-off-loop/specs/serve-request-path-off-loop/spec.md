# Spec Delta: serve request path off the event loop

## ADDED Requirements

### Requirement: An evidence read SHALL verify the scope's chain once

`DurableEvidenceStore._current` SHALL walk the whole hash chain of the scope exactly once per
call and SHALL NOT keep the result beyond the call. The walk SHALL happen before any row is read;
a broken chain SHALL raise `ReceiptIntegrityError` there. The readers `get_receipt` and
`query_receipts` SHALL skip their own walk only when given the proof from
`ReceiptStore.verified_scope` for the same store and scope, and SHALL walk as before otherwise.

#### Scenario: One walk per call
- **WHEN** `DurableEvidenceStore.find(evidence_id=...)` runs once on a store with several rows
- **THEN** the scope's chain is walked exactly once, and `append_event` walks twice (one per `_current` call)

#### Scenario: Tampering anywhere in the scope still fails the read
- **WHEN** any row of the scope has a changed payload, chain link or metadata, in the first, a middle or the last position
- **THEN** `find` (by id, by selector, for an unknown id) and `append_event` raise `ReceiptIntegrityError` and append nothing

#### Scenario: A proof is not honoured for another scope or store
- **WHEN** `get_receipt` or `query_receipts` is given a proof made for a different scope or a different store
- **THEN** the reader walks the chain itself

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

#### Scenario: One lane, arrival order
- **WHEN** two requests are in flight and the first decision is held
- **THEN** no two lane jobs overlap, decisions and evidence rows are written in arrival order, and each request's events keep their order

#### Scenario: Request context reaches the lane
- **WHEN** a worker request is decided on the lane
- **THEN** the request instant bound for that request and the verified assignment are visible to the decision, and the assigned route is the one dispatched

#### Scenario: Denials and errors are unchanged
- **WHEN** a request is denied, or the decision raises `ExecutionPathError` or another error
- **THEN** no upstream call is made and no proof is minted for a denial, `ExecutionPathError` is the same 400 and another error the same 500

#### Scenario: Cancelled waiter, drained shutdown
- **WHEN** a request that queued a terminal evidence write is cancelled while the lane is busy, or the app shuts down with jobs queued
- **THEN** the write still runs in order, and shutdown refuses new jobs and drains the queued ones within its bound
