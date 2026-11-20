# Tasks

## 1. Implementation

- [x] 1.1 Add `escalation_capacity_classes: frozenset[CapacityClass]` to `TaskRequirements` in `verdict/orchestration/contracts.py`
- [x] 1.2 Add `escalation_tier0_capacity_classes` to `RuntimePolicy` in `verdict/orchestration/runtime.py` (default: `{SUBSCRIPTION, FREE}`)
- [x] 1.3 Add `escalation_capacity_classes: frozenset[CapacityClass]` to `NodeRun` in `verdict/orchestration/runtime.py`
- [x] 1.4 Apply `escalation_capacity_classes` filter in `eligibility._assess` after tier ceiling check (reason: `escalation_capacity_excluded`)
- [x] 1.5 Split `_record_failure` else-branch: `elif t == 1` sets ceiling=0 + capacity classes; `else` (t==0) emits `escalation_exhausted` as before
- [x] 1.6 Pass `escalation_capacity_classes=run.escalation_capacity_classes` to escalation `TaskRequirements` in `_drive`
- [x] 1.7 Emit `escalation_exhausted` from `_drive` when `_esc_ceiling == 0` and `_esc_choice is None`
- [x] 1.8 Add `capacity_classes_allowed` to the `escalation` event when non-empty (1→0 step)
- [x] 1.9 Extend `EscalationSelector` in `tests/test_orch_escalation.py` to support 3-tuples with optional `CapacityClass` and filter by `escalation_capacity_classes`
- [x] 1.10 Add four failing-first tests in `tests/test_orch_escalation.py`
- [x] 1.11 Add two failing-first tests in `tests/test_orch_eligibility.py`
- [x] 1.12 Run `tests/test_orch_escalation.py`, `tests/test_orch_eligibility.py`, `tests/test_orch_runtime.py`, `tests/test_orch_recovery.py`, `tests/test_admission.py` — all pass
- [x] 1.13 `ruff check`, `ruff format --check`, `mypy verdict --strict` — all pass
- [ ] (post-merge) 1.14 Push through vgate; CI exact-head checks green on PR head; squash-merge to main

## 2. Live checks

- [ ] (post-merge) 2.1 Re-run a real-story task with a tier-1 route that previously returned `escalation_exhausted`; verify `escalation` event appears and a tier-0 subscription route is selected on the next attempt
- [ ] (post-merge) 2.2 Run `verdict doctor` and confirm it stays green after installation
