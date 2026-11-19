# Tasks

## 1. Implementation

- [x] 1.1 Red-first tests in `tests/test_orch_review.py` for the short wait, the long cooldown,
  the review deadline, floor-rejected routes, still cooling after the wait, the launch re-check,
  an available reviewer and an implementer route.
- [x] 1.2 `OpenCodeReviewer.review` waits once per level for a short cooldown and reselects.
- [x] 1.3 `DagRuntime` passes the policy limit and a wait that uses its clock, sleep and event
  log; red-first test in `tests/test_orch_runtime.py`.
- [x] 1.4 Docs: ADR-036 independent review step.
