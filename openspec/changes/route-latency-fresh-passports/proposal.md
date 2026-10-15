# Reuse fresh route evidence (BOD-350)

## Why
Repeated confirmation of a sufficient fresh shortlist and forced snapshot reloads
on reselect add avoidable latency.

## What Changes
- Reuse an ordered fresh shortlist without confirmation calls.
- Bound a refresh shortlist to max_confirm_candidates requests; replace failed
  confirms only within that bound. Do not persist skipped observations.
- Keep the snapshot TTL cache on bounded reselect while cooling failed routes.

## Impact
No new dependencies or changes to quota, authorization, or cooling authority.
The two-execution-attempt cap remains. Account-status loading is unchanged.
