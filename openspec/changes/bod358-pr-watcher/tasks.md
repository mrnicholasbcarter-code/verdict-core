# Tasks

Ownership is disjoint. Core owns policy and watcher modules/tests; host owns command registration, setup, and doctor shared files; documentation owns this change; verification owns live smoke/evidence. Host work is sequenced after core interfaces are fixed.

## 1. Research - wave 1

- [ ] 1.1 (research-pr-watcher-boundaries; depends_on: []; owned_files: []) Trace policy, GitHub/Linear clients, common state, command registry/parsers, setup plan, doctor collector, tests, and forbidden architecture terms. Return exact symbols and ownership boundaries.

## 2. Core - wave 2

- [ ] 2.1 (implement-pr-watcher-core; depends_on: 1.1; owned_files: verdict/ci_policy.py, scripts/prime_state.py, verdict/pr_watcher.py, verdict/pr_watcher_state.py, verdict/pr_watcher_github.py, verdict/pr_watcher_notifications.py, tests/test_ci_policy.py, tests/test_pr_watcher.py, tests/fixtures/pr_watcher/**) Move classify_ci, classify_merge_state, ci_recovery_action, and INFRA_PATTERNS into importable verdict/ci_policy.py; make scripts/prime_state.py re-export them. Implement exact-head polling and pure action adaptation. GREEN / PROCEED_MERGE -> READY, no side effect; PENDING / WAIT_CI -> no action; INFRA_FAILURE / CANCELLED / stalled (>30m) / RETRY_CI -> one bounded rerun per key; CODE_FAILURE / CORRECTIVE_IMPLEMENTATION, CONFLICT / BEHIND / REBASE_AND_REPROOF -> durable controller repair record; EMPTY / MISSING_REQUIRED -> not green, repair record with reason; attempts exhausted / BLOCKED -> BLOCKED with reason.
- [ ] 2.2 (implement-pr-watcher-core; depends_on: 2.1; owned_files: same as 2.1) Reconcile open polling with closed+merged GitHub PRs since a durable cursor and bounded lookback. A fail review is GitHub CHANGES_REQUESTED or a recorded Verdict/ocr fail receipt keyed by repository, PR number, and head SHA. Persist the regression once per transition and cover PR open at run N, merged with fail review before N+1.
- [ ] 2.3 (implement-pr-watcher-core; depends_on: 2.1; owned_files: same as 2.1) Bound reruns by atomic key repository + PR number + exact head SHA + check name + workflow/job identity. Re-read and fence exact head/check immediately before rerun. Add duplicate timer, concurrent invocation, multiple checks, changed head, and retry exhaustion fixtures. Use non-blocking flock under common verdict-prime state; expose no merge, push, branch, or source-edit method to the watcher client.
- [ ] 2.4 (implement-pr-watcher-core; depends_on: 2.1; owned_files: same as 2.1) Persist atomic idempotency keys for repair, block, regression, digest, and Linear events; cover transition-only notifications, negative capability, path-safe keys, and malformed state fail-closed behavior.

## 3. Host and contract - wave 3

- [ ] 3.1 (integrate-pr-watcher-host-runtime; depends_on: 2.1, 2.2, 2.3, 2.4; owned_files: verdict/commands/dispatch.py, verdict/commands/parsers_runtime.py, verdict/setup_host.py, verdict/doctor_diagnostics.py, verdict/pr_watcher_service.py, tests/test_setup_host.py, tests/test_doctor_host_prerequisites.py, tests/test_pr_watcher_service.py) Register the command in dispatch.py and parsers_runtime.py, render units in pr_watcher_service.py, and integrate the service/timer into setup dry-run/apply/rollback at <=5 minutes. Extend doctor timer, provenance, configured repository, and state-scope diagnostics with redaction. This is the only task editing command registry/parsers, setup plan, or doctor collector.
- [ ] 3.2 (update-pr-watcher-openspec; depends_on: 2.1, 2.2, 2.3, 2.4; owned_files: openspec/changes/bod358-pr-watcher/{proposal.md,design.md,tasks.md,specs/pr-watcher/spec.md}) Align proposal, design, spec, and this task map with regression source/identity, rerun fencing, action mapping, negative capability, flock, idempotency, and ownership.

## 4. Verification - wave 4

- [ ] 4.1 (verify-pr-watcher-release-gates; depends_on: 3.1, 3.2; owned_files: tests/test_pr_watcher_live_smoke.py, docs/proof/dod/bod358-pr-watcher.json) Add fixture-free live smoke gated by VERDICT_LIVE_SMOKE=1 and run focused tests, ruff, strict mypy, reachability, forbidden-architecture scan, doc links, both OpenSpec validators, proof matrix, and git diff --check. Record raw evidence and confirm every new symbol has a production caller.
