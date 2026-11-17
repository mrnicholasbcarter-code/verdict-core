# Tasks

## 1. Local implementation

- [x] 1.1 Create OpenSpec change: proposal.md, design.md, tasks.md, specs/shared-selection/spec.md
- [x] 1.2 Validate OpenSpec change: both validators exit 0
- [x] 1.3 Write failing tests for EligibilityLadder.select with lease held:
  - [x] 1.3.1 Lock held + fresh healthy launchable route -> select returns it, zero probe calls
  - [x] 1.3.2 Lock held + only route needing probe -> no probe, chosen is None, reason is 'lease_held'
  - [x] 1.3.3 Lock not held -> behaviour unchanged (existing tests stay green)
- [x] 1.5 Write failing test for judge_choose with nonzero exit + stdout selected
- [x] 1.6 Fix EligibilityLadder.select: keep probe_order, set lease_held flag, skip probe dispatch with 'lease_held' reason
- [x] 1.8 Fix judge_choose: nonzero exit always fails; stdout JSON first, then last non-"outcome history:" stderr line
- [x] 1.9 Run focused tests: pytest -q tests/test_orch_eligibility.py tests/test_admit_prove_confirm.py tests/test_acceptance.py tests/test_admission.py tests/test_orch_runtime.py tests/test_visual_system.py
- [x] 1.10 Run lint gates: ruff check . && ruff format --check . && mypy verdict --strict
- [x] 1.11 Run doc links check: python scripts/check_doc_links.py
- [x] 1.12 Run reachability check: python scripts/check_reachability.py --ratchet-base origin/main (0 NEW)
- [x] 1.13 Run OpenSpec tasks check: python scripts/check_openspec_tasks.py --base origin/main
- [x] 1.14 Run both OpenSpec validators
- [x] 1.15 Run git diff --check origin/main HEAD
- [x] 1.16 Tick implementation tasks in tasks.md
- [x] 1.17 Commit changes
- [x] 1.18 Push branch and create PR
- [ ] 1.19 (post-merge) Install from merged main, restart verdict-serve, and confirm the next hourly acceptance run that starts with the six-hourly screen (00/06/12/18 UTC) is green
