# Tasks

## 1. Contract and pure watcher policy

- [ ] 1.1 Define the watcher configuration, exact-head observation model, bounded attempt/transition state schema, and controller repair-record schema under the existing common `verdict-prime` state boundary; add fixture tests for schema validation and path-safe keys.
- [ ] 1.2 Implement the pure adapter/orchestrator that reuses `classify_ci`, `classify_merge_state`, and `ci_recovery_action`, with explicit zero-check, pending, stalled (>30 minutes), cancelled, exact-head mismatch, and retry-exhaustion behavior; verify unit fixtures for every classification.

## 2. GitHub actions and durable routing

- [ ] 2.1 Implement paginated configured-repository/open-PR discovery and exact-head check/merge/review retrieval behind a testable client interface; verify pagination and changed-head fencing with fixture payloads.
- [ ] 2.2 Implement one bounded rerun for infrastructure/cancelled/stalled checks and durable repair records for code failure and conflict/behind states; verify no merge, force-push, source edit, or unbounded retry occurs and exhausted attempts become BLOCKED with a reason.
- [ ] 2.3 Implement transition-only digest and linked Linear-story notification handling, including merged PR + fail-review regression records; verify idle deduplication, new-failure/blocked/regression transitions, and missing-story behavior.

## 3. Installed runtime and operator entry points

- [ ] 3.1 Add the deterministic watcher command/service and user systemd timer to the existing `verdict setup` installation model with an interval of five minutes or less and isolated `VERDICT_HOME`/common state configuration; verify dry-run, install, rollback, and timer invocation.
- [ ] 3.2 Add `verdict doctor` diagnostics for watcher service/timer state, runtime provenance, configured repositories, and state scope; verify missing/inactive/failed timer rows and redacted details.

## 4. Verification and live boundary

- [ ] 4.1 Add fixture-free live smoke coverage gated by `VERDICT_LIVE_SMOKE=1`, with no live calls in default tests; verify the gate and bounded behavior.
- [ ] 4.2 Run focused watcher/setup/doctor suites plus ruff, strict mypy, reachability, doc links, OpenSpec strict validation, proof matrix, and `git diff --check`; record raw evidence and confirm every new symbol has a production caller.
