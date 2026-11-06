# Refresh the health cache from passive OmniRoute traffic

## Why
Doctor failed live with "health-cache.json has no fresh or stale route". The only
scheduled writer of the health cache is the daily bounded screen, and its positive
evidence goes stale after six hours. The three-hour passive identify run already
reads OmniRoute traffic but wrote only `identify-latest.json`.

## What Changes
- The passive identify unit runs with `--refresh-health-cache` and folds OmniRoute
  health-matrix traffic, plus verified webhook events when registered, into the
  health cache as chat-only liveness entries tagged with `evidence_source`.
- Traffic is never tool or agentic proof, never replaces usable tool proof, and
  never shortens an unexpired negative. Evidence age is OmniRoute `lastRequestAt`.
- Doctor requires the saved passive report to carry a zero-call, zero-write
  `health_refresh` without source errors, names the passive source in the
  health-cache failure, and adds a warning-only OmniRoute webhook row.

## Impact
No new cadence: the passive interval stays three hours and the screen timer is
unchanged. No model calls and no OmniRoute writes from the refresh. No new
dependencies. Verdict never changes OmniRoute's private-URL setting.
