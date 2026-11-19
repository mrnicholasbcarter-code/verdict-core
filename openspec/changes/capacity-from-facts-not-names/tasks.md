# Tasks: capacity-from-facts-not-names

- [x] 1.1 Red: name-only routes are not FREE; declared 0/0 and gateway free-tier are (`tests/test_serve_capacity_order.py`)
- [x] 1.2 Green: `verdict/capacity.py` delegates price to `free_rule.classify_free`; name rules removed
- [x] 1.3 `verdict/intelligence.py` passes gateway free-tier membership
- [x] 1.4 `PREFLIGHT_MAX_ROUTES = 30`; docs updated
- [x] 1.5 Measured offline impact on the local health cache and inventory (PR body)
- [ ] (post-merge) Rehearsal preflight on merged main proves 5 FREE pools from facts
- [ ] (post-merge) Rehearsal starts and completes with PREFLIGHT_MIN_PLANNER_POOLS=2 on merged main (D2 2026-11-19)
