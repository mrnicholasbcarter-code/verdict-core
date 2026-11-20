# Tasks

## 1. Implementation

- [x] 1.1 Add `diff_rejected` classification in `verdict/orchestration/recovery.py` before the `unknown` fallthrough
- [x] 1.2 Rename `_clear_existing_salvage_dir` → `clear_salvage_dir` (public) in `verdict/orchestration/salvage.py`
- [x] 1.3 Add `_JUDGED_WORK_CATEGORIES`, `NodeRun.last_rejected`, update `_record_failure()`, `failure_feedback`, `_fail()` salvage block, `_attempt()` restore block in `verdict/orchestration/runtime.py`
- [x] 1.4 Add three red-first tests in `tests/test_orch_salvage.py`; show failing without fix, green after
- [x] 1.5 Run focused salvage and orchestration tests: 37 salvage + 223 orchestration pass
- [ ] (post-merge) 1.6 Push through vgate; CI exact-head checks green on PR head; squash-merge to main

## 2. Live checks

- [ ] (post-merge) 2.1 From installed merged main, replay one of the failing runs (`/tmp/real-story-5/runs/`) and confirm no "SEARCH text not found" failures appear on retry
- [ ] (post-merge) 2.2 Confirm `verdict doctor` green and no salvage/restore regressions in a fresh orchestrate run after the fix is deployed
