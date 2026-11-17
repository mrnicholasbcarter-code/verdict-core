# Tasks

## 1. Implementation (T3-T12)
Verification: Each task has a dedicated test suite. T3-T9 are verified by unit tests in `tests/test_selection_*.py` and integration tests in `tests/test_orch_*.py`. T4 and T10 are verified by independent reviewer sign-off. T11 and T12 are verified by live acceptance checks L1-L5.

- [ ] T3: Implement the selection pipeline (S0-S3) and the `role_ladder.shadow` event.
- [ ] T4: Independent review of slice 1.
- [ ] T5: Implement Capacity and Quota filtering.
- [ ] T6: Implement Family independence and Measured Demotion.
- [ ] T7: Implement the advisory `role_strength` ordering.
- [ ] T8: Implement the flag modes (Off/Shadow/On) and CLI/env configuration.
- [ ] T9: Wire the new selection into the Orchestration runtime.
- [ ] T10: Independent review of the integration branch.
- [ ] T11: Swarm wiring and post-merge live checks.
- [ ] T12: Clean up legacy constants (conditional on L1-L5 stability).

## 2. Post-Merge Validation (L1-L5)
Verification: Run on merged main against a live gateway. Evidence recorded in `docs/proof/`.

- [ ] (post-merge) L1: Verify H1 inclusion and pool/capacity correctness on real gateway.
- [ ] (post-merge) L2: Verify stale source fallback to legacy order.
- [ ] (post-merge) L3: Verify unavailable provider handling and `FAIL_CLOSED`.
- [ ] (post-merge) L4: Verify identity mismatch handling in call logs.
- [ ] (post-merge) L5: Verify shadow-mode parity and on-mode review independence.
