# BOD-262 proof report

Subscription usage and headroom now feed canonical `AdmittedSet` evidence.

- Fresh `CapacitySnapshot` pools become provider observations with source, observed time, pool id, reset and retry metadata.
- Exhaustion hard-drops at `AVAILABLE`; constrained, cooldown/rate-limit, lockout/auth, payment, available and unknown remain distinct.
- Stale snapshots are omitted and cannot overwrite fresh evidence.
- Provider/account/workspace/pool identity is preserved. Separate reset windows remain separate observations.
- Unknown remains explicit and uses the existing bounded confirmation path.
- Legacy cost-ledger budgets are projected by `admit` and `admit_with_subscription`.

Validation: focused admission/capacity/subscription suite 37 passed; compileall and diff check pass. No network or gateway mutation was used.
