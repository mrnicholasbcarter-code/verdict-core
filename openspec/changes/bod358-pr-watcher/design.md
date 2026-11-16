# Design

## Context

The repository already centralizes CI classification and bounded recovery decisions in `scripts/prime_state.py`, but production code cannot import a scripts module. The watcher therefore moves the policy symbols into `verdict/ci_policy.py` and keeps `scripts/prime_state.py` as a compatibility re-export. GitHub is the source of PR/check/merge truth; Linear is only the owning-story notification target. The common `verdict-prime` state directory is the handoff boundary to the controller.

## Goals / Non-Goals

**Goals:** deterministic no-LLM polling, exact-head fencing, bounded operational recovery, durable repair/block/regression records, transition-only notifications, and <=5 minute setup/doctor timer.

**Non-Goals:** code edits, branch updates, rebases, force-pushes, merges, provider calls outside bounded client operations, or general BOD-358 health watching.

## Decisions

1. **Importable policy boundary.** `verdict/ci_policy.py` owns `INFRA_PATTERNS`, `classify_ci`, `classify_merge_state`, and `ci_recovery_action`. `scripts/prime_state.py` re-exports these names so existing tests/callers remain compatible. The watcher imports only from `verdict`.
2. **Merged regression source.** Open-PR polling is reconciled with a GitHub closed+merged PR list/search using a durable `merged_cursor` and bounded lookback. A fail review is GitHub `CHANGES_REQUESTED`, or a recorded Verdict/ocr fail receipt keyed by repository, PR number, and head SHA. A PR open at run N and merged before run N+1 is therefore visible.
3. **Exact-head and check fencing.** Every observation is keyed by repository, PR number, and observed head SHA. Every rerun is keyed additionally by check name and workflow/job identity. Immediately before rerun, the client rereads the PR head and exact check identity; any mismatch aborts without consuming a stale action.
4. **Capability restriction.** The watcher receives a client exposing discovery, check/review/merge reads, bounded rerun, and notification operations only. No merge, push, branch creation, or source-edit method exists on that interface. The normative adapter table is defined in specs/pr-watcher/spec.md; this file does not restate it.
5. **Concurrency and idempotency.** A non-blocking `flock` under the common state directory makes a second overlapping invocation exit. Atomic writes use stable keys for rerun, repair, block, regression, digest, and Linear events, suppressing duplicates across timer invocations and retries.
6. **Durable state before side effects.** Attempt counters, cursor, last classification, transitions, and records are persisted atomically before bounded GitHub/Linear side effects. Malformed state fails closed.
7. **Installation follows host patterns.** Add one watcher service/timer to the existing setup step model and doctor timer diagnostics, with an interval no greater than five minutes and explicit state scope.

## Data flow

`verdict pr-watcher` acquires the non-blocking lock -> loads configured repositories and durable cursor -> paginates open PRs -> fetches exact-head checks, merge state, and reviews -> rejects stale/mismatched observations -> classifies -> applies bounded rerun or writes repair/block records -> lists closed+merged PRs since cursor and evaluates fail reviews -> writes transition digest and linked Linear event by idempotency key -> atomically persists state and advances cursor.

## Test strategy

A green PR produces no repair record, no rerun, and no GitHub or Linear write. Use fixture GitHub payloads and fake capability-restricted clients for every classifier/action/transition, pagination, exact-head reread, zero/pending/stalled/cancelled checks, retry exhaustion, stable keys, changed heads, multiple checks, duplicate/concurrent invocations, flock exclusion, repair records, notification deduplication, and merged fail-review regression (open at run N, merged before N+1). Add setup/doctor unit coverage for the timer. Keep live smoke fixture-free and gated by `VERDICT_LIVE_SMOKE=1`; tests never use paid providers or real operator state. Run the forbidden-architecture checker to ensure new names do not match removed architecture terms.

## Risks / Trade-offs

- Cross-repository polling increases API pagination and rate-limit exposure; bounded pages, lookback, timeouts, and explicit configuration keep idle runs deterministic.
- Shared state can be concurrently written by a timer and controller; flock, atomic writes, stable keys, and fail-closed malformed-state handling are required.
- Linear notification failure can leave an unnotified durable event; the event remains durable for the next bounded retry and is never silently dropped.
- GitHub review state and recorded Verdict/ocr receipts can disagree; the keyed fail evidence is recorded with provenance and does not authorize merge or source mutation.

## Migration / rollback

Additive installation. Existing setup steps and timers remain unchanged; disabling or uninstalling the watcher removes only its service/timer and leaves durable records for audit. Reverting the change removes the command and integration, while existing records remain inert data. The policy move is compatibility-preserving because `scripts/prime_state.py` re-exports the names.

## ADR impact

No new ADR is required. The watcher preserves the existing authority boundaries: GitHub remains PR truth, `verdict/ci_policy.py` is importable recovery policy, the controller remains repair executor, and setup/doctor remain host lifecycle authorities.

## Security / trust implications

GitHub, Linear, and worker payloads are untrusted. Validate repository allowlists and exact-head identity, reject path traversal in state keys, bound log excerpts, redact secrets and token-bearing URLs, isolate `VERDICT_HOME` in tests, and never treat check text as executable instructions. The watcher capability interface has no source-write, force-push, branch, or merge operation.

## Routing / context / memory implications

The watcher uses no LLM or model routing on idle runs. It consumes only GitHub/Linear facts and existing policy, writes bounded controller repair records in the common verdict-prime state directory, and emits transition events through existing digest/Linear channels. No new memory subsystem or cross-project context store is introduced.
