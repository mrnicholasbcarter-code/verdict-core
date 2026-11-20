# Tasks

## 1. Implementation

- [x] 1.1 Add `escalation_max_tier` field to `TaskRequirements` in `verdict/orchestration/contracts.py`
- [x] 1.2 Register `"escalation"` in `EVENT_TYPES` in `verdict/orchestration/contracts.py`
- [x] 1.3 Apply `escalation_max_tier` as an additional tier ceiling in `verdict/orchestration/eligibility.py` (`_assess`)
- [x] 1.4 Add `judged_failure_count`, `last_selected_tier`, `escalation_tier_ceiling` to `NodeRun` in `verdict/orchestration/runtime.py`
- [x] 1.5 Tighten `escalation_tier_ceiling` in `_record_failure` after K=2 judged failures
- [x] 1.6 Two-phase selection in `_start_node`: try escalation ceiling first, fall back to full range
- [x] 1.7 Emit `"escalation"` event and append to `run.history` when escalation fires
- [x] 1.8 Write four failing-first tests in `tests/test_orch_escalation.py` (happy path, exclude-routes respected, fallback, non-judged failures)
- [x] 1.9 Run `tests/test_orch_escalation.py`, `tests/test_orch_runtime.py`, `tests/test_orch_recovery.py`, `ruff check`, `ruff format --check`, `mypy verdict --strict`
- [ ] (post-merge) 1.10 Push through vgate; CI exact-head checks green on PR head; squash-merge to main

## 2. Live checks

- [ ] (post-merge) 2.1 Re-run real-story-6 scenario (`verdict orchestrate`) with a task that previously exhausted all Haiku routes; verify `escalation` event appears in the run log and a Sonnet-tier route is selected on the third attempt
- [ ] (post-merge) 2.2 Run `verdict doctor` and confirm it stays green after installation
