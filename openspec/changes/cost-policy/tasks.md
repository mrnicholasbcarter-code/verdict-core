# Tasks — operator cost policy (CP1)

- [ ] 1.1 Add `verdict/cost_policy.py`: schema, validation, normalised version hash, loader from Verdict config; fail closed on a bad policy.
- [ ] 1.2 Replace `CAPACITY_ORDER` (capacity, eligibility, candidate_pool, verified_refresh, model_refresh, identify_pools) with the policy order.
- [ ] 1.3 Generalise both `RouteSelection` assertions to the policy order.
- [ ] 2.1 Unknown-cost modes at admission; discovery refresh probes `UNKNOWN` routes under `probe_only`.
- [ ] 2.2 Per-route daily cap for `UNKNOWN` routes from the attempt ledger; reason `unknown_daily_cap`.
- [ ] 2.3 Subscription reserve for worker nodes from existing quota readings; `null` and errors are unreadable and never trigger it.
- [ ] 2.4 Narrowing-only `--include-unknown` and `--allow-metered`; `free_only` under `assume_free` admits `UNKNOWN`.
- [ ] 3.1 Receipts record `cost_policy_version`, `cost_class`, the deciding fact and the admitting mode.
- [ ] 3.2 `docs/CONFIGURATION.md`: the policy section; "unknown is never free" replaced by the mode description.
- [ ] 3.3 AGENTS.md cost row: "A route is PAID only from declared or observed facts, never from its name"; both name tests still pass.
- [ ] 4.1 Tests: schema and fail-closed load; each mode; the cap; the reserve including `null` quota; flags never widen; one order everywhere; default policy keeps today's order.
- [ ] (post-merge) 5.1 Live read-only route on the real catalogue with the default policy, before and after; then with this operator's policy. Save all outputs as evidence.
