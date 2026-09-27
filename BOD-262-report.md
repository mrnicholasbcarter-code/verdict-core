# BOD-262 proof report — r3 (re-review fixes)

## Scope of this pass

This continues from `8d8b7f1` (re-reviewed in `REVIEW-262.md` / "Re-review of
8d8b7f1", verdict REQUEST-CHANGES). It closes the three blocking findings
(F1, F2, F3) and the four non-blocking cleanups from that re-review.

## F1 — Account isolation

`verdict/admission.py` `_judge()` matched provider-scoped subscription
observations against active connections via:

```python
o.account_id in active_accounts or (not active_accounts and len(active) == 1)
```

The `len(active) == 1` fallback let an exhausted/foreign-account snapshot
apply to a connection whose payload has no `account_id`, directly
contradicting the adjacent comment ("Ambiguous provider-only rows reject
account scoped subscription evidence rather than applying the wrong
account"). It also collapsed distinct active accounts on one provider into a
single hard-drop signal.

Fix:
- Removed the `len(active) == 1` fallback for account scoping. A connection
  with no resolvable account id no longer inherits any account-scoped
  observation.
- Added explicit multi-account handling: when more than one account is
  active on a provider, an exhausted/cooldown/unauthorized observation tied
  to one account no longer drops a route for a sibling active account that
  carries no such evidence.
- Fixed `tests/test_admission_subscription.py::_inventory()` fixture to give
  its connection a matching `account_id` (the snapshot in that suite was
  already account-scoped to `acct-123`; only the connection fixture was
  missing the field). No assertions were changed.

Commit: `4b18ffb fix(admission): reject ambiguous account-scoped subscription evidence`

## F3 — Bounded confirmation clears unknown headroom

`AdmittedSet.launchable()` hard-blocked `reason in
{"subscription_unknown", "subscription_constrained"}` unconditionally, so
`record_confirmation()` setting `confirmation_source` had no effect on those
routes — `require_launchable()` kept raising even after a successful bounded
confirmation, contradicting the documented "UNKNOWN_HEADROOM requires
bounded confirmation" contract.

Fix: `launchable()` now only blocks `subscription_unknown` when
`confirmation_source is None`. A successful `record_confirmation()` clears
it; `require_launchable()` still raises before confirmation and passes
after.

Commit: `5e4bfe0 fix(admission): let bounded confirmation clear subscription_unknown`

## F2 — Mutation-killing tests

Reran the mutant matrix in `/tmp/verdict-s3/mut262b` (mutants M1, M2, M3, M4,
M5, M6, M7, M8, M11 — reconstructed from the diffs recorded against
`review262b-mutants/base` since the matrix was not preserved as an
executable directory) against this branch's `verdict/admission.py`,
`verdict/subscription_headroom.py`, and
`verdict/admission_with_subscription.py`, running
`tests/test_admission_subscription.py`, `tests/test_admission_wrapper.py`,
`tests/test_subscription_headroom.py`, and `tests/test_admission.py`.

| Mutant | Fix reverted | Before new tests | After new tests |
|---|---|---:|---:|
| M1 | account scoping in `_judge()` | SURVIVED | **KILLED** |
| M2 | workspace scoping in `_judge()` | SURVIVED | **KILLED** |
| M3 | default freshness TTL | SURVIVED | **KILLED** |
| M4 | unknown-headroom gate in `_judge()` | SURVIVED | **KILLED** |
| M5 | `launchable()` unknown/constrained guard | SURVIVED | **KILLED** |
| M6 | `reset_at` in `to_dict()` | SURVIVED | **KILLED** |
| M7 | `CONCURRENCY_LIMIT` classification | SURVIVED | **KILLED** |
| M8 | `PROVIDER_OVERLOAD` classification | SURVIVED | **KILLED** |
| M11 | wrapper applies unscoped ledger key | SURVIVED | **KILLED** (by the pre-existing `test_wrapper_exhaustion_hard_drop`) |

New tests added to `tests/test_admission_subscription.py`:

- `test_exhausted_account_does_not_drop_route_for_active_sibling_account` (M1)
- `test_connection_without_account_id_rejects_foreign_account_evidence` (M1)
- `test_workspace_scope_isolates_foreign_workspace_exhaustion` (M2)
- `test_default_freshness_ttl_rejects_snapshot_without_fresh_until` (M3)
- `test_default_freshness_ttl_still_applies_snapshot_within_window` (M3, direction check)
- `test_unknown_headroom_admitted_but_not_launchable_without_confirmation` (M4)
- `test_launchable_blocks_unknown_headroom_until_confirmed` (M5, functional)
- `test_launchable_guard_blocks_unknown_reason_even_if_health_marked_healthy` (M5, white-box)
- `test_reset_at_is_preserved_in_admission_record_dict` (M6)
- `test_distinct_capacity_failure_classes_hard_drop[concurrency_limit]` (M7)
- `test_distinct_capacity_failure_classes_hard_drop[provider_overload]` (M8)

Note on M5: the public `admit()` path can never produce a record with
`reason="subscription_unknown"` and `health="healthy"` at the same time (an
unknown-headroom record is always stamped `health="unknown"`), and
`subscription_constrained` is never produced as a record reason at all (see
non-blocking cleanup below). The functional test
(`test_launchable_blocks_unknown_headroom_until_confirmed`) exercises the
real confirm/launch flow but does not by itself distinguish the mutant,
because the mutant's `False and ...` guard removal only matters when
`health == "healthy"` — which normal admission cannot reach for that reason.
The white-box test forces that state via `AdmittedSet._derive()` (the same
internal extension point `narrow()` and `record_confirmation()` use) to
prove the guard defends derived states, not just the direct-admission path.

Commit: `e264022 test(admission): add mutation-killing tests for scoped subscription fixes`

## Non-blocking cleanup (same commit as F2 tests)

- Removed the dead `subscription_constrained` branches: no code path in
  `_judge()` ever produces a record with that reason (only
  `subscription_unknown` is reachable for the "unknown" family), so it was
  dropped from both the hard-drop reason allow-set and the `launchable()`
  block-set rather than left as unreachable code.
- Deleted `legacy_subscription_observations()` from
  `verdict/subscription_headroom.py` — unreferenced anywhere in `verdict/`
  or `tests/` since `admit()` stopped calling it — and dropped it from
  `__all__`, along with the now-unused `Mapping` import.
- Replaced both `__import__("datetime").timedelta(...)` call sites with the
  top-level `timedelta` import already present in the module.

## Validation

Focused:
```
$ /tmp/verdict-s3/venv/bin/python -m pytest -q -p no:cacheprovider \
    tests/test_admission_subscription.py tests/test_admission_wrapper.py \
    tests/test_subscription_headroom.py tests/test_admission.py
50 passed, 1 warning in 0.54s
```

Full gate (run directly, not delegated):
```
$ /tmp/verdict-s3/venv/bin/python -m pytest -q -p no:cacheprovider
3745 passed, 11 skipped, 4 warnings in 320.53s (0:05:20)

$ /tmp/verdict-s3/venv/bin/python -m ruff check .
All checks passed!

$ /tmp/verdict-s3/venv/bin/python -m ruff format --check .
711 files already formatted

$ /tmp/verdict-s3/venv/bin/python -m mypy --strict verdict/
Success: no issues found in 236 source files

$ /tmp/verdict-s3/venv/bin/python scripts/check_doc_links.py
doc links ok (263 files checked)
```

All commands used `PYTHONPATH=/tmp/verdict-s3/bod-262-r3` and an isolated
temp `HOME`. No network, push, PR, or Linear mutation was used. Worktree is
clean at `e264022` on `s3/bod-262-r3`.

## Existing-test integrity

`test_subscription_snapshot_lockout_precedes_availability`,
`test_snapshot_exhaustion_is_canonical_hard_drop`, and
`test_distinct_pool_windows_keep_identity_and_reset_metadata` retain their
original assertions unchanged. The only pre-existing-test edit was giving
`_inventory()`'s connection fixture a matching `account_id` (required by the
F1 fix, since account-scoped evidence can no longer apply to an
account-agnostic connection).
