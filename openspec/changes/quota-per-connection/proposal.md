# A provider is out of quota only when every usable connection of it is

## Why

On 2026-11-28 the gateway reported four Codex connections, all healthy: three Plus accounts with quota left and one free account whose monthly allowance was used up until 2026-12-08T09:45:13Z. `verdict eligibility --no-refresh` rejected 27 of the 34 Codex routes (prefixes `cx/` and `codex/`) at the AVAILABLE stage with `quota_low` and `cooldown_until` 2026-12-08T09:45:13Z. One exhausted account made every Codex route unavailable, while the gateway itself kept serving requests through the other three (GitHub issue #1416). Cheapest qualified tier first needs the subscription pool to be selectable when it holds quota.

What the code did: the ladder reads one quota value per connection (`quota_remaining`, from the gateway's usage report) and dropped every connection with no value before folding. The Plus connections' windows (`session`, `weekly`) carry only `remainingPercentage`, which `quota_from_usage` keeps as named windows, not as the account's quota, so their quota for a Codex route is unknown and they were dropped. The free connection was the only measured one, measured at 0, so "every measured connection is below the floor" held, and the cooldown was the earliest reset of the connections that had one: the free connection's. The gateway's own pool report was correct (4 of 4 available); the fold was Verdict's.

## What Changes

**`verdict/orchestration/eligibility.py`**

- A new `provider_quota` folds the connections of one provider into one answer for one route: `has_quota`, `exhausted` or `unknown`. Only usable connections count: active, with a `testStatus` that is not a failed status.
- The AVAILABLE stage rejects a route with `quota_low` only when the answer is `exhausted`, and the cooldown ends at the earliest reset among the exhausted connections. A provider with an unmeasured connection is not rejected for quota and reports no quota percent.

No other stage, reason, CLI surface or schema changes.

## Capabilities

### New Capabilities

- `route-quota-availability`: when quota alone makes a provider's routes unavailable.

## Impact

`verdict/orchestration/eligibility.py` and `tests/test_quota_per_connection.py`. This widens who is available (it is a slow-lane change, approved on #1416). It can only stop a `quota_low` rejection; it admits nothing that fails another stage, and a route's cost class is not involved.

Invariants this touches (AGENTS.md): unknown is never healthy (an unmeasured connection is never counted as quota left); the admitted set only narrows (the fold runs after admission and before the task gate, and re-admits nothing).

## Evidence boundary

- **KNOWN:**
  - Read-only run of the live read path on 2026-11-28: the four Codex connections, all active with test status `active`. The three Plus connections had `quota_percent` empty and `quota_models` holding `session` and `weekly`; the free connection had `quota_percent` 0 and `quota_reset_at` 2026-12-08T09:45:13Z.
  - `verdict eligibility --no-refresh --json` for the same moment: 27 Codex routes at AVAILABLE, reason `quota_low`, cooldown scope empty.
  - The existing tests `test_one_exhausted_account_does_not_block_a_provider_that_still_has_quota` and `test_every_account_exhausted_blocks_until_the_earliest_reset` pin the cases where every connection is measured; they still hold.
- **INFERRED:** a failed `testStatus` (the set admission already treats as not viable) marks a connection not usable for quota, the same rule `verdict/admission.py` applies when it asks whether a connection is viable.
- **NOT AVAILABLE:** whether the gateway marks `session` and `weekly` windows as account-wide in its usage report. Until it does, Verdict keeps a Plus connection's Codex quota unknown (follow-up on #1416).

## Acceptance criteria

- With three connections with quota left and one exhausted, no route of the provider is rejected `quota_low` and no cooldown is taken from the exhausted connection.
- With every usable connection exhausted, the provider's routes are rejected `quota_low` and the cooldown ends at the earliest reset among them.
- A provider with one unknown and one exhausted connection is not rejected for quota and reports no quota percent.
- A connection that is inactive or has a failed test status does not count as usable.
