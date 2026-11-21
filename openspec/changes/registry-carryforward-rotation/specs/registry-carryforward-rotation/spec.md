# Spec Delta: Discovery registry carry-forward and probe rotation (S5b)

## Context

Before this change, `run_refresh` built the registry only from routes probed
in the current run.  Routes are probed in catalog order (pool-grouped, serial
within a pool) until `wall_budget_s` expires.  With a 300 s budget and 1,000+
FREE routes, only the first few dozen routes in catalog order were ever probed;
routes late in the pool list were never reached.  `save_registry` replaced the
file on each run, so skipped routes lost any prior proof.

This caused two problems: (1) only routes near the top of each pool's catalog
order accumulated proof; and (2) a route with a valid `tool_ok=True` proof from
a previous run lost that proof if the wall budget ran out before it was
re-probed, even though the entry had not yet expired.

## ADDED Requirements

### Requirement: run_refresh SHALL accept a previous registry for carry-forward and rotation

`run_refresh` SHALL accept an optional `previous: DiscoveryRegistry | None`
parameter.  When `previous` is provided:

1. **Rotation** — within each pool, routes are sorted by their `checked_at`
   in `previous` before probing begins.  Routes with no entry in `previous`
   (never-checked) sort first.  Among previously-checked routes, the oldest
   `checked_at` sorts first.  Ties preserve catalog order.

2. **Carry-forward** — when a route's liveness probe is not attempted this
   run because `skip_reason` is `wall_budget_exhausted` or
   `pool_breaker_open`, AND the route has an entry in `previous`, the
   previous entry is kept verbatim in the new registry (including its
   original `checked_at`).  Measured failures (`tool_ok=False`) are carried
   as well as passes.

   Carry-forward does NOT apply to `key_missing` skips: a missing key is
   fresh negative evidence and must produce a new entry.

3. **Dropped routes** — entries in `previous` for routes that are not in this
   run's route list (removed from catalog or no longer classified FREE) are
   silently dropped; they are never carried.

#### Scenario: Unprobed entry keeps its original checked_at

Given a previous registry with entry for route `p/m` checked at `T_old`,
When `run_refresh` is called with `wall_budget_s=0` (all routes immediately
budget-exhausted) and `previous` containing that entry,
Then the new registry contains an entry for `p/m` with `checked_at == T_old`.

#### Scenario: Route reclassified away from FREE is dropped

Given a previous registry with entry for route `p/dropped`,
When `run_refresh` is called with a route list that does NOT contain `p/dropped`,
Then `p/dropped` is NOT in the new registry.

#### Scenario: Probe order rotates never-checked before oldest

Given a previous registry with entries for routes `r_mid` (checked at T_mid)
and `r_old` (checked at T_old, where T_old < T_mid),
And a current route list of `[r_new, r_mid, r_old]` in catalog order,
When `run_refresh` is called with `previous`,
Then the probe order observed is `r_new`, then `r_old`, then `r_mid`.

#### Scenario: Two budget-limited runs cover more routes than one

Given N routes in catalog order,
When run 1 completes with zero budget (no probes),
And run 2 receives `previous=reg1` with a generous budget,
Then run 2 probes all N routes.

### Requirement: DiscoveryRegistry SHALL track carried entry count

`DiscoveryRegistry` SHALL include a `carried: int` field (default 0) recording
the number of entries carried from the previous registry in a given run.  The
field is included in `to_dict` serialization and read back by `from_dict`.

#### Scenario: carried count reflects entries preserved from previous run

Given a previous registry with one entry for route `p/m`,
When `run_refresh` is called with zero budget so the route is skipped,
Then `registry.carried == 1`.

### Requirement: run_refresh_action SHALL load previous WITHOUT registry-level TTL gate

`run_refresh_action` SHALL load the previous registry with `_load_registry_raw`
(which bypasses the registry-level TTL check) and pass it to `run_refresh` as
`previous`.  Per-entry freshness is enforced by `tool_ok_proof`'s
`checked_at`-based TTL; the file-level TTL must not suppress carry-forward for
entries that have not yet expired at the entry level.

#### Scenario: Previous registry loaded without TTL gate for carry-forward

Given a previous registry file whose `written_at` is older than `REGISTRY_TTL_S`,
When `run_refresh_action` is called (not dry-run),
Then the previous registry is loaded and passed to `run_refresh` for rotation
and carry-forward, rather than discarded due to file-level TTL expiry.

### Requirement: refresh summary SHALL include carried count

The summary returned by `_registry_to_summary` SHALL include a `carried` key
in its `counts` dict equal to `registry.carried`.  `render_refresh_plain` SHALL
display the carried count when it is non-zero.

#### Scenario: Summary counts include carried

Given a `DiscoveryRegistry` with `carried=3`,
When `_registry_to_summary` is called,
Then `result["counts"]["carried"] == 3`.
