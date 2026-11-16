# pr-watcher Specification

## Purpose

Provide deterministic ownership for swarm-opened pull requests until they are green and mergeable or explicitly blocked with a durable reason.

## Spec Delta

## ADDED Requirements

### Requirement: Exact-head polling classifies every open configured pull request

The watcher SHALL paginate open pull requests across configured allowlisted repositories, retrieve checks and merge state for the observed head SHA, and classify each PR using the existing CI and merge classifiers. Zero checks SHALL NOT be green; pending checks SHALL remain pending; a check in progress for more than 30 minutes SHALL be stalled.

#### Scenario: Paginated exact-head observation
- **WHEN** two pages contain open PRs and the second page includes a PR whose head changes before checks are consumed
- **THEN** the watcher evaluates all stable PRs and records the changed-head observation without retrying or routing stale data

### Requirement: Merged PR discovery reconciles the open poll

The watcher SHALL list or search closed and merged PRs since a durable cursor with a bounded lookback. A merged fail review SHALL be identified by a GitHub `CHANGES_REQUESTED` review on the merged head or a recorded Verdict/ocr fail receipt keyed by repository, PR number, and merged head SHA.

#### Scenario: PR merges between polls
- **WHEN** a PR is open on run N and merges with a fail review before run N+1
- **THEN** the closed+merged reconciliation discovers it despite open-PR listing no longer returning it, records one regression for that head, and emits the linked transition notification once

### Requirement: Stable rerun identity and fencing

Each rerun SHALL use the stable key repository + PR number + exact head SHA + check name + workflow/job id. The per-key attempt counter SHALL update atomically. Immediately before requesting a rerun, the watcher SHALL reread and fence the exact PR head and check identity; a mismatch SHALL abort without rerunning or consuming a stale attempt.

#### Scenario: Changed head before rerun
- **WHEN** the head or check identity differs on the immediate pre-rerun reread
- **THEN** no rerun is requested and the old key is not advanced

### Requirement: Bounded recovery adapter is normative

The watcher SHALL apply this adapter table identically for every observation:

| Classification or action | Adapter result |
|---|---|
| GREEN / PROCEED_MERGE | READY, no side effect |
| PENDING / WAIT_CI | No action |
| INFRA_FAILURE / CANCELLED / stalled (>30m) / RETRY_CI | One bounded rerun per key |
| CODE_FAILURE / CORRECTIVE_IMPLEMENTATION; CONFLICT / BEHIND / REBASE_AND_REPROOF | Durable controller repair record |
| EMPTY / MISSING_REQUIRED | Not green, durable repair record with reason |
| Attempts exhausted / BLOCKED | BLOCKED with reason |

#### Scenario: Green has no side effect
- **WHEN** a PR is GREEN and mergeable at its exact head
- **THEN** the watcher marks READY and creates no repair record, requests no rerun, and performs no GitHub or Linear write

#### Scenario: Exhausted retry
- **WHEN** a retryable check has exhausted its per-key attempt budget
- **THEN** the watcher records BLOCKED with a stable reason and does not request another rerun

### Requirement: Watcher capability excludes mutations

The client exposed to the watcher SHALL provide only reads, bounded check reruns, and notification operations. It SHALL expose no merge, push, branch creation, or source-edit method. The watcher SHALL never merge, push, create a branch, or edit source.

#### Scenario: Mutation methods absent
- **WHEN** the watcher receives its GitHub client
- **THEN** capability inspection finds no merge, push, branch, or source-edit operation

### Requirement: Single writer and idempotent events

The watcher SHALL take a non-blocking single-writer `flock` under the common verdict-prime state directory; an overlapping invocation SHALL exit without writing. Durable idempotency keys SHALL suppress duplicates for repair, block, regression, digest, and Linear events.

#### Scenario: Duplicate timer invocation
- **WHEN** the same exact-head timer invocation runs twice
- **THEN** only one rerun and one event per idempotency key are persisted

#### Scenario: Concurrent invocation
- **WHEN** two watcher invocations overlap
- **THEN** one obtains the lock and the other exits non-blocking without duplicate state or provider writes

### Requirement: Code and merge failures route to durable repair

For code failure, conflict, behind, empty, or missing required state, the watcher SHALL write a durable controller repair record containing repository, PR number, exact head SHA, failing checks, bounded log excerpt path, classification, and reason. It SHALL never edit source, force-push, or merge.

#### Scenario: Conflict
- **WHEN** a PR head is classified CONFLICT or BEHIND
- **THEN** a repair record is written for controller dispatch and the PR is not mutated by the watcher

### Requirement: Notifications occur only on transitions

