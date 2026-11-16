# pr-watcher Specification

## Purpose

Provide deterministic ownership for swarm-opened pull requests until they are green and mergeable or explicitly blocked with a durable reason.

## Spec Delta

## ADDED Requirements

### Requirement: Exact-head polling classifies every open configured pull request

The watcher SHALL paginate open pull requests across the configured allowlisted repositories, retrieve checks and merge state for the observed head SHA, and classify each PR using the existing CI and merge classifiers. A PR with zero checks SHALL NOT be green; pending checks SHALL remain pending; a check in progress for more than 30 minutes SHALL be stalled.

#### Scenario: Paginated exact-head observation
- **WHEN** two pages contain open PRs and the second page includes a PR whose head changes before checks are consumed
- **THEN** the watcher evaluates all stable PRs and records the changed-head observation without retrying or routing stale data.

### Requirement: Retryable failures have bounded reruns

For infrastructure failure, cancelled checks, or a stalled job, the watcher SHALL request at most one bounded rerun per available attempt budget. When the budget is exhausted it SHALL record BLOCKED with a reason.

#### Scenario: Stalled check
- **WHEN** a check has been in progress for more than 30 minutes
- **THEN** the watcher classifies it as retryable stalled infrastructure and requests one rerun unless the bounded budget is exhausted.

### Requirement: Code and merge failures route to durable repair

For code failure, conflict, or behind merge state, the watcher SHALL write a durable controller repair record containing repository, PR number, exact head SHA, failing checks, bounded log excerpt path, classification, and reason. It SHALL never edit source, force-push, or merge.

#### Scenario: Conflict
- **WHEN** a PR head is classified CONFLICT or BEHIND
- **THEN** a repair record is written for controller dispatch and the PR is not mutated by the watcher.

### Requirement: Green state and attempt exhaustion are explicit

A PR SHALL be marked ready only when checks are green and merge state is mergeable/clean. Empty, unknown, pending, or stale observations SHALL NOT be marked ready. Exhausted recovery SHALL be BLOCKED with a stable reason.

#### Scenario: Zero checks
- **WHEN** an open PR has no check runs
- **THEN** its state is not GREEN and no ready notification is emitted.

### Requirement: Notifications occur only on transitions

The watcher SHALL emit a digest event and, when a Linear story is linked, a Linear comment only for a new failure, blocked transition, or regression. Repeated unchanged observations SHALL produce no notification.

#### Scenario: Idle healthy run
- **WHEN** a previously ready PR remains ready at the same head
- **THEN** the watcher emits no message.

### Requirement: Merged fail-review regressions are recorded

When a watched PR is merged and its review verdict is fail, the watcher SHALL record a regression and notify the linked owning Linear story once per regression transition.

#### Scenario: Merged failing review
- **WHEN** a merged PR has a fail review verdict
- **THEN** the regression is durable and the owning story receives a transition comment when linked.

### Requirement: Setup and doctor manage the watcher timer

`verdict setup` SHALL install and enable a user service/timer that runs the watcher at least every five minutes, and `verdict doctor` SHALL report timer state, runtime provenance, configured repositories, and state scope with secrets redacted.

#### Scenario: Missing timer
- **WHEN** the watcher timer is absent or inactive
- **THEN** doctor reports a failing diagnostic with the corrective setup action.

### Requirement: Live smoke is opt-in

Default tests SHALL use fixtures and fake clients. Any fixture-free live smoke SHALL run only when `VERDICT_LIVE_SMOKE=1` and SHALL remain bounded.

#### Scenario: Default test isolation
- **WHEN** `VERDICT_LIVE_SMOKE` is unset
- **THEN** no live GitHub, Linear, or paid-provider call is made.
