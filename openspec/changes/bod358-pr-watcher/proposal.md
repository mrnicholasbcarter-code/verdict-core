# BOD-358 PR watcher

## Why

Open pull requests created by the swarm can remain red, stalled, conflicting, or mergeable with an invalid review outcome because no deterministic owner polls exact-head GitHub state and routes the next bounded recovery step. This change delivers only the PR-watcher slice of BOD-358; it does not implement the broader main/CI/quota/timer/memory watcher.

## What Changes

- Add a deterministic, no-LLM watcher for open PRs across configured allowlisted repositories.
- Move the reusable `classify_ci`, `classify_merge_state`, `ci_recovery_action`, and `INFRA_PATTERNS` policy into importable `verdict/ci_policy.py`; `scripts/prime_state.py` re-exports them for compatibility. Production code never imports `scripts`.
- Retry infrastructure failures and cancelled checks at most once per stable check key: repository, PR number, exact head SHA, check name, and workflow/job identity. Re-read and fence head/check immediately before rerun.
- Persist code-failure, conflict/behind, blocked, and regression records in the common `verdict-prime` state directory. The watcher never edits code, force-pushes, creates branches, or merges.
- Reconcile merged PRs using a closed+merged GitHub listing/search since a durable cursor with bounded lookback. A fail review is GitHub `CHANGES_REQUESTED` on the merged head.
- Install a user systemd timer through `verdict setup`; report it through `verdict doctor`; notify only on transitions through the existing digest channel.

## Capabilities

### New Capabilities

- `pr-watcher`: bounded, exact-head polling and recovery routing for swarm-owned pull requests.

### Modified Capabilities

- `installed-runtime-provenance`: setup and doctor include the PR watcher timer and runtime/state provenance.

## Acceptance criteria

- A `verdict setup` installed user timer runs at least every five minutes, and `verdict doctor` reports it.
- Each open configured PR is classified from exact-head checks and merge state; zero checks and pending checks are not green.
- Infrastructure/cancelled failures receive only bounded reruns; code failures and conflicts produce durable repair records; exhausted attempts become blocked with a reason.
- Stable rerun keys fence changed heads and duplicate/concurrent timer invocations.
- Merged PRs with a fail review (GitHub CHANGES_REQUESTED) are found despite open-PR disappearance, recorded, and notified via digest once per transition.
- The watcher follows the normative adapter table in the capability specification. Action mapping: the adapter table in specs/pr-watcher/spec.md is normative; this file does not restate it.
- The watcher client has no merge, push, branch, or source-edit capability.
- Idle runs produce no notification; new failures, blocked states, and regressions notify once per transition via digest. flock serializes writers.
- Hermetic fixtures cover classifications, transitions, pagination, zero checks, pending, cancelled jobs, merged regression, multiple checks, changed head, duplicate timer, concurrent requests, and opt-in live smoke.
- OpenSpec, focused tests, ruff, strict mypy, reachability, forbidden-architecture scan, doc links, and repository proof gates pass.

## Impact

New watcher/policy modules, setup and doctor integration, fixture tests, and the `pr-watcher` capability delta. Existing classifier imports remain compatible through the `scripts/prime_state.py` re-export.

## Evidence boundary

- KNOWN: existing prime-state policy, setup/doctor timer patterns, and bounded exact-head requirements.
- INFERRED: precise reusable GitHub/Linear client symbols and final state filenames, to be confirmed during research.
- NOT AVAILABLE: live GitHub/Linear smoke evidence before implementation and operator credentials.
