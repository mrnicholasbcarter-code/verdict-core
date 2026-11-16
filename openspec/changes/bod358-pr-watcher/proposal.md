# BOD-358 PR watcher

## Why

Open pull requests created by the swarm can remain red, stalled, conflicting, or mergeable with an invalid review outcome because no deterministic owner polls exact-head GitHub state and routes the next bounded recovery step. This change delivers only the PR-watcher slice of BOD-358; it does not implement the broader main/CI/quota/timer/memory watcher.

## What Changes

- Add a deterministic, no-LLM watcher that discovers open PRs across configured `github.com/mrnicholasbcarter-code` repositories and evaluates the exact head SHA.
- Reuse `scripts/prime_state.py` CI and merge classifiers and recovery policy, extending only where the watcher needs stalled-job and review-regression semantics.
- Retry infrastructure failures, cancelled checks, and jobs stalled for more than 30 minutes at most once per bounded attempt budget.
- Persist code-failure and conflict/behind repair work in the common `verdict-prime` state directory for controller dispatch; the watcher never edits code, force-pushes, or merges.
- Install a user systemd timer through `verdict setup`; report its state through `verdict doctor`; notify only on state transitions through the existing digest and linked Linear story comment channels.
- Detect merged PRs whose review verdict is fail, record a regression, and comment on the linked owning Linear story.

## Capabilities

### New Capabilities

- `pr-watcher`: bounded, exact-head polling and recovery routing for swarm-owned open pull requests.

### Modified Capabilities

- `installed-runtime-provenance`: setup and doctor include the PR watcher timer and its runtime/state provenance.

## Acceptance criteria

- A `verdict setup` installed user timer runs at least every five minutes, and `verdict doctor` reports it.
- Each open configured PR is classified from exact-head checks and merge state; zero checks and pending checks are not green.
- Infrastructure/cancelled/stalled failures receive only bounded reruns; code failures and conflicts produce durable repair records; exhausted attempts become blocked with a reason.
- No watcher path force-pushes, merges, or edits source code.
- Merged PRs with a fail review verdict are recorded and notify the linked Linear story.
- Idle runs produce no notification; new failures, blocked states, and regressions notify once per transition.
- Hermetic fixture tests cover classifications, transitions, pagination, zero checks, pending, stalled jobs, and opt-in live smoke.
- OpenSpec, focused tests, ruff, strict mypy, reachability, doc links, and repository proof gates pass.

## Impact

New watcher command/state modules, setup and doctor integration, fixture tests, and the `pr-watcher` capability delta. Existing classifier behavior remains compatible.

## Evidence boundary

- KNOWN: existing prime_state classifiers, setup/doctor timer patterns, and the requested bounded exact-head behavior.
- INFERRED: the precise reusable GitHub/Linear client symbols and final state-directory filenames, to be confirmed during research.
- NOT AVAILABLE: live GitHub/Linear smoke evidence before implementation and operator credentials.