The watcher SHALL emit a digest event and, when a Linear story is linked, a Linear comment only for a new failure, blocked transition, or regression. Repeated unchanged observations SHALL produce no notification.

#### Scenario: Idle healthy run
- **WHEN** a previously ready PR remains ready at the same head
- **THEN** the watcher emits no message

### Requirement: Setup and doctor manage the watcher timer

`verdict setup` SHALL install and enable a user service/timer that runs the watcher at least every five minutes, and `verdict doctor` SHALL report timer state, runtime provenance, configured repositories, and state scope with secrets redacted.

#### Scenario: Missing timer
- **WHEN** the watcher timer is absent or inactive
- **THEN** doctor reports a failing diagnostic with the corrective setup action

### Requirement: Live smoke is opt-in

Default tests SHALL use fixtures and fake clients. Any fixture-free live smoke SHALL run only when `VERDICT_LIVE_SMOKE=1` and SHALL remain bounded.

#### Scenario: Default test isolation
- **WHEN** `VERDICT_LIVE_SMOKE` is unset
- **THEN** no live GitHub, Linear, or paid-provider call is made

### Requirement: Malformed or unreadable durable state fails closed

The watcher SHALL treat any malformed or unreadable durable state as a fatal error. The run SHALL exit without requesting any rerun and without emitting any notification. The run SHALL report the error with sufficient context for diagnosis.

#### Scenario: Corrupt state file
- **WHEN** the durable state file exists but cannot be parsed or is missing required fields
- **THEN** the watcher exits immediately with a non-zero status, writes no state, requests no rerun, and emits no notification

### Requirement: Failed Linear notification is durable and retried

When a Linear notification fails, the event SHALL remain durably recorded and SHALL be retried on the next watcher run within a bounded window of five consecutive runs. The event SHALL never be silently dropped.

#### Scenario: Linear notification failure
- **WHEN** a Linear comment request returns an error or times out
- **THEN** the idempotency key persists the event, the watcher does not advance past the failed notification, and the next run within five attempts retries the same event

### Requirement: State keys are path-safe and reject traversal

All durable state keys SHALL be validated to contain no path traversal sequences. A key containing `..`, absolute paths, or other traversal patterns SHALL be rejected at write time and the operation SHALL fail closed.

#### Scenario: Path traversal in state key
- **WHEN** a state key containing `..` or an absolute path is constructed
- **THEN** the write is rejected, the run reports the error, and no state is persisted

### Requirement: Repair record log excerpts are bounded and redacted

Log excerpts stored in repair records SHALL be bounded to at most 4096 characters. The excerpt SHALL be secret-redacted: token-bearing URLs, authorization headers, and any field matching known secret patterns SHALL be replaced with `[REDACTED]` before persistence.

#### Scenario: Large log with secrets
- **WHEN** a failing check produces a log exceeding 4096 characters and containing a GitHub token in a URL
- **THEN** the stored excerpt is truncated to 4096 characters and the token is replaced with `[REDACTED]`

### Requirement: Durable state is written before side effects

Attempt counters, the merged-PR cursor, the last classification, transitions, and repair/block/regression records SHALL be persisted atomically BEFORE the watcher performs any GitHub or Linear side effect such as a rerun request or a notification comment. If the state write fails, the run SHALL report the error and SHALL NOT request a rerun or send a notification.

#### Scenario: State write fails
- **WHEN** the atomic state write fails before the watcher requests a rerun or sends a notification
- **THEN** no rerun is requested, no notification is sent, and the run reports the error

#### Scenario: Process dies between state write and rerun
- **WHEN** the process dies or is killed after the durable state write and before the GitHub rerun call
- **THEN** the next run reads the persisted attempt counter and does not exceed the one-rerun bound for that key

### Requirement: Disagreeing review evidence is recorded and treated as fail

When the GitHub review state and a recorded Verdict/ocr review receipt for the same PR head disagree, the watcher SHALL record the fail evidence with its provenance (source, PR, head SHA) and SHALL treat the PR as failed-review for regression detection. The evidence SHALL NOT authorize a merge or any source mutation.

#### Scenario: APPROVED review with keyed Verdict/ocr fail
- **WHEN** GitHub shows an APPROVED review but a keyed Verdict/ocr receipt for the same head records a fail, and the PR is merged
- **THEN** a regression record is written naming both sources (GitHub review and Verdict/ocr receipt), and no merge or source mutation is triggered by the watcher

### Requirement: Idle runs make no model call

The watcher SHALL make no LLM call and no model-routing call on any run; it consumes only GitHub and Linear facts and the existing CI policy.

#### Scenario: No-state-change run
- **WHEN** a run completes with no PRs changing state
- **THEN** zero model or gateway requests are made and no notification is emitted
