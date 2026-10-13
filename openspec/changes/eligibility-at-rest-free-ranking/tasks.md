# Tasks

## 1. Liveness and selection
- [x] 1.1 One FRESH_SECONDS (900 s) for cache, ladder and admission.
- [x] 1.2 Agentic-proven, served, unblocked FREE routes are eligible with `liveness_at_select`.
- [x] 1.3 select() confirms in rank order; failing providers wait; selected route is rank 0.

## 2. Prober scope
- [x] 2.1 Failure scope from the provider message; account errors write `pool:<id>`.
- [x] 2.2 No further probe reaches a cooled pool in the same cycle (A4 test).
- [x] 2.3 Half-open retries capped at 25% of a cycle; daemon clears the cursor per cycle.

## 3. Ranking
- [x] 3.1 Failing last, FREE first, then session score; frontier roles proven > unknown > failing first.
- [x] 3.2 Inside FREE: gateway success rate, capability, context, latency; quota never ranks FREE.
- [x] 3.3 Gateway overload writes nothing in the prober and the select-time check.
- [x] 3.4 Only an answered failure revokes an agentic proof; a timeout/5xx is re-probed after its negative.

## 4. Live
- [x] 4.1 `verdict eligibility --json` on the operator host lists the proven FREE routes without --probe.
- [x] 4.2 Bounded `prove-at-rest once` from the branch (run 5: 94 agentic probed, 28 passed).
- [ ] 4.3 A3 recall >= 0.90 and precision >= 0.95 against a same-time direct check
      (13:27Z: precision 0.93, recall 0.81 on determinate routes; causes fixed in 98448f0f and 7de03b43, re-check after the next prober cycles).
- [ ] 4.4 Merged-main run and the parent's own re-run (definition of done 1, 6, 7).
