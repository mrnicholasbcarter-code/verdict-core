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

## F4 — sibling-account suppression was fail-open (re-review of `04e299d`)

The multi-account filter added at `verdict/admission.py:742-758` fired on
connection cardinality alone (`len(active_accounts) > 1`) and then deleted the
bad account's observations for **every** route on that provider. Three
fail-open consequences, all fixed here:

1. **Account-pinned routes lost their own account's exhaustion.** A route whose
   inventory row declares `subscription_pool_id = "openai/acct-a/subscription"`,
   or whose route id encodes `acct-a`, is unambiguously bound to `acct-a`. The
   filter overrode both bindings.
2. **ENTITLED-stage lockout was bypassed.** `"unauthorized"` was in
   `_bad_states`, so `AUTH_EXPIRED`, `PERMISSION_DENIED` and the payment/billing
   classification were all discardable by an unrelated sibling connection.
3. **"Viable" was inferred from absence of evidence.** A sibling counted as
   viable purely because no bad subscription observation named it, even when its
   own connection reported a bad `testStatus`.

### Fix

The filter is now gated on three conditions, all of which must hold:

- **No resolvable account binding.** `route_is_account_bound` is true when the
  row carries a `subscription_pool_id`/`pool_id` marker or the route id encodes
  an account (the same two markers the upstream scoping filter already trusts).
  A bound route's own account governs and no sibling can stand in for it.
- **Only `exhausted` and `cooldown` are suppressible.** `"unauthorized"` is
  removed from the state set, so ENTITLED-stage lockout, auth and payment
  failures are never cleared by a sibling account. Suppressing exhaustion to let
  a sibling serve is routing policy; suppressing a credential failure is not.
- **Positive viability for the sibling.** New `_connection_is_viable()` requires
  an `isActive` connection whose `testStatus` is not in `_BAD_TEST_STATUS` and
  which carries no live `rate_limited_until` window for the route. Absence of
  bad subscription evidence is no longer viability.
  `_connection_is_rate_limited()` factors out the window check so the per-route
  key filtering (`canonical_route_id`) matches the existing loop in `_judge()`.

### Reviewer repro, `/tmp/rev262r3_repro.py`

All seven cases now match the `8d8b7f1` base:

```text
T1_POOL_PINNED_EXHAUSTED           {"admitted": false, "reason": "subscription_exhaustion", "launchable": false}
T1b_POOL_PINNED_SINGLE_ACCT        {"admitted": false, "reason": "subscription_exhaustion", "launchable": false}
T2_ENCODED_ROUTE_EXHAUSTED         {"admitted": false, "reason": "subscription_exhaustion", "launchable": false}
T2b_ENCODED_ROUTE_LOCKOUT          {"admitted": false, "reason": "lockout", "launchable": false}
T2c_ENCODED_ROUTE_PAYMENT          {"admitted": false, "reason": "lockout", "launchable": false}
T3_SIBLING_TESTSTATUS_ERROR        {"admitted": false, "reason": "subscription_exhaustion", "launchable": false}
T4_AGNOSTIC_LOCKOUT_SIBLING_SILENT {"admitted": false, "reason": "lockout", "launchable": false}
```

The F1 cases still behave as approved (`/tmp/rev262r3_probe.py`): `acct-a`
exhausted with an active, unpinned, viable `acct-b` stays admitted
(`R1`/`R2`), both-exhausted still hard-drops (`R3`), an account-less connection
still ignores foreign-account evidence (`R4`), and a matching-account
exhaustion still drops (`R5`).

### New tests (restrictive direction)

Five tests added to `tests/test_admission_subscription.py`. Four fail on
`04e299d`, proven by extracting `git archive 04e299d` to `/tmp/f4-mut`,
overwriting only the test file, and running it against the old source:

```
$ cd /tmp/f4-mut && python -m pytest tests/test_admission_subscription.py -q
4 failed, 17 passed, 1 warning in 0.77s
FAILED test_pool_pinned_route_keeps_own_account_exhaustion_despite_viable_sibling
FAILED test_encoded_account_route_keeps_lockout_despite_viable_sibling
FAILED test_agnostic_route_keeps_lockout_despite_viable_sibling
FAILED test_sibling_with_bad_test_status_does_not_confer_viability
```

