# BOD-262 proof report

## Implementation

- Canonical authority is `verdict.admission.admit`; subscription snapshots are projected into `RuntimeEvidence` by `subscription_observations`.
- `admission_subscription.py` and `admission_with_subscription.py` are typed compatibility wrappers; neither imports private minting state or constructs `AdmittedSet`.
- Fresh subscription pool and balance evidence preserves source, account identity, pool id, reset windows, retry windows, and categories. Stale snapshots are omitted.
- Hard drop precedence is explicit: lockout/auth/payment at `ENTITLED`, unhealthy at `HEALTHY`, exhaustion/cooldown at `AVAILABLE`; unknown remains admitted-unverified and needs bounded confirmation.

## Acceptance criteria to tests

| AC | Evidence | Tests |
|---|---|---|
| Canonical admission and compatibility | thin wrappers delegate to `admit` | wrapper exhaustion/available/unknown tests |
| Freshness and stale precedence | stale snapshots emit no observations | `test_stale_snapshot_does_not_overwrite_fresh_route_evidence` |
| Hard drop and category precedence | exhaustion and lockout stages/reasons | `test_snapshot_exhaustion_is_canonical_hard_drop`, `test_subscription_snapshot_lockout_precedes_availability` |
| Pool identity/reset windows | each pool remains distinct with reset metadata | `test_distinct_pool_windows_keep_identity_and_reset_metadata` |
| Legacy budgets | account-scoped ledger exhausted route drops | `test_legacy_subscription_exhaustion_hard_drop` |

## Mutation coverage

- Added tests kill mutations that omit subscription snapshot projection, treat stale snapshots as fresh, collapse pool identity/reset windows, or let availability precede lockout.
- Existing full suite mutation corpus remains covered; no test weakening or authority bypass added.

## Validation

Focused command (correct collection):
```
/tmp/verdict-s3/venv/bin/python -m pytest -q -p no:cacheprovider tests/test_admission_subscription.py tests/test_admission_wrapper.py tests/test_subscription_headroom.py
# 14 passed
```

Full gates:
```
/tmp/verdict-s3/venv/bin/python -m pytest -q -p no:cacheprovider
# 3734 passed, 11 skipped
/tmp/verdict-s3/venv/bin/ruff check .
# All checks passed
/tmp/verdict-s3/venv/bin/ruff format --check .
# 711 files already formatted
/tmp/verdict-s3/venv/bin/python -m mypy --strict verdict/
# Success: no issues found in 236 source files
```

No network, push, PR, or Linear mutation was used.
