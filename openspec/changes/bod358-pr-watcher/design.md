# Design

## Context

The repository already centralizes CI classification and bounded recovery decisions in `scripts/prime_state.py`, and `verdict setup`/`verdict doctor` manage user timers. The watcher must compose those authorities rather than create a second CI policy. GitHub is the source of PR/check/merge truth; Linear is only the owning-story notification target. The common `verdict-prime` state directory is the handoff boundary to the controller.

## Goals / Non-Goals

**Goals:** deterministic no-LLM polling, exact-head fencing, bounded operational recovery, durable repair/block/regression records, and transition-only notifications.

**Non-Goals:** code edits, branch updates, rebases, force-pushes, merges, provider calls, or general BOD-358 health watching.

## Decisions

1. **Reuse prime state policy.** `classify_ci`, `classify_merge_state`, and `ci_recovery_action` remain the policy authority. The watcher adapts GitHub payloads into their input contract and adds only stalled-job detection and review-regression recording.
2. **Exact-head fencing.** Every check query and action is keyed by repository, PR number, and observed head SHA. A changed head invalidates the observation and prevents retrying or emitting a stale repair record.
3. **Durable state before side effects.** Attempt counters, last classification, transition notifications, and repair records are atomically persisted beneath the common state directory. Reruns are idempotent and bounded across timer invocations.
4. **Split actions.** Retryable infrastructure/cancelled/stalled checks may be rerun once within budget. Code failures and merge conflicts/behind states create controller repair records; the watcher does not mutate git state.
5. **Existing notification channels.** State-transition digests are appended to the existing digest path, and Linear comments are emitted only when a linked story exists and the transition key is new. No notification is emitted for unchanged healthy state.
6. **Installation follows host patterns.** Add one watcher service/timer to the existing setup step model and doctor timer diagnostics, with an interval no greater than five minutes and explicit state scope.

## Data flow

`verdict pr-watcher` loads configured repositories -> paginates open PRs -> fetches PR head/check/review/merge payloads -> rejects stale/mismatched head observations -> classifies CI and merge state -> loads per-PR attempt/transition state -> applies bounded retry or writes repair/block/regression records -> writes transition digest and linked Linear notification -> persists state.

## Test strategy

Use fixture GitHub payloads and fake clients for every classifier/action/transition, pagination, exact-head fencing, zero/pending/stalled checks, retry exhaustion, repair records, notification deduplication, and merged-fail-review regression. Add setup/doctor unit coverage for the timer. Keep live smoke fixture-free and gated by `VERDICT_LIVE_SMOKE=1`; tests never use paid providers or real operator state.

## Risks / Trade-offs

- Cross-repository polling increases API pagination and rate-limit exposure; bounded pages, timeouts, and explicit configuration keep idle runs deterministic.
- Shared state can be concurrently written by a timer and controller; atomic writes and stable record keys are required, while malformed state fails closed.
- Linear notification failure can leave an unnotified durable event; the event remains durable for the next bounded retry and is never silently dropped.

## Migration / rollback

Additive installation. Existing setup steps and timers remain unchanged; disabling or uninstalling the watcher removes only its service/timer and leaves durable records for audit. Reverting the change removes the command and integration, while existing records remain inert data. No schema migration is required beyond versioned watcher records.

## ADR impact

No new ADR is required. The watcher preserves the existing authority boundaries: GitHub remains PR truth, prime_state remains recovery policy, the controller remains repair executor, and setup/doctor remain host lifecycle authorities.

## Security / trust implications

GitHub, Linear, and worker payloads are untrusted. Validate repository allowlists and exact-head identity, reject path traversal in state keys, bound log excerpts, redact secrets and token-bearing URLs, isolate `VERDICT_HOME` in tests, and never treat check text as executable instructions. The watcher has no source-write, force-push, or merge capability.

## Routing / context / memory implications

The watcher uses no LLM or model routing on idle runs. It consumes only GitHub/Linear facts and existing prime_state policy, writes bounded controller repair records in the common verdict-prime state directory, and emits transition events through the existing digest/Linear channels. No new memory subsystem or cross-project context store is introduced.
