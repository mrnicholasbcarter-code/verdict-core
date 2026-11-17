# Tasks

## 1. Local implementation

- [x] 1.1 Create OpenSpec change: proposal.md, design.md, tasks.md, specs/<capability>/spec.md
- [x] 1.2 Validate OpenSpec change: both validators exit 0
- [ ] 1.3 Write failing tests for EligibilityLadder.select with lease held:
  - [ ] 1.3.1 Lock held + fresh healthy launchable route -> select returns it, zero probe calls
  - [ ] 1.3.2 Lock held + only route needing probe -> no probe, chosen is None, reason is 'lease_held'
  - [ ] 1.3.3 Lock not held -> behaviour unchanged (existing tests stay green)
- [ ] 1.4 Write failing tests for gate_admit_prove_confirm with lease held:
  - [ ] 1.4.1 Lease held + fresh passport -> admitted not empty, no confirm sent
  - [ ] 1.4.2 Lease held + stale passport -> confirm_unavailable
- [ ] 1.5 Write failing test for judge_choose with stderr tail ending in outcome-history lines
- [ ] 1.6 Fix EligibilityLadder.select: keep probe_order, set lease_held flag, skip probe dispatch with 'lease_held' reason
- [ ] 1.7 Fix gate_admit_prove_confirm: admit fresh passports, mark stale as confirm_unavailable
- [ ] 1.8 Fix judge_choose: extract failure reason from stdout JSON first, then last non-"outcome history:" stderr line
- [ ] 1.9 Run focused tests: pytest -q <touched test files> tests/test_admission.py tests/test_orch_runtime.py tests/test_visual_system.py
- [ ] 1.10 Run lint gates: ruff check . && ruff format --check . && mypy verdict --strict
- [ ] 1.11 Run doc links check: python scripts/check_doc_links.py
- [ ] 1.12 Run reachability check: python scripts/check_reachability.py --ratchet-base origin/main (0 NEW)
- [ ] 1.13 Run OpenSpec tasks check: python scripts/check_openspec_tasks.py --base origin/main
- [ ] 1.14 Run both OpenSpec validators
- [ ] 1.15 Run git diff --check origin/main HEAD
- [ ] 1.16 Tick implementation tasks in tasks.md
- [ ] 1.17 Commit changes
- [ ] 1.18 Push branch and create PR
- [ ] 1.19 (post-merge) Install from merged main, restart verdict-serve, and confirm the next hourly acceptance run that starts with the six-hourly screen (00/06/12/18 UTC) is green