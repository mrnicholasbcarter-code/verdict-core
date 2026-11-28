# Tasks — quota across a provider's connections

## 1. Fold

- [x] 1.1 `provider_quota` folds a provider's usable connections into `has_quota`, `exhausted` or `unknown` for a route; an inactive connection and one with a failed test status are not usable. Verify: `tests/test_quota_per_connection.py` (`test_the_only_account_with_quota_left_is_not_usable`, `test_a_provider_with_no_usable_account_is_not_decided_by_quota`).
- [x] 1.2 The AVAILABLE stage rejects `quota_low` only for `exhausted`, with the cooldown at the earliest reset among the exhausted connections; one unmeasured connection beside an exhausted one is not rejected and reports no quota percent. Verify: `tests/test_quota_per_connection.py` (`test_one_exhausted_account_among_four_does_not_reject_the_provider`, `test_every_usable_account_exhausted_rejects_until_the_earliest_reset`, `test_the_earliest_reset_ignores_accounts_that_are_not_usable`, `test_unknown_plus_exhausted_is_neither_rejected_nor_reported_as_having_quota`, `test_unknown_quota_is_never_healthy_and_never_exhausted`).
- [x] 1.3 No other stage or reason changes. Verify: `tests/test_quota_per_connection.py::test_a_provider_that_is_not_out_of_quota_still_fails_its_other_stages`, and the existing `tests/test_quota_headroom_routing.py` and `tests/test_orch_eligibility.py` pass unchanged.

## 2. Live check

- [ ] (post-merge) 2.1 On installed main, while an account of the provider holds quota, `verdict eligibility --no-refresh --json` shows Codex routes (`cx/`, `codex/`) past the AVAILABLE stage, with no `quota_low` taken from the exhausted free account. Verify: the command's raw output.