The fifth, `test_rate_limited_sibling_does_not_confer_viability`, is labelled a
direction check in its docstring: it does not distinguish `04e299d`, because the
earlier per-connection rate-limit loop in `_judge()` already hard-drops any
route with a live window on any active connection. The rate-limit clause in
`_connection_is_viable()` is defence-in-depth and the test pins the direction.

### Mutation matrix, rebuilt on the F4 source

Rebuilt from scratch at `/tmp/f4-matrix`: `git ls-files` export as `base`, then
one independent source-level revert per mutant, each `py_compile`-verified, each
run against the same 12 admission/capacity/headroom test files
(`167 passed` at baseline). All 13 mutants die.

| Mutant | Fix reverted | Result | Killing test |
|---|---|---:|---|
| M1 | account scoping in `_judge()` | KILLED | `test_connection_without_account_id_rejects_foreign_account_evidence` |
| M2 | workspace scoping in `_judge()` | KILLED | `test_workspace_scope_isolates_foreign_workspace_exhaustion` |
| M3 | default freshness TTL | KILLED | `test_default_freshness_ttl_rejects_snapshot_without_fresh_until` |
| M4 | unknown-headroom gate in `_judge()` | KILLED (2 tests) | `test_unknown_headroom_admitted_but_not_launchable_without_confirmation` |
| M5 | `launchable()` unknown guard | KILLED | `test_launchable_guard_blocks_unknown_reason_even_if_health_marked_healthy` |
| M6 | `reset_at` in `to_dict()` | KILLED | `test_reset_at_is_preserved_in_admission_record_dict` |
| M7 | `CONCURRENCY_LIMIT` classification | KILLED | `test_distinct_capacity_failure_classes_hard_drop[concurrency_limit]` |
| M8 | `PROVIDER_OVERLOAD` classification | KILLED | `test_distinct_capacity_failure_classes_hard_drop[provider_overload]` |
| M11 | wrapper unscoped ledger key | KILLED | `test_admission_wrapper.py::test_wrapper_exhaustion_hard_drop` |
| **M12** | `route_is_account_bound` gate (`if True:`) | **KILLED** | `test_pool_pinned_route_keeps_own_account_exhaustion_despite_viable_sibling` |
| **M13** | `"unauthorized"` back in `_suppressible_states` | **KILLED** | `test_agnostic_route_keeps_lockout_despite_viable_sibling` |
| **M14** | viability inferred from absence of evidence | **KILLED** | `test_sibling_with_bad_test_status_does_not_confer_viability` |
| **M15** | `testStatus` clause in `_connection_is_viable()` | **KILLED** | `test_sibling_with_bad_test_status_does_not_confer_viability` |

M12-M15 are the four F4-specific mutants; M1-M8 and M11 were re-verified on the
new source rather than carried over from the previous round.

### Test integrity

No existing test was deleted, skipped, weakened or re-asserted. The F1 case
(`test_exhausted_account_does_not_drop_route_for_active_sibling_account`) and
all nine previously killed mutants keep their original assertions. The F4 change
is additive in `tests/` (+5 tests) and touches only the suppression block plus
the two new helpers in `verdict/admission.py`.

## Validation

Focused:
```
$ /tmp/verdict-s3/venv/bin/python -m pytest -q tests/test_admission_subscription.py
21 passed, 1 warning in 0.39s
```

Full gate (run directly, not delegated):
```
$ /tmp/verdict-s3/venv/bin/python -m pytest -q -p no:cacheprovider
3750 passed, 11 skipped, 4 warnings in 324.80s (0:05:24)

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
clean on `s3/bod-262-r3`.

## Existing-test integrity

`test_subscription_snapshot_lockout_precedes_availability`,
`test_snapshot_exhaustion_is_canonical_hard_drop`, and
`test_distinct_pool_windows_keep_identity_and_reset_metadata` retain their
original assertions unchanged. The only pre-existing-test edit was giving
`_inventory()`'s connection fixture a matching `account_id` (required by the
F1 fix, since account-scoped evidence can no longer apply to an
account-agnostic connection).
